"""Small supported app API; adapters are injected, not imported from the robot."""
from __future__ import annotations

import io
import json
import copy
from pathlib import Path
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
import math

from robbi.errors import ValidationError, UnavailableError
from robbi.storage import atomic_write, write_json
from .schema import bounded_json
from .manifest import confined
from .permissions import AppPermissionRequired
from .undo import write_state_file
from robbi.orchestration import APP, Intent, intent_scope


class AppCancelled(UnavailableError):
    pass


def health_only_status(value):
    """Status permission exposes diagnostics, never captured sensor payloads."""
    if not isinstance(value, dict):
        return {}
    fields = {"available", "enabled", "fresh", "state", "last_error", "error", "hint", "message"}
    def select(item, allowed):
        if not isinstance(item, dict):
            return {}
        return {key: child for key, child in item.items()
                if key in allowed and (child is None or isinstance(child, (str, bool, int, float)))}
    result = {"health": {}, "components": {}}
    summary = value.get("health", {})
    if isinstance(summary, dict):
        result["health"] = select(summary, fields | {"version", "mode", "uptime_s"})
        entries = summary.get("components", [])
        if isinstance(entries, list):
            result["health"]["components"] = [select(item, fields | {"name"})
                for item in entries[:64] if isinstance(item, dict)]
    components = value.get("components", {})
    if isinstance(components, dict):
        for name in ("camera", "motion", "tof", "imu", "media", "microphone"):
            if name not in components:
                continue
            result["components"][name] = select(components[name], fields)
            if name == "media" and isinstance(components[name], dict):
                for output in ("display", "audio"):
                    if isinstance(components[name].get(output), dict):
                        result["components"][name][output] = select(components[name][output], fields)
    outputs = value.get("outputs")
    if isinstance(outputs, dict):
        result["outputs"] = select(outputs, {"waiting", "active_leases", "rejections", "preemptions"})
        result["outputs"]["limits"] = select(outputs.get("limits"),
            {"resources", "waiters", "max_wait_s", "max_lease_s", "active_leases"})
        resources = outputs.get("resources", {})
        if isinstance(resources, dict):
            result["outputs"]["resources"] = {name: select(item,
                {"owner", "intent_id", "priority", "references", "state", "remaining_s", "hold_remaining_s", "reason"})
                for name, item in resources.items() if name in {"display", "audio", "motion"}}
    return bounded_json(result)


class Cancellation:
    def __init__(self, event, external, deadline):
        self.event, self.external, self.deadline = event, external, deadline

    def __call__(self):
        return self.event.is_set() or time.monotonic() >= self.deadline or bool(self.external and self.external())

    is_set = __call__

    def wait(self, seconds):
        until = time.monotonic() + max(0, seconds)
        while not self() and time.monotonic() < until:
            self.event.wait(min(.05, max(0, until - time.monotonic())))
        return self()


class Artifacts:
    """Keep only screen-sized PNGs: bounded storage and no full-resolution cache."""
    LIMIT = 12

    def __init__(self, directory, app_id):
        self.directory, self.app_id = Path(directory), app_id
        self._lock = threading.RLock()

    def list(self):
        if not self.directory.exists():
            return []
        result = []
        for path in sorted(self.directory.glob("*.json"), reverse=True)[:self.LIMIT]:
            try:
                if path.stat().st_size <= 4096:
                    item = json.loads(path.read_text(encoding="utf-8"))
                    self.path(item["id"])
                    result.append(item)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(result, key=lambda item: item.get("created_at", 0), reverse=True)

    def path(self, artifact_id):
        if not isinstance(artifact_id, str) or len(artifact_id) != 32 or any(char not in "0123456789abcdef" for char in artifact_id):
            raise ValidationError("Invalid image artifact ID")
        path = self.directory / f"{artifact_id}.png"
        if not path.is_file() or path.is_symlink():
            raise UnavailableError("Image is no longer retained")
        return path

    def save(self, data, title, checkpoint):
        checkpoint()
        if not isinstance(data, bytes) or not 0 < len(data) <= 12 * 1024 * 1024:
            raise ValidationError("Generated image must contain at most 12 MiB")
        from PIL import Image, ImageOps, UnidentifiedImageError
        try:
            with Image.open(io.BytesIO(data)) as source:
                if source.width * source.height > 16_000_000 or source.width < 1 or source.height < 1:
                    raise ValidationError("Generated image exceeds 16 million pixels")
                source.draft("RGB", (560, 560))
                # Shrink before EXIF transpose copies pixels, allowing either
                # orientation; fit once more afterwards to avoid clipping.
                source.thumbnail((280, 280))
                image = ImageOps.exif_transpose(source)
                image.thumbnail((280, 240))
                image = image.convert("RGBA")
                canvas = Image.new("RGB", (280, 240), "white")
                canvas.paste(image, ((280-image.width)//2, (240-image.height)//2), image)
                output = io.BytesIO()
                canvas.save(output, format="PNG", optimize=False)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise ValidationError("The image provider returned an invalid image") from error
        checkpoint()
        artifact_id = uuid.uuid4().hex
        item = {"id": artifact_id, "title": str(title)[:160], "width": 280, "height": 240,
                "mime": "image/png", "created_at": time.time(),
                "url": f"/api/apps/{self.app_id}/artifacts/{artifact_id}"}
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            atomic_write(self.directory / f"{artifact_id}.png", output.getvalue())
            try:
                write_json(self.directory / f"{artifact_id}.json", item)
            except BaseException:
                (self.directory / f"{artifact_id}.png").unlink(missing_ok=True)
                raise
            retained = sorted(self.directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
            for old in retained[self.LIMIT:]:
                old.with_suffix(".png").unlink(missing_ok=True)
                old.unlink(missing_ok=True)
        return item


class RobotAPI:
    def __init__(self, context, port, token):
        self._context, self._port, self._token = context, port, token
        # Execution still uses the job deadline below. A completed screen may
        # remain held for up to 120 seconds and must keep its cancellation epochs.
        source = context.cancelled
        if type(source) is Cancellation:
            presentation_cancelled = lambda: (source.event.is_set()
                or bool(source.external and source.external()))
            intent_deadline = context.deadline + 120
        else:
            presentation_cancelled = source
            intent_deadline = context.deadline
        self._intent = Intent("app:" + context.app_id, APP,
                              cancelled=presentation_cancelled, deadline=intent_deadline)
        self._signal = Cancellation(self._intent, None, context.deadline)
        self._signal.presentation_signal = self._intent

    def _call(self, callback, *args, **kwargs):
        self._context.checkpoint()
        if self._context._read_only and getattr(callback, "__name__", "") not in {"tof", "imu", "camera_snapshot", "assets"}:
            raise ValidationError("Composed read-only tools cannot control robot outputs")
        if getattr(callback, "__name__", "") not in {"tof", "imu", "camera_snapshot", "assets", "timer"}:
            self._context.note_non_undoable("Movement, sound and earlier screen output cannot be reversed.")
        with intent_scope(self._intent):
            name = getattr(callback, '__name__', '')
            if name in {'show_image', 'show_face', 'face_animation', 'play_video', 'gesture'}:
                self._context._screen_requested = True
            if name in {'play_sound', 'speak', 'play_video'}:
                self._context._audio_requested = True
            receipt = callback(*args, **kwargs)
            if name in {'show_image', 'show_face', 'face_animation', 'play_video', 'gesture'}:
                self._context._last_screen_receipt = receipt
            if name in {'play_sound', 'speak', 'play_video'}:
                self._context._last_audio_receipt = receipt
            return receipt

    @staticmethod
    def _number(value, name, low, high):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValidationError(f"{name} must be between {low} and {high}")
        return value

    @staticmethod
    def _name(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 150 or '/' in value or '\\' in value or ':' in value:
            raise ValidationError("Use an asset name from robbi.engine.assets(), not a file path")
        return value

    def status(self):
        self._context.require("robot.status")
        return health_only_status(self._port.status())

    def speak(self, text):
        self._context.require("robot.speak")
        if not isinstance(text, str) or not 1 <= len(text) <= 4000:
            raise ValidationError("Speech must contain 1 to 4000 characters")
        if hasattr(self._port, "speak"):
            return self._call(self._port.speak, text, self._token, self._signal)
        return self._call(self._port.action, {"speak": text}, self._token, self._signal)

    def show_image(self, artifact_id, *, hold_s=60):
        self._context.require("robot.display")
        self._number(hold_s, "hold_s", 0, 120)
        path = self._context._artifacts.path(artifact_id)
        kwargs = {} if hold_s == 60 else {"hold_s": hold_s}
        return self._call(self._port.show_image, path, self._token, self._signal, **kwargs)

    def capture_image(self):
        self._context.require("robot.camera")
        result = self._port.capture_image()
        self._context.checkpoint()
        if result is None:
            raise UnavailableError("Camera is unavailable; check the camera connection in Overview")
        return result

    def listen_claps(self, *, duration_s=6):
        self._context.require("robot.microphone")
        self._number(duration_s, "duration_s", 1, 8)
        if self._context._read_only:
            raise ValidationError("Composed tools cannot start microphone capture")
        callback = getattr(self._port, "listen_claps", None)
        if callback is None:
            raise UnavailableError("Clap listening is unavailable; use the tap input instead")
        result = self._call(callback, duration_s, self._token, self._signal)
        self._context.require("robot.microphone")
        return bounded_json(result)

    def gesture(self, name, *, with_motion=True, with_audio=True):
        if type(with_motion) is not bool or type(with_audio) is not bool:
            raise ValidationError('Gesture output switches must be booleans')
        self._context.request_permissions('robot.display', *(['robot.motion'] if with_motion else []),
                                          *(['robot.audio'] if with_audio else []))
        return self._call(self._port.gesture, self._name(name), self._token, self._signal,
                          with_motion=with_motion, with_audio=with_audio)

    def face_animation(self, name, *, fps=10, loops=1):
        self._context.require('robot.display')
        self._number(fps, 'fps', 1, 30)
        if type(loops) is not int or not 1 <= loops <= 30:
            raise ValidationError('loops must be an integer from 1 to 30')
        return self._call(self._port.face_animation, self._name(name), self._token, self._signal, fps=fps, loops=loops)

    def timer(self, operation, **arguments):
        self._context.request_permissions('robot.reminders')
        if operation not in {'start', 'list', 'cancel'}:
            raise ValidationError('Unknown timer operation')
        if operation == 'start':
            self._number(arguments.get('seconds'), 'seconds', 1, 86400)
            if not isinstance(arguments.get('label'), str) or not 1 <= len(arguments['label'].strip()) <= 80:
                raise ValidationError('Supply a timer label of 1 to 80 characters')
        if operation == 'cancel' and (not isinstance(arguments.get('id'), str) or not 1 <= len(arguments['id']) <= 80):
            raise ValidationError('Supply the returned timer ID')
        result = self._call(self._port.timer, operation, arguments, self._token, self._signal)
        if operation == "start" and isinstance(result, dict) and result.get("ok"):
            timer_id = result.get("data", {}).get("id")
            if isinstance(timer_id, str) and 1 <= len(timer_id) <= 80:
                self._context.register_undo(lambda context: context.robot.timer("cancel", id=timer_id),
                    "Cancel the timer started by this action", permissions=("robot.reminders",))
            else:
                self._context.note_non_undoable("The timer returned no exact ID, so it cannot be cancelled automatically.")
        elif operation == "cancel":
            self._context.note_non_undoable("A cancelled timer cannot be restored with its original timing.")
        return result

    def move(self, *, direction, speed=100, duration=.5):
        self._context.require("robot.motion")
        if direction not in {"forward", "backward", "left", "right"}:
            raise ValidationError("direction must be forward, backward, left or right")
        self._number(speed, "speed", 1, 100)
        self._number(duration, "duration", .05, 3)
        spec = {"direction": direction, "speed": speed, "duration": duration}
        if hasattr(self._port, "move"):
            return self._call(self._port.move, spec, self._token, self._signal)
        return self._call(self._port.action, {"drive": spec}, self._token, self._signal)

    def tof(self, sensor="h1"):
        self._context.require("robot.tof")
        if sensor not in {"h1", "h2"}:
            raise ValidationError("ToF sensor must be h1 or h2")
        return bounded_json(self._call(self._port.tof, sensor))

    def imu(self):
        self._context.require("robot.imu")
        return bounded_json(self._call(self._port.imu))

    def camera_snapshot(self, *, max_width=640, quality=75, max_age_s=2):
        self._context.require("robot.camera")
        if type(max_width) is not int or type(quality) is not int:
            raise ValidationError("Image width and quality must be integers")
        self._number(max_width, "max_width", 64, 640)
        self._number(quality, "quality", 25, 85)
        self._number(max_age_s, "max_age_s", .05, 2)
        result = self._call(self._port.camera_snapshot, max_width=max_width, quality=quality, max_age_s=max_age_s)
        self._context.checkpoint()
        return result

    def assets(self, kind):
        self._context.require("robot.assets")
        if kind not in {"faces", "sounds", "videos"}:
            raise ValidationError("Asset kind must be faces, sounds or videos")
        return bounded_json(self._call(self._port.assets, kind))

    def play_sound(self, name):
        self._context.require("robot.audio")
        return self._call(self._port.play_sound, self._name(name), self._token, self._signal)

    def play_sound_file(self, relative_path):
        self._context.require("robot.audio")
        if self._context._project_root is None:
            raise ValidationError("This app has no project assets")
        path = confined(self._context._project_root, relative_path)
        if not path.is_file() or path.suffix.lower() != '.wav' or path.stat().st_size > 8 * 1024 * 1024:
            raise ValidationError("App sounds must be local WAV files of at most 8 MiB")
        return self._call(self._port.play_sound, path, self._token, self._signal)

    def show_face(self, name, *, hold_s=15):
        self._context.require("robot.display")
        self._number(hold_s, "hold_s", 0, 120)
        return self._call(self._port.show_face, self._name(name), self._token, self._signal, hold_s=hold_s)

    def play_video(self, name):
        self._context.request_permissions("robot.display", "robot.audio")
        return self._call(self._port.play_video, self._name(name), self._token, self._signal)

    def display_text(self, text, *, hold_s=15):
        return self.display_card(text, hold_s=hold_s)

    def display_card(self, text, *, title=None, kind='card', values=(), animate=False, hold_s=30):
        """Show an illustrated app result with an optional short reveal animation."""
        if self._context._read_only:
            raise ValidationError("Composed read-only tools cannot control robot outputs")
        self._context.require("robot.display")
        self._number(hold_s, "hold_s", 0, 120)
        if not isinstance(text, str) or not 1 <= len(text) <= 500:
            raise ValidationError("Display text must contain 1 to 500 characters")
        if title is not None and (not isinstance(title, str) or not 1 <= len(title) <= 80):
            raise ValidationError('Card title must contain 1 to 80 characters')
        if kind not in {'card', 'coin', 'dice', 'choose', 'shuffle', 'numbers'} or type(animate) is not bool:
            raise ValidationError('Unknown result card or animation setting')
        if not isinstance(values, (list, tuple)) or len(values) > 100 or any(
                not isinstance(value, (str, int)) or isinstance(value, bool) or len(str(value)) > 80 for value in values):
            raise ValidationError('Card values must be a bounded list of numbers or short text')
        if kind == 'coin' and (len(values) != 1 or values[0] not in {'Heads', 'Tails'}):
            raise ValidationError('A coin card requires its one sampled Heads or Tails value')
        if kind == 'dice' and (not 1 <= len(values) <= 10 or any(type(value) is not int or not 1 <= value <= 100 for value in values)):
            raise ValidationError('Dice cards require one to ten actual rolls from 1 to 100')
        from .screen import card_image
        artwork = None
        if self._context._project_root is not None:
            artwork = confined(self._context._project_root, 'images/screen.png')
        options = dict(title=title or getattr(self._context, '_presentation_title', self._context.app_id.replace('-', ' ').title()),
                       artwork=artwork, kind=kind, values=values)
        # The final artifact is saved last, so bounded artifact retention cannot
        # evict it while the display holds it. All waits observe Stop/deadlines.
        session = self.session('display', duration_s=5) if animate and hasattr(self._port, 'session') else nullcontext()
        with session:
            if animate:
                for phase in (.0, .2, .4, .6, .8, 1.0):
                    self._context.checkpoint()
                    frame, _ = card_image('A little suspense...', phase=phase, **options)
                    preview = self._context.save_image(frame, 'Result reveal')
                    receipt = self.show_image(preview['id'], hold_s=2)
                    if receipt.get('displayed') is not True:
                        break
                    self._context.wait(.13)
            data, clipped = card_image(text, **options)
            artifact = self._context.save_image(data, text[:100])
            receipt = self.show_image(artifact['id'], hold_s=hold_s)
        return {**receipt, 'artifact': artifact, 'truncated': clipped}

    @contextmanager
    def session(self, *resources, duration_s=15):
        """Reserve a short scene atomically; do remote work before entering."""
        if self._context._read_only:
            raise ValidationError("Composed read-only tools cannot reserve robot outputs")
        self._number(duration_s, "duration_s", .05, 120)
        if not resources or len(resources) > 3 or any(r not in {"display", "audio", "motion"} for r in resources):
            raise ValidationError("Reserve display, audio and/or motion")
        permissions = []
        for resource in resources:
            if resource == 'audio' and "robot.speak" in self._context.permissions:
                permissions.append("robot.speak")
            else:
                permissions.append("robot." + resource)
        self._context.request_permissions(*permissions)
        with intent_scope(self._intent):
            with self._port.session(resources, self._token, self._signal, duration_s):
                yield self
                self._context.checkpoint()


class _ServiceAccess:
    """Context-bound calls reuse the host provider without exporting it."""

    def __init__(self, context, factory, service):
        self._context, self._factory, self._service_name = context, factory, service

    def _invoke(self, method, *args, cancellation=None, deadline=None, **kwargs):
        if self._context._read_only and method in {"generate", "calendar_create", "calendar_undo_create", "gmail_draft"}:
            raise ValidationError("Composed read-only tools cannot make remote changes")
        if self._service_name == 'google':
            permission = 'account.gmail' if method.startswith('gmail_') else 'account.calendar'
            self._context.require(permission)
        self._context.require("network")
        if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                                     or not math.isfinite(deadline)):
            raise ValidationError("Provider deadline must be a finite monotonic timestamp")
        deadline = self._context.deadline if deadline is None else min(deadline, self._context.deadline)
        if cancellation is not None and not (callable(cancellation) or callable(getattr(cancellation, "is_set", None))):
            raise ValidationError("Provider cancellation must be a callback or event")
        def caller_cancelled():
            if cancellation is None:
                return False
            try:
                return bool(cancellation.is_set() if callable(getattr(cancellation, "is_set", None)) else cancellation())
            except Exception:
                return True
        signal = Cancellation(self._context.robot._signal, caller_cancelled, deadline)
        if signal():
            raise AppCancelled("Provider request cancelled or its deadline expired")
        provider = self._factory(self._service_name)
        if self._service_name == 'google':
            self._context.require(permission)
        self._context.require("network")
        if signal():
            raise AppCancelled("Provider request cancelled or its deadline expired")
        try:
            result = getattr(provider, method)(*args, cancellation=signal, deadline=deadline, **kwargs)
        except BaseException:
            if self._service_name == "google" and method in {"calendar_create", "gmail_draft"}:
                self._context.note_non_undoable("Google creation was not confirmed. Check Google before repeating or removing the change.")
            elif self._service_name == "images" and method == "generate":
                self._context.note_non_undoable("Image generation and any provider charges cannot be undone.")
            raise
        # Capture confirmed remote effects before cancellation discards their
        # visible response. Undo uses a fresh context, never this old signal.
        if self._service_name == "google" and method == "calendar_create":
            receipt = result.get("undo_receipt") if isinstance(result, dict) else None
            if isinstance(receipt, dict):
                config, receipt = copy.deepcopy(args[0]), copy.deepcopy(receipt)
                self._context.register_undo(
                    lambda context: context.services.google.calendar_undo_create(config, receipt),
                    "Delete the unchanged calendar event created by this action", permissions=("network", "account.calendar"))
            else:
                self._context.note_non_undoable("Calendar returned no version receipt; remove the event in Google Calendar.")
        elif self._service_name == "google" and method == "gmail_draft":
            self._context.note_non_undoable("Gmail cannot safely condition deletion on the original draft version. Review or delete the draft in Gmail.")
        elif self._service_name == "images" and method == "generate":
            self._context.note_non_undoable("Image generation and any provider charges cannot be undone.")
        self._context.checkpoint()
        if signal():
            raise AppCancelled("Provider completed after cancellation or deadline")
        # Also catches revocation for standalone contexts without an AppHost epoch.
        if self._service_name == 'google':
            self._context.require(permission)
        self._context.require("network")
        return result


class _ImageAccess(_ServiceAccess):
    def generate(self, config, prompt, *, cancellation=None, deadline=None):
        return self._invoke("generate", config, prompt, cancellation=cancellation, deadline=deadline)


class _WeatherAccess(_ServiceAccess):
    def forecast(self, config, location="", day="tomorrow", *, latitude=None, longitude=None,
                 timezone=None, country_code=None, cancellation=None, deadline=None):
        return self._invoke("forecast", config, location, day, latitude=latitude, longitude=longitude,
                            timezone=timezone, country_code=country_code, cancellation=cancellation, deadline=deadline)


class _GoogleAccess(_ServiceAccess):
    def calendar_events(self, config, **kwargs):
        return self._invoke("calendar_events", config, **kwargs)

    def calendar_create(self, config, **kwargs):
        return self._invoke("calendar_create", config, **kwargs)

    def calendar_undo_create(self, config, receipt, **kwargs):
        return self._invoke("calendar_undo_create", config, receipt, **kwargs)

    def gmail_search(self, config, **kwargs):
        return self._invoke("gmail_search", config, **kwargs)

    def gmail_read(self, config, message_id, **kwargs):
        return self._invoke("gmail_read", config, message_id, **kwargs)

    def gmail_draft(self, config, **kwargs):
        return self._invoke("gmail_draft", config, **kwargs)


class _SearchAccess(_ServiceAccess):
    def search(self, config, query, **kwargs):
        return self._invoke("search", config, query, **kwargs)


class _MapsAccess(_ServiceAccess):
    def geocode(self, config, address, **kwargs):
        return self._invoke("geocode", config, address, **kwargs)

    def route(self, config, origin, destination, **kwargs):
        return self._invoke("route", config, origin, destination, **kwargs)


class ServiceAccess:
    def __init__(self, context, factory):
        self._context = context
        self._images = _ImageAccess(context, factory, "images")
        self._weather = _WeatherAccess(context, factory, "weather")
        self._google = _GoogleAccess(context, factory, "google")
        self._search = _SearchAccess(context, factory, "search")
        self._maps = _MapsAccess(context, factory, "maps")

    @property
    def images(self):
        self._context.require("network")
        return self._images

    @property
    def weather(self):
        self._context.require("network")
        return self._weather

    @property
    def google(self):
        self._context.require("network")
        return self._google

    @property
    def search(self):
        self._context.require("network")
        return self._search

    @property
    def maps(self):
        self._context.require("network")
        return self._maps


class AppContext:
    def __init__(self, *, app_id, config, permissions, directory, artifacts, cancelled, deadline, robot, robot_token, services, project_root=None,
                 permission_check=None, permission_check_many=None, undo_plan=None, app_reader=None, read_only=False,
                 provider_config=None):
        self.app_id, self.config = app_id, bounded_json(config)
        # Only explicit owner overrides, never defaults from an imported manifest.
        self._provider_config = bounded_json(provider_config or {})
        self.permissions = frozenset(permissions)
        self._permission_check, self._permission_check_many = permission_check, permission_check_many
        self._undo_plan = undo_plan
        self._app_reader, self._read_only, self._read_count = app_reader, read_only, 0
        self.deadline, self.cancelled = deadline, cancelled
        self._directory, self._artifacts = Path(directory), artifacts
        self._project_root = Path(project_root) if project_root is not None else None
        self.robot = RobotAPI(self, robot, robot_token)
        self.services = ServiceAccess(self, services)

    def call_app(self, app_id, tool, arguments):
        self.require("apps.read")
        if self._read_only or self._app_reader is None:
            raise ValidationError("Nested app composition is not supported")
        if self._read_count >= 8:
            raise ValidationError("A tool may read at most eight other app tools")
        self._read_count += 1
        result = self._app_reader(app_id, tool, arguments)
        self.require("apps.read")
        if result.get("permission_required"):
            raise AppPermissionRequired(result.get("app", app_id), result.get("permissions", []),
                                        denied=result.get("state") == "permission_denied", text=result.get("text"))
        if result.get("cancelled"):
            raise AppCancelled("Composed app read was cancelled")
        return result

    def checkpoint(self):
        if self.cancelled() or (hasattr(self, 'robot') and self.robot._intent.is_set()):
            raise AppCancelled("App cancelled or its deadline expired")

    def wait(self, seconds):
        RobotAPI._number(seconds, "seconds", 0, 120)
        self.checkpoint()
        self.robot._signal.wait(seconds)
        self.checkpoint()

    def require(self, permission):
        self.checkpoint()
        if self._permission_check is not None:
            self._permission_check(permission)
        elif permission not in self.permissions:
            raise ValidationError(f"App does not declare permission: {permission}")

    def request_permissions(self, *permissions):
        """Preflight capabilities before side effects; approval never waits here."""
        self.checkpoint()
        if not permissions:
            return
        if self._permission_check_many is not None:
            self._permission_check_many(permissions)
            return
        pending, denied, message = set(), False, None
        for permission in permissions:
            try:
                self.require(permission)
            except AppPermissionRequired as error:
                pending.update(error.details["permissions"])
                denied = denied or error.details["state"] == "permission_denied"
                message = error.details["text"]
        if pending:
            raise AppPermissionRequired(self.app_id, pending, denied=denied, text=message)

    def read_state(self):
        self.require("state")
        path = self._directory / "state.json"
        if not path.exists():
            return {}
        if path.stat().st_size > 65536:
            raise ValidationError("App state exceeds 64 KiB")
        try:
            result = bounded_json(json.loads(path.read_text(encoding="utf-8")))
            if not isinstance(result, dict):
                raise ValidationError("App state must be an object")
            return result
        except (OSError, ValueError, RecursionError) as error:
            raise ValidationError("App state is unreadable; repair its state.json or restore a backup") from error

    def write_state(self, state):
        if self._read_only:
            raise ValidationError("Composed read-only tools cannot change saved state")
        self.require("state")
        if not isinstance(state, dict):
            raise ValidationError("App state must be an object")
        result = bounded_json(state)
        before = self.read_state() if self._undo_plan is not None else None
        before_exists = (self._directory / "state.json").exists()
        self.checkpoint()
        write_state_file(self._directory / "state.json", result)
        if self._undo_plan is not None:
            self._undo_plan.state_written(before, result, before_exists)

    def register_undo(self, callback, description, *, permissions=()):
        """Register an explicit compensation accepting a fresh AppContext.

        Registration itself has no side effect and must work even when the
        original request was cancelled immediately after a confirmed write.
        """
        if self._read_only:
            raise ValidationError("Composed read-only tools cannot register mutation callbacks")
        if self._undo_plan is not None:
            self._undo_plan.register(callback, description, permissions)

    def note_non_undoable(self, reason):
        if self._undo_plan is not None:
            self._undo_plan.note(reason)

    def save_image(self, data, title="Generated image"):
        if self._read_only:
            raise ValidationError("Composed read-only tools cannot create artifacts")
        self.require("robot.display")
        result = self._artifacts.save(data, title, self.checkpoint)
        self.note_non_undoable("Generated image files and previous screen output are kept.")
        return result
