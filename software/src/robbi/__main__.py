"""`python -m robbi` starts the isolated version 2 installation."""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal

from .config import load
from .runtime import Runtime
from .web import create_app


def load_secrets(root):
    from .configuration.secrets import load_into_environment
    load_into_environment(root / ".env.secrets")


def main():
    parser = argparse.ArgumentParser(description="Robbi version 2")
    parser.add_argument("--root", type=Path, help="v2 project directory")
    parser.add_argument("--config", type=Path, help="Robot configuration (default config/robot.yaml)")
    parser.add_argument("--data-dir", type=Path, help="isolated state directory")
    parser.add_argument("--host", help="bind address (default loopback)")
    parser.add_argument("--port", type=int, help="HTTP port (default 8082)")
    parser.add_argument("--check", action="store_true", help="validate config without initializing hardware")
    args = parser.parse_args()
    if not args.check:
        selected_root = Path(args.root or os.getenv("ROBBI_ROOT") or Path(__file__).resolve().parents[2]).resolve()
        load_secrets(selected_root)
    config = load(args.root, args.config, args.data_dir)
    if args.host:
        config.values["server"]["host"] = args.host
    if args.port is not None:
        config.values["server"]["port"] = args.port
    from .config import validate
    validate(config.values)
    if args.check:
        print(json.dumps({"valid": not config.issues, "issues": config.issues, "root": str(config.root), "config": str(config.source)}, indent=2))
        return 1 if config.issues else 0
    if config.values.get("server", {}).get("auth_enabled", False) and not os.getenv("ROBBI_WEB_TOKEN"):
        parser.error("Dashboard authentication is enabled: set ROBBI_WEB_TOKEN in .env.secrets or set server.auth_enabled: false")
    config.install_environment()
    # Small DSP/image tasks must not start a large native compute pool on the Pi.
    for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS"):
        os.environ.setdefault(variable, "1")
    log_dir = config.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), RotatingFileHandler(log_dir / "runtime.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")])
    from .observability import start_trace_logging
    trace_listener = start_trace_logging(log_dir)
    runtime = Runtime(config)
    application = create_app(runtime)
    from waitress import create_server
    server = create_server(application, host=config.host, port=config.port,
                           threads=int(config.values.get("runtime", {}).get("worker_threads", 4)),
                           connection_limit=32, channel_timeout=30, max_request_body_size=32 * 1024 * 1024)
    def shutdown(*_):
        server.close()
        runtime.stop()
        raise SystemExit(0)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    runtime.start()
    logging.info("Robbi v2 dashboard: http://%s:%s", config.host, config.port)
    try:
        server.run()
    finally:
        runtime.stop()
        server.close()
        logging.getLogger("robot.trace").handlers.clear()
        trace_listener.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
