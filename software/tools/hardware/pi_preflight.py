"""Read-only deployment checks; no robot imports, devices, network or state writes.

Runs with the standard library; the optional installed PyYAML parser enables
profile inspection. Package discovery never imports drivers. Firmware querying
is opt-in and is restricted to one timeout-bounded vcgencmd get_throttled call.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys


MIB = 1024 * 1024
THREAD_VARIABLES = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS")
CORE_MODULES = ("flask", "waitress", "yaml", "PIL")
THROTTLE_FLAGS = {0: "undervoltage_now", 1: "frequency_capped_now",
                  2: "throttled_now", 3: "soft_temperature_limit_now",
                  16: "undervoltage_since_boot", 17: "frequency_capped_since_boot",
                  18: "throttled_since_boot", 19: "soft_temperature_limit_since_boot"}


def read_text(path, limit=65536):
    """Bound input from proc/sys and config files without relying on stat size."""
    with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("input exceeds size limit")
    return value


def optional_text(path):
    try:
        return read_text(path).strip("\x00\r\n ")
    except (OSError, ValueError):
        return None


def module_available(name):
    # Dotted find_spec calls can import parent packages. Accept root names only.
    if not name.isidentifier():
        raise ValueError("only top-level module names are allowed")
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, OSError):
        return False


def parse_meminfo(text):
    values = {}
    for line in text.splitlines():
        match = re.fullmatch(r"(MemTotal|MemAvailable|SwapTotal|SwapFree):\s+(\d+)\s+kB", line)
        if match:
            values[match[1]] = int(match[2]) * 1024
    return values


def memory_info():
    if sys.platform.startswith("linux"):
        return parse_meminfo(optional_text("/proc/meminfo") or "")
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong) for name in
                ("total", "available", "pagefile", "free_pagefile", "virtual", "free_virtual", "extended")]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {"MemTotal": status.total, "MemAvailable": status.available}
    return {}


def parse_throttled(text):
    match = re.fullmatch(r"\s*throttled=(0x[0-9a-fA-F]{1,8})\s*", text)
    if not match:
        raise ValueError("firmware returned an unrecognized throttle response")
    value = int(match[1], 16)
    return {"raw": hex(value), "flags": [name for bit, name in THROTTLE_FLAGS.items() if value & (1 << bit)]}


def firmware_info(enabled, model, runner=subprocess.run, which=shutil.which):
    if not enabled:
        return {"state": "skipped", "detail": "Use --firmware for a read-only firmware query."}
    if not sys.platform.startswith("linux") or "Raspberry Pi" not in (model or ""):
        return {"state": "skipped", "detail": "Firmware query requires a detected Raspberry Pi."}
    executable = which("vcgencmd")
    if not executable:
        return {"state": "warn", "detail": "vcgencmd is not installed or is not on PATH."}
    try:
        result = runner([executable, "get_throttled"], stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        text=True, timeout=2, check=False, shell=False)
        if result.returncode:
            return {"state": "warn", "detail": "Firmware query failed; check the service user's permissions."}
        throttle = parse_throttled(result.stdout[:256])
        return {"state": "warn" if throttle["flags"] else "pass", **throttle}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"state": "warn", "detail": "Firmware query timed out or returned an invalid result."}


def inspect_profile(root, selected):
    """Read a bounded two-level config without importing the robot/config runtime."""
    if not module_available("yaml"):
        return None, "PyYAML is missing; profile inspection unavailable. Install core dependencies."
    import yaml
    merged = {}
    sources = [root / "config" / "desktop.yaml", selected]
    try:
        for path in dict.fromkeys(sources):
            value = yaml.safe_load(read_text(path, 256 * 1024)) or {}
            if not isinstance(value, dict) or len(value) > 200:
                raise ValueError("profile must contain a bounded YAML mapping")
            for key, group in value.items():
                if not isinstance(key, str) or not isinstance(group, dict) or len(group) > 200:
                    raise ValueError("profile groups must be bounded mappings")
                # Inspect only scalar two-level settings. Extensions may contain
                # nested lists/maps; don't traverse them or echo private content.
                merged.setdefault(key, {}).update({k: v for k, v in group.items()
                    if isinstance(k, str) and (v is None or type(v) in (str, int, float, bool))})
        return merged, None
    except (OSError, ValueError, RecursionError, yaml.YAMLError):
        return None, "Cannot read a valid profile. Run python -m robbi --config PATH --check for full validation."


def nearest_existing(path):
    path = path.resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def collect(root, config, data_dir, firmware=False):
    model = optional_text("/proc/device-tree/model") if sys.platform.startswith("linux") else None
    os_release = optional_text("/etc/os-release") if sys.platform.startswith("linux") else None
    os_name = platform.system()
    if os_release:
        os_name = next((line.partition("=")[2].strip('"') for line in os_release.splitlines()
                        if line.startswith("PRETTY_NAME=")), os_name)
    disk_path = nearest_existing(data_dir)
    try:
        disk = {"path": str(disk_path), "free_bytes": shutil.disk_usage(disk_path).free}
    except OSError:
        disk = {"path": str(disk_path), "free_bytes": None}
    temperature = optional_text("/sys/class/thermal/thermal_zone0/temp") if model else None
    try:
        temperature = float(temperature) / 1000 if temperature else None
        if temperature is not None and (not math.isfinite(temperature) or not -20 < temperature < 150):
            temperature = None
    except ValueError:
        temperature = None
    profile, profile_error = inspect_profile(root, config)
    modules = set(CORE_MODULES) | {"numpy", "scipy", "cv2", "picamera2", "lgpio", "spidev", "st7789",
        "adafruit_vl53l1x", "adafruit_extended_bus", "adafruit_lsm6ds", "python_speech_features", "openai", "httpx"}
    return {"host": {"os": os_name, "platform": sys.platform, "machine": platform.machine(),
                     "userspace_bits": struct.calcsize("P") * 8, "python": list(sys.version_info[:3]),
                     "cpu_count": os.cpu_count(), "model": model},
            "memory": memory_info(), "disk": disk, "temperature_c": temperature,
            "firmware": firmware_info(firmware, model),
            "environment": {key: os.getenv(key) for key in THREAD_VARIABLES},
            "modules": {name: module_available(name) for name in sorted(modules)},
            "commands": {name: shutil.which(name) is not None for name in ("aplay", "arecord")},
            "profile": profile, "profile_error": profile_error, "config": str(config)}


def evaluate(snapshot, require_pi=False):
    checks = []
    def add(name, state, detail):
        checks.append({"name": name, "state": state, "detail": detail})
    host = snapshot["host"]
    version = tuple(host["python"][:2])
    add("python", "pass" if (3, 11) <= version < (3, 13) else "fail",
        f"Python {'.'.join(map(str, host['python']))}; supported runtime: 3.11 or 3.12 (audioop is required).")
    is_target = "Raspberry Pi Zero 2" in (host.get("model") or "") and host["platform"].startswith("linux")
    add("target", "pass" if is_target else ("fail" if require_pi else "info"),
        "Pi Zero 2 detected; physical behavior remains unverified." if is_target else "Development/other host; this cannot certify Pi Zero 2 W performance.")
    if host["userspace_bits"] == 32:
        add("architecture", "warn", "32-bit userspace: verify compatible prebuilt packages; do not compile large native dependencies on this board.")
    else:
        add("architecture", "pass", f"{host['userspace_bits']}-bit userspace ({host['machine']}).")
    available = snapshot["memory"].get("MemAvailable")
    if available is None:
        add("memory", "warn", "Available RAM could not be read.")
    else:
        add("memory", "warn" if available < 96 * MIB else "pass",
            f"Available RAM: {available / MIB:.1f} MiB; investigate below 96 MiB before combined workloads.")
    free = snapshot["disk"].get("free_bytes")
    add("disk", "warn" if free is None or free < 512 * MIB else "pass",
        "Free storage unavailable." if free is None else f"Free storage: {free / MIB:.1f} MiB; keep at least 512 MiB spare for logs, state and updates.")
    for name in CORE_MODULES:
        add(f"package.{name}", "pass" if snapshot["modules"].get(name) else "fail", "Discoverable (not imported or ABI-tested)." if snapshot["modules"].get(name) else "Required core dependency missing from this interpreter.")
    for key, value in snapshot["environment"].items():
        add(f"native.{key}", "pass" if value == "1" else "warn",
            "One native worker." if value == "1" else "Set to 1 in the launch environment before importing NumPy/OpenCV; service already sets this.")
    profile = snapshot.get("profile")
    if profile is None:
        add("profile", "warn", snapshot.get("profile_error") or "Profile unavailable.")
    else:
        add("profile", "info", "Profile read; run robbi --check for complete schema validation. Only safe budget values are included here.")
        def enabled(group, key="enabled"):
            return profile.get(group, {}).get(key) is True
        needed = set()
        if enabled("camera"):
            needed |= {"numpy", "cv2", "picamera2"}
        if enabled("voice"):
            needed |= {"numpy"}
            if profile.get("voice", {}).get("wake_engine", "local_wake") == "local_wake":
                needed |= {"python_speech_features", "scipy"}
        if enabled("hardware", "enable_lcd"):
            needed |= {"st7789", "spidev"}
        if enabled("hardware", "enable_motors") or enabled("tof"):
            needed.add("lgpio")
        if enabled("tof"):
            needed |= {"adafruit_vl53l1x", "adafruit_extended_bus"}
        if enabled("imu"):
            needed |= {"adafruit_lsm6ds", "adafruit_extended_bus"}
        for name in sorted(needed):
            add(f"optional.{name}", "pass" if snapshot["modules"].get(name) else "warn",
                "Discoverable; device/ABI not tested." if snapshot["modules"].get(name) else "Configured feature dependency missing; runtime should report a degraded component and still boot.")
        for name in ("aplay", "arecord"):
            if name == "aplay" or enabled("voice"):
                add(f"command.{name}", "pass" if snapshot["commands"].get(name) else "warn", "Present; no audio device opened." if snapshot["commands"].get(name) else "ALSA command missing; install alsa-utils on Pi.")
        motor_enabled = enabled("hardware", "enable_motors")
        calibrated = enabled("runtime", "calibration_confirmed")
        add("motors", "fail" if motor_enabled and not calibrated else ("warn" if motor_enabled else "pass"),
            "Motor output is configured on; preflight cannot verify wiring or calibration." if motor_enabled else "Motor output disabled.")
        budgets = {"runtime.worker_threads": (4, "More than four HTTP workers consume extra stacks; preserve reserved Stop capacity."),
                   "runtime.feature_task_limit": (16, "More outstanding feature tasks can retain extra audio/provider payloads."),
                   "runtime.command_queue_size": (2, "Large job queues increase stale work and memory pressure."),
                   "voice.local_wake_max_references": (8, "Additional wake templates increase matching CPU cost."),
                   "voice.local_wake_max_total_feature_frames": (1600, "Additional wake feature frames increase matching CPU and memory cost."),
                   "camera.fps": (3, "Camera rate exceeds the conservative Pi profile; measure combined voice/video load."),
                   "camera.width": (640, "Camera width exceeds the conservative Pi profile."),
                   "camera.height": (480, "Camera height exceeds the conservative Pi profile.")}
        for name, (maximum, message) in budgets.items():
            group, key = name.split(".")
            value = profile.get(group, {}).get(key)
            if value is not None:
                valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
                add(f"budget.{name}", "fail" if not valid else ("warn" if value > maximum else "pass"),
                    "Invalid numeric budget; run robbi --check." if not valid else (message if value > maximum else f"Configured: {value}."))
    temperature = snapshot.get("temperature_c")
    add("temperature", "info" if temperature is None else ("warn" if temperature >= 75 else "pass"),
        "Not available on this host." if temperature is None else f"{temperature:.1f} C; investigate cooling/load at 75 C or above.")
    firmware = snapshot["firmware"]
    add("firmware", firmware["state"], firmware.get("detail") or f"Throttle flags: {', '.join(firmware.get('flags', [])) or 'none'}.")
    counts = {state: sum(check["state"] == state for check in checks) for state in ("pass", "warn", "fail", "info", "skipped")}
    # Do not expose arbitrary profile values (provider URLs, prompts, future secrets).
    details = {key: value for key, value in snapshot.items() if key not in {"profile", "profile_error"}}
    return {"schema_version": 1, "hardware_verified": False, "checks": checks, "counts": counts,
            "status": "fail" if counts["fail"] else ("warn" if counts["warn"] else "pass"), "details": details}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--config", type=Path, default=Path("config/pi.yaml"))
    parser.add_argument("--data-dir", type=Path, default=Path("var"))
    parser.add_argument("--firmware", action="store_true", help="Query vcgencmd get_throttled with a two-second timeout")
    parser.add_argument("--require-pi", action="store_true", help="Fail if this is not a Pi Zero 2")
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable report")
    args = parser.parse_args(argv)
    sys.dont_write_bytecode = True
    root = args.root.resolve()
    config = args.config if args.config.is_absolute() else root / args.config
    data_dir = args.data_dir if args.data_dir.is_absolute() else root / args.data_dir
    report = evaluate(collect(root, config, data_dir, args.firmware), args.require_pi)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for check in report["checks"]:
            print(f"{check['state'].upper():7} {check['name']}: {check['detail']}")
        print(f"\nResult: {report['status']}. Read-only checks; hardware and real-time behavior are unverified.")
    return 1 if report["counts"]["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
