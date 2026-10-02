"""`python -m app` – startet JARVIS mit Host/Port aus config.yaml.

Unter pythonw.exe (Autostart ohne Konsolenfenster) gibt es kein stdout/stderr; dann wird
alles nach state/logs/server.log geschrieben.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app.config import PROJECT_DIR, ConfigError, load_config

MAX_LOG_BYTES = 5 * 1024 * 1024


def _redirect_output_if_headless() -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir = Path(os.environ.get("JARVIS_STATE", PROJECT_DIR / "state")) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "server.log"
    if log_path.exists() and log_path.stat().st_size > MAX_LOG_BYTES:
        log_path.replace(log_path.with_suffix(".log.1"))
    stream = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream


def main() -> int:
    _redirect_output_if_headless()
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"\nJARVIS kann nicht starten:\n{exc}\n", file=sys.stderr)
        return 2

    import uvicorn

    print(f"JARVIS startet auf http://{config.server.bind}:{config.server.port}", flush=True)
    uvicorn.run("app.main:app", host=config.server.bind, port=config.server.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
