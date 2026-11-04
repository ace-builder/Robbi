"""OS isolation for imported apps, with a narrow broker to the existing SDK.

Bundled first-party projects retain the lightweight in-process path. Imported
code is never imported here, even when permissions are disabled for debugging.
"""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys

from robbi.errors import UnavailableError, ValidationError
from .sandbox_wire import MAX_FRAME, encode, unpack

CONTEXT_METHODS = {
    "checkpoint",
    "wait",
    "require",
    "request_permissions",
    "read_state",
    "write_state",
    "save_image",
    "call_app",
    "note_non_undoable",
}
ROBOT_METHODS = {
    "status",
    "speak",
    "show_image",
    "capture_image",
    "camera_snapshot",
    "listen_claps",
    "gesture",
    "face_animation",
    "timer",
    "move",
    "tof",
    "imu",
    "assets",
    "play_sound",
    "play_sound_file",
    "show_face",
    "play_video",
    "display_text",
    "display_card",
}
SERVICES = {
    "images": {"generate"},
    "weather": {"forecast"},
    "search": {"search"},
    "maps": {"geocode", "route"},
    "google": {
        "calendar_events",
        "calendar_create",
        "gmail_search",
        "gmail_read",
        "gmail_draft",
    },
}


def availability():
    if not sys.platform.startswith("linux") or not shutil.which("bwrap"):
        return {
            "available": False,
            "message": "Imported apps require Linux bubblewrap; run the updated installer (or install bubblewrap and libseccomp2).",
        }
    return {
        "available": True,
        "message": "Imported apps use isolated processes; kernel namespace support is checked on launch.",
    }


def command(project):
    status = availability()
    if not status["available"]:
        raise UnavailableError(status["message"])
    args = [
        shutil.which("bwrap"),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "PATH",
        "/usr/bin",
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
        "--setenv",
        "OPENBLAS_NUM_THREADS",
        "1",
        "--setenv",
        "OMP_NUM_THREADS",
        "1",
    ]
    # Standard software only; never mount /home, /etc, /run, /sys or robot state.
    for path in ("/usr", "/lib", "/lib64"):
        if Path(path).exists():
            args += ["--ro-bind", path, path]
    # Python dependencies, not the venv root or any environment files.
    if sys.prefix != sys.base_prefix:
        lib = Path(sys.prefix) / "lib"
        args += ["--ro-bind", str(lib), "/deps"]
    args += [
        "--ro-bind",
        str(Path(__file__).resolve().parents[2]),
        "/runtime",
        "--ro-bind",
        str(project.root.resolve()),
        "/app",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--size",
        str(16 * 1024 * 1024),
        "--tmpfs",
        "/tmp",
        "--remount-ro",
        "/dev",
        "--remount-ro",
        "/",
        "--chdir",
        "/app",
        "/usr/bin/python3",
        "-I",
        "-B",
        "-c",
        "import sys,glob;sys.path[:0]=['/runtime']+glob.glob('/deps/python*/site-packages');"
        "from robbi.app_runtime.sandbox_worker import main;main()",
    ]
    return args


def worker_config(config):
    """Credential values stay in the broker; environment variable names are safe."""
    secrets = {
        "api_key",
        "client_secret",
        "refresh_token",
        "access_token",
        "password",
        "token",
    }
    return {
        key: value for key, value in config.items() if key.casefold() not in secrets
    }


def provider_config(context, service):
    # App code may not redirect a credential to a new endpoint or choose a
    # different environment variable. Provider configuration is owner-managed.
    config = {k: v for k, v in context._provider_config.items() if v != ""}
    if service == "images":
        for name in ("request_options", "request_template"):
            raw = config.pop(name + "_json", "")
            if raw:
                config[name] = json.loads(raw)
    return config


class Broker:
    def __init__(self, context):
        self.context = context
        self.sessions = ExitStack()
        self.session_active = False

    def dispatch(self, message):
        self.context.checkpoint()
        if not isinstance(message, dict) or set(message) != {
            "method",
            "args",
            "kwargs",
        }:
            raise ValidationError("Invalid app broker request")
        method, args, kwargs = message["method"], message["args"], message["kwargs"]
        if (
            not isinstance(method, str)
            or not isinstance(args, list)
            or not isinstance(kwargs, dict)
        ):
            raise ValidationError("Invalid app broker arguments")
        if (
            len(args) > 16
            or len(kwargs) > 24
            or any(not isinstance(k, str) or k.startswith("_") for k in kwargs)
        ):
            raise ValidationError("Invalid app broker arguments")
        if any(k in kwargs for k in ("cancellation", "deadline")):
            raise ValidationError("App cannot override cancellation or deadlines")
        if method == "session.begin":
            if self.session_active:
                raise ValidationError("Nested robot sessions are not supported")
            self.sessions.enter_context(self.context.robot.session(*args, **kwargs))
            self.session_active = True
            return None
        if method == "session.end":
            self.sessions.close()
            self.session_active = False
            return None
        parts = method.split(".")
        if len(parts) == 2 and parts[0] == "context" and parts[1] in CONTEXT_METHODS:
            target = getattr(self.context, parts[1])
        elif len(parts) == 2 and parts[0] == "robot" and parts[1] in ROBOT_METHODS:
            target = getattr(self.context.robot, parts[1])
        elif (
            len(parts) == 3
            and parts[0] == "services"
            and parts[2] in SERVICES.get(parts[1], ())
        ):
            if parts[1] == "google":
                self.context.require(
                    "account.gmail"
                    if parts[2].startswith("gmail_")
                    else "account.calendar"
                )
            if not args or not isinstance(args[0], dict):
                raise ValidationError("Provider configuration is required")
            args[0] = provider_config(self.context, parts[1])
            target = getattr(getattr(self.context.services, parts[1]), parts[2])
        else:
            raise ValidationError("Unsupported sandbox SDK method")
        return target(*args, **kwargs)


def run(project, context, tool, arguments):
    """No preexec_fn: the robot has threads; restrictions run inside the worker."""
    proc = subprocess.Popen(
        command(project),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin"},
        close_fds=True,
        start_new_session=True,
    )
    broker = Broker(context)
    buffer = bytearray()
    os.set_blocking(proc.stdin.fileno(), False)
    os.set_blocking(proc.stdout.fileno(), False)

    def send(value):
        data = memoryview(encode(value))
        while data:
            context.checkpoint()
            if proc.poll() is not None:
                raise UnavailableError(
                    "App sandbox could not start; check bubblewrap, libseccomp2 and kernel user namespaces"
                )
            if select.select([], [proc.stdin], [], 0.025)[1]:
                try:
                    data = data[os.write(proc.stdin.fileno(), data) :]
                except BlockingIOError:
                    pass

    try:
        send(
            {
                "app_id": project.id,
                "config": worker_config(context.config),
                "permissions": sorted(context.permissions),
                "entrypoint": project.manifest["entrypoint"],
                "tool": tool,
                "arguments": arguments,
                "deadline": context.deadline,
            }
        )
        calls = 0
        while True:
            context.checkpoint()
            if b"\n" not in buffer:
                if not select.select([proc.stdout], [], [], 0.025)[0]:
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    raise UnavailableError(
                        "App sandbox exited; verify namespace support and app resource limits"
                    )
                buffer.extend(chunk)
                if len(buffer) > MAX_FRAME:
                    raise ValidationError("App sandbox output exceeds limit")
                continue
            line, _, rest = buffer.partition(b"\n")
            buffer = bytearray(rest)
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValidationError("Invalid sandbox response")
            if set(message) == {"result"}:
                return message["result"]
            if set(message) == {"error"}:
                raise UnavailableError(
                    "App failed inside isolation; check its SDK compatibility and inputs"
                )
            calls += 1
            if calls > 128:
                raise ValidationError("App exceeds 128 SDK calls per invocation")
            # Permission errors and broker failures terminate the worker and
            # retain their structured metadata in the existing AppHost handler.
            result = broker.dispatch(unpack(message))
            send({"value": result})
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)
        proc.stdin.close()
        proc.stdout.close()
        broker.sessions.close()
