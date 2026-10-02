"""`python -m app` – startet JARVIS mit Host/Port aus config.yaml.

Unter pythonw.exe (Autostart ohne Konsolenfenster) gibt es kein stdout/stderr; dann wird
alles nach state/logs/server.log geschrieben.

Solange der Server läuft, steht in state/server.pid (JSON) die eigene Prozess-ID:
  {"pid": 1234, "executable": "...python.exe", "started": "2026-01-01T12:00:00+00:00",
   "create_time": 1767268800.12}
"executable" ist sys.executable (unter Windows der Pfad in .venv\\Scripts, der echte Prozess
kann dabei der Basis-Interpreter sein). "create_time" (psutil) macht PID + Startzeit eindeutig,
damit Stop-Skripte keinen fremden Prozess mit wiederverwendeter PID erwischen. Die Datei wird
beim Beenden entfernt; nach einem harten Abbruch bleibt sie liegen und gilt dann als veraltet.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.config import PROJECT_DIR, ConfigError, load_config

MAX_LOG_BYTES = 5 * 1024 * 1024
PID_FILE_NAME = "server.pid"
# uvicorn fährt bei diesen Signalen sauber herunter und löst sie danach erneut aus. Mit dem
# Standard-Handler würde der Prozess dann sofort enden – ohne finally/atexit (PID-Datei bliebe liegen).
_STOP_SIGNALS = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGBREAK") if hasattr(signal, name))


def state_dir() -> Path:
    return Path(os.environ.get("JARVIS_STATE", PROJECT_DIR / "state"))


def pid_file_path(directory: Path | None = None) -> Path:
    return (directory or state_dir()) / PID_FILE_NAME


def _redirect_output_if_headless() -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir = state_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "server.log"
    if log_path.exists() and log_path.stat().st_size > MAX_LOG_BYTES:
        log_path.replace(log_path.with_suffix(".log.1"))
    stream = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream


def _process_create_time(pid: int) -> float | None:
    try:
        import psutil

        return psutil.Process(pid).create_time()
    except Exception:
        return None


def read_pid_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
        return None
    return data


def running_instance(path: Path) -> dict | None:
    """Inhalt der PID-Datei, wenn der dort eingetragene Server noch läuft – sonst None.

    "Läuft" heißt: Prozess existiert und hat dieselbe Startzeit wie notiert (sonst wurde die PID
    nach einem Absturz von einem anderen Prozess wiederverwendet).
    """
    data = read_pid_file(path)
    if data is None or data["pid"] == os.getpid():
        return None
    recorded = data.get("create_time")
    if not isinstance(recorded, (int, float)):
        return None
    actual = _process_create_time(data["pid"])
    if actual is None or abs(actual - recorded) > 1.0:
        return None
    return data


def write_pid_file(path: Path) -> dict:
    """Schreibt die PID-Datei atomar (erst .tmp, dann umbenennen) und gibt den Inhalt zurück."""
    data = {
        "pid": os.getpid(),
        "executable": sys.executable,
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "create_time": _process_create_time(os.getpid()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)
    return data


def remove_pid_file(path: Path, pid: int | None = None) -> None:
    """Entfernt die PID-Datei – aber nur, wenn sie zu diesem Prozess (bzw. `pid`) gehört."""
    pid = os.getpid() if pid is None else pid
    data = read_pid_file(path)
    if data is not None and data["pid"] != pid:
        return
    try:
        path.unlink()
    except OSError:  # schon weg oder gesperrt – beim nächsten Start gilt sie als veraltet
        pass


def _exit_on_signal(signum, _frame) -> None:
    raise SystemExit(0)


@contextmanager
def _exit_cleanly_on_stop_signals() -> Iterator[None]:
    """SIGTERM/SIGBREAK → SystemExit, damit finally und atexit laufen. Danach alte Handler zurück."""
    previous = {}
    for sig in _STOP_SIGNALS:
        try:
            previous[sig] = signal.signal(sig, _exit_on_signal)
        except (ValueError, OSError):  # nicht im Haupt-Thread o. Ä.
            pass
    try:
        yield
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def main() -> int:
    _redirect_output_if_headless()
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"\nJARVIS kann nicht starten:\n{exc}\n", file=sys.stderr)
        return 2

    import uvicorn

    pid_path = pid_file_path()
    other = running_instance(pid_path)
    if other is not None:
        print(
            f"\nJARVIS läuft bereits (PID {other['pid']}, gestartet {other.get('started', '?')}).\n"
            "Zum Beenden: powershell -ExecutionPolicy Bypass -File scripts\\stop.ps1\n",
            file=sys.stderr,
        )
        return 1

    def _cleanup() -> None:
        remove_pid_file(pid_path)

    print(f"JARVIS startet auf http://{config.server.bind}:{config.server.port}", flush=True)
    try:
        write_pid_file(pid_path)
    except OSError as exc:
        print(f"Hinweis: PID-Datei {pid_path} konnte nicht geschrieben werden ({exc}).", file=sys.stderr)
    atexit.register(_cleanup)
    try:
        with _exit_cleanly_on_stop_signals():
            uvicorn.run("app.main:app", host=config.server.bind, port=config.server.port, log_level="info")
    except KeyboardInterrupt:  # Strg+C in der Konsole = normales Beenden, kein Stacktrace
        pass
    finally:
        _cleanup()
        atexit.unregister(_cleanup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
