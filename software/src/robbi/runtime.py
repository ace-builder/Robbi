"""Small coordinator. Feature startup never blocks the HTTP listener."""
from __future__ import annotations

import copy
import logging
import threading
import time

from . import __version__
from .errors import UnavailableError, ValidationError
from .health import HealthRegistry
from .jobs import JobRegistry
from .plugins import PluginRegistry
from .app_runtime.host import AppHost
from .app_runtime.robot_port import RuntimeRobotPort
from .activity import create_sink
from .audio_studio import AudioStudio
from .filming.service import FilmingStudio
from .services.morning_briefing import MorningBriefingService

LOG = logging.getLogger(__name__)


class Runtime:
    def __init__(self, config, controller_factory=None):
        self.config = config
        studio = config.values.get("audio_studio", {})
        self.audio_studio = AudioStudio(config.data_dir,
            max_storage_mb=studio.get("max_storage_mb", 128),
            max_clip_mb=studio.get("max_clip_mb", 16),
            history_limit=studio.get("history_limit", 500))
        self.recording = create_sink(config)
        self.health = HealthRegistry()
        self.health.set("runtime", "starting", "Starting local services")
        self.health.set("configuration", "degraded" if config.issues else "ready",
                        "; ".join(config.issues) if config.issues else "Configuration validated",
                        "Correct the configuration and restart; safe defaults are active" if config.issues else "")
        self.jobs = JobRegistry(max_pending=int(config.values.get("runtime", {}).get("command_queue_size", 2)))
        self._factory = controller_factory
        self._controller = None
        self.filming = FilmingStudio(self)
        from .conversation_lab.service import ConversationLab
        self.conversation_lab = ConversationLab(self)
        self._legacy_app = None
        self._started_at = time.monotonic()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._snapshot = {}
        self._snapshot_at = None
        self._asset_revision = 0
        self._lock = threading.Lock()
        self._generation = 0
        self.plugins = PluginRegistry(config.values.get("plugins", {}).get("enabled", []), config.data_dir, self.health)
        self.app_host = AppHost(config.root, config.data_dir, RuntimeRobotPort(self),
                                permissions_enabled=config.values.get("apps", {}).get("permissions_enabled", True),
                                on_game_turn=self._on_game_turn)
        from .app_runtime.manager import AppManager
        self.app_manager = AppManager(self.app_host, config.data_dir)
        self.app_management_jobs = JobRegistry(max_workers=1, max_pending=2, history_limit=16)
        self.briefing_jobs = JobRegistry(max_workers=1, max_pending=0, history_limit=8)
        self.morning_briefing = MorningBriefingService(
            config.data_dir / "morning-briefing.json", self.app_host,
            deliver=self._deliver_morning_briefing, can_run=self._briefing_ready,
            capture_cancellation=self._briefing_cancellation)

    def _on_game_turn(self, session, source):
        controller = self._controller
        callback = getattr(controller, '_game_turn_completed', None)
        if callback is not None:
            callback(session, source)

    def _briefing_ready(self):
        if self.filming.enabled:
            return False
        if not self._ready.is_set() or self._stop.is_set() or self._controller is None:
            return False
        with self._lock:
            snapshot = self._snapshot
            if (snapshot.get("thinking") or snapshot.get("presentation", {}).get("thinking")
                    or snapshot.get("behavior", {}).get("awake") is False
                    or snapshot.get("focus_timer", {}).get("active")
                    or snapshot.get("voice", {}).get("processing")
                    or snapshot.get("audio", {}).get("playing")
                    or snapshot.get("microphone", {}).get("voice_state", "idle")
                    not in {"idle", "disabled", "detecting_wake"}):
                return False
        coordinator = getattr(self._controller, "orchestrator", None)
        if coordinator and any(value.get("priority", 0) >= 20 for value in coordinator.snapshot().get("resources", {}).values()):
            return False
        autonomy = getattr(self._controller, "autonomy_status", None)
        if autonomy:
            status = autonomy()
            policy = status.get("policy", status)
            if policy.get("operator_paused") or policy.get("paused"):
                return False
        return True

    def _briefing_cancellation(self):
        generation = self._generation
        controller = self.controller()
        capture = getattr(controller, "_action_token", None)
        token = capture() if capture else None
        return lambda: self._stop.is_set() or self._generation != generation or bool(token and token[1].is_set())

    def _deliver_morning_briefing(self, result, *, cancellation, deadline):
        from .orchestration import AUTONOMY, Intent, intent_scope
        if cancellation() or not self._briefing_ready():
            return {"delivered": False}
        port = RuntimeRobotPort(self)
        token = port.capture_token()
        intent = Intent("morning-briefing", AUTONOMY, cancelled=cancellation, deadline=deadline)
        with intent_scope(intent):
            receipt = port.speak(result["text"][:4000], token, lambda: cancellation() or intent.is_set())
        return {"delivered": bool(receipt.get("ok"))}

    def submit_briefing(self, *, preview=False):
        self.controller()
        if type(preview) is not bool:
            raise ValidationError("preview must be true or false")
        generation = self._generation
        runtime_cancelled = self._briefing_cancellation()
        settings_cancelled = self.morning_briefing.capture_cancellation()
        cancellation = lambda: runtime_cancelled() or settings_cancelled()
        def run():
            if generation != self._generation or self._stop.is_set():
                return {"ok": False, "cancelled": True, "text": "Briefing cancelled before it started."}
            return self.morning_briefing.run(preview=preview, cancellation=cancellation)
        return self.briefing_jobs.submit("morning_briefing", run)

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="runtime-supervisor")
        self._thread.start()

    def _run(self):
        try:
            if self._factory is None:
                from robbi.engine.controller import RobotController
                from robbi.engine.webapp import create_app
                runtime_config = self.config.values.get("runtime", {})
                controller = RobotController(recording=self.recording, feature_task_limit=int(runtime_config.get("feature_task_limit", 16)), diagnostic_intervals={
                    "fast_s": float(runtime_config.get("status_interval_s", 2)),
                    "summary_s": float(runtime_config.get("summary_interval_s", 15)),
                    "catalog_s": float(runtime_config.get("catalog_interval_s", 60)),
                })
                legacy_app = create_app(controller)
                from .web import install_error_handlers
                install_error_handlers(legacy_app)
                self._legacy_app = legacy_app
            else:
                controller = self._factory()
            self._controller = controller
            controller.recording = self.recording
            controller.app_host = self.app_host
            controller.app_manager = self.app_manager
            self.filming.bind(controller)
            if hasattr(controller, "execution"):
                self.app_host.bind_scheduler(controller.execution.lane("apps"))
            if self._stop.is_set():
                controller.stop()
                return
            controller.start()
            with self._lock:
                stopped = self._stop.is_set()
                if not stopped:
                    self._ready.set()
                    self.health.set("runtime", "ready", "Local services are running")
            if stopped:
                controller.stop()
                return
            # Trusted optional app initialization may block; it must not hold up
            # local tools, component diagnostics or the supervisor's status loop.
            threading.Thread(target=self._load_plugins, daemon=True, name="app-loader").start()
        except Exception as error:
            LOG.exception("Feature startup failed; diagnostics remain online")
            if self._stop.is_set():
                return
            self.health.set("runtime", "failed", str(error), "Check diagnostics and installed optional dependencies, then restart")
            return
        interval = float(self.config.values.get("runtime", {}).get("status_interval_s", 2))
        while not self._stop.is_set():
            try:
                drain = getattr(controller, "drain_background_work_delivery", None)
                if drain is not None:
                    drain()
            except Exception as error:
                LOG.exception("Background completion delivery deferred")
                self.health.set("background-delivery", "degraded", str(error), "Saved results remain available in Interact")
            try:
                with self._lock:
                    asset_revision = self._asset_revision
                snapshot = controller.status()
                if self.recording is not None:
                    self.recording.sample_imu(snapshot)
                with self._lock:
                    if self._asset_revision != asset_revision and "assets" in self._snapshot:
                        snapshot["assets"] = copy.deepcopy(self._snapshot["assets"])
                    self._snapshot = snapshot
                    self._snapshot_at = time.monotonic()
                # The status sample already contains component health. Do not
                # re-read every device just to publish the same diagnostics.
                entries = snapshot.get("components")
                if entries is None and hasattr(controller, "health_snapshot"):
                    entries = controller.health_snapshot()
                if entries is not None:
                    for entry in entries:
                        self.health.set(entry["name"], entry["state"], entry.get("message", ""), entry.get("hint", ""), entry.get("retries", 0))
            except Exception as error:
                LOG.exception("Status refresh failed")
                self.health.set("status", "degraded", str(error), "Other components remain available")
            else:
                self.health.set("status", "ready", "Diagnostics current")
            self._stop.wait(interval)

    def _load_plugins(self):
        try:
            self.app_host.scan()
            self.morning_briefing.start()
            self.plugins.load()
        except Exception as error:
            LOG.exception("Optional app discovery failed")
            self.health.set("apps", "degraded", str(error), "Check installed app packages; local features remain available")

    def controller(self):
        if self._controller is None or not self._ready.is_set() or self._stop.is_set():
            raise UnavailableError("Robbi is starting or stopped; diagnostics are still available")
        return self._controller

    def snapshot(self):
        if self._stop.is_set():
            raise UnavailableError("Robbi has stopped")
        with self._lock:
            if not self._snapshot:
                raise UnavailableError("Components are starting; please wait a moment")
            snapshot = copy.deepcopy(self._snapshot)
        if self.recording is not None:
            snapshot["recording"] = self.recording.status()
        return snapshot

    def health_snapshot(self):
        entries = self.health.snapshot()
        state = "ready"
        if self._stop.is_set():
            state = "stopped"
        elif any(entry["state"] in {"failed", "degraded", "retrying"} for entry in entries):
            state = "degraded"
        elif any(entry["state"] == "starting" for entry in entries):
            state = "starting"
        return {"version": __version__, "state": state, "uptime_s": round(time.monotonic() - self._started_at, 1),
                "components": entries, "mode": "hardware" if self.config.values.get("hardware", {}).get("enable_motors") else "development"}

    def refresh_assets(self):
        assets = self.controller().refresh_asset_catalog()
        with self._lock:
            if self._stop.is_set():
                raise UnavailableError("Robbi stopped during asset refresh")
            self._asset_revision += 1
            self._snapshot["assets"] = copy.deepcopy(assets)
            return copy.deepcopy(self._snapshot)

    def diagnostics_metrics(self):
        with self._lock:
            sampled_at = self._snapshot_at
        controller = self._controller
        read = getattr(controller, "diagnostics_metrics", None)
        resources = getattr(controller, "resource_metrics", None)
        return {"snapshot_age_s": round(max(0, time.monotonic() - sampled_at), 3) if sampled_at is not None else None,
                "sections": read() if read else {}, "resources": resources() if resources else {}}

    def submit(self, operation, payload):
        if operation not in ("text", "provider_discovery", "extension"):
            raise ValidationError("Unknown job operation")
        if not isinstance(payload, dict):
            raise ValidationError("Job payload must be an object")
        controller = self.controller()
        generation = self._generation
        capture_token = getattr(controller, '_action_token', None)
        action_token = capture_token() if capture_token is not None else None
        if operation == "text":
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 12000:
                raise ValidationError("Enter a message from 1 to 12000 characters")
            photo = payload.get("include_photo", False)
            if not isinstance(photo, bool):
                raise ValidationError("include_photo must be true or false")
            conversation_id = payload.get("conversation_id", "")
            if not isinstance(conversation_id, str) or len(conversation_id) > 128:
                raise ValidationError("conversation_id must be text of at most 128 characters")
            options = {"conversation_id": conversation_id} if conversation_id else {}
            callback = lambda: controller.think_and_respond(text, include_photo=photo, source="ui_text", **options)
        elif operation == "provider_discovery":
            callback = controller.llm.discover_local_runtime
        elif operation == "extension":
            def callback():
                action = self.plugins.run(payload.get("id"), payload.get("text", ""))
                if self._generation != generation:
                    raise UnavailableError("App action cancelled by Stop")
                return controller.execute_action(action)
        else:
            raise ValidationError("Unknown job operation")
        def execute():
            if self._generation != generation:
                raise UnavailableError("Request cancelled by Stop; submit again")
            # Stop may race the handoff after the runtime generation check.
            # Descendants must retain admission's token, never adopt the fresh
            # controller generation when they eventually enter a domain.
            local = getattr(controller, '_action_local', None)
            previous = getattr(local, 'token', None) if local is not None else None
            if local is not None and action_token is not None:
                local.token = action_token
            try:
                return callback()
            finally:
                if local is not None and action_token is not None:
                    local.token = previous
        return self.jobs.submit(operation, execute)

    def emergency_stop(self, *, operator_stop=True):
        self._generation += 1
        issues = []
        conversation = getattr(self, 'conversation_lab', None)
        if conversation is not None:
            try:
                conversation.stop()
            except Exception as error:
                issues.append(f"conversation: {error}")
        manager = getattr(self, "app_manager", None)
        if manager is not None:
            try:
                manager.cancel_all()
            except Exception as error:
                issues.append(f"app management: {error}")
        filming = getattr(self, "filming", None)
        if filming is not None:
            try:
                filming.stop()
            except Exception as error:
                issues.append(f"filming: {error}")
        briefing = getattr(self, "morning_briefing", None)
        if briefing is not None:
            try:
                briefing.cancel()
            except Exception as error:
                issues.append(f"morning briefing: {error}")
        try:
            self.app_host.cancel_all()
        except Exception as error:
            issues.append(f"apps: {error}")
        controller = self._controller
        if controller is not None:
            cancel = getattr(controller, "cancel_actions", lambda: None)
            if not operator_stop:
                cancel = getattr(controller, "cancel_actions_for_shutdown", cancel)
            # Each stop is attempted even when another subsystem is faulty.
            for name, callback in (
                ("actions", cancel),
                ("motion", lambda: controller.motion.stop()),
                ("mapping", lambda: controller.slam.stop()),
                ("audio", lambda: controller.media.stop_audio()),
            ):
                try:
                    callback()
                except Exception as error:
                    issues.append(f"{name}: {error}")
        return {"ok": not issues, "stopped": True, "issues": issues}

    def stop(self):
        with self._lock:
            if self._stop.is_set():
                return
            self._stop.set()
            self._ready.clear()
        # Look up each method inside its own failure boundary. A missing or
        # partially initialized adapter must not prevent even the first stop.
        steps = (
            ("emergency-stop", None, None),
            ("conversation", "conversation_lab", "shutdown"),
            ("jobs", "jobs", "stop"),
            ("morning-briefing", "morning_briefing", "stop"),
            ("briefing-jobs", "briefing_jobs", "stop"),
            ("audio-studio", "audio_studio", "stop"),
            ("filming", "filming", "shutdown"),
            ("apps", "app_host", "stop"),
            ("app-manager", "app_manager", "stop"),
            ("app-management-jobs", "app_management_jobs", "stop"),
            ("controller", "_controller", "stop"),
            ("recording", "recording", "stop"),
        )
        failures = []
        for name, attribute, method in steps:
            try:
                if attribute is None:
                    self.emergency_stop(operator_stop=False)
                else:
                    component = getattr(self, attribute, None)
                    if component is not None:
                        getattr(component, method)()
            except Exception as error:
                # One failed optional adapter must not skip motor/controller
                # cleanup, worker cancellation, or flushing the event sink.
                failures.append(name)
                LOG.exception("Shutdown failed for %s; continuing cleanup", name)
                self.health.set(
                    "shutdown-" + name, "failed",
                    f"Shutdown failed ({type(error).__name__})",
                    "Inspect local logs before restarting this component",
                )
        message = "Runtime stopped"
        if failures:
            message += "; cleanup failed for " + ", ".join(failures)
        self.health.set("runtime", "stopped", message)
