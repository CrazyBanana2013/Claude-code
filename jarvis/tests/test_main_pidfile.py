"""PID-Datei state/server.pid von `python -m app` (app/__main__.py)."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime

import httpx
import psutil
import pytest
import uvicorn

from app import __main__ as server_main
from app.config import PROJECT_DIR


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    shutil.copy(PROJECT_DIR / "config.example.yaml", config)
    monkeypatch.setenv("JARVIS_CONFIG", str(config))
    monkeypatch.setenv("JARVIS_STATE", str(tmp_path / "state"))
    return tmp_path / "state" / "server.pid"


def test_pid_file_path_follows_jarvis_state(env):
    assert server_main.pid_file_path() == env


def test_write_and_remove(tmp_path):
    path = tmp_path / "state" / "server.pid"
    written = server_main.write_pid_file(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == written
    assert data["pid"] == os.getpid()
    assert data["executable"] == sys.executable
    assert datetime.fromisoformat(data["started"]).tzinfo is not None
    assert abs(data["create_time"] - psutil.Process().create_time()) < 1
    assert [p.name for p in path.parent.iterdir()] == ["server.pid"]  # keine .tmp-Reste
    server_main.remove_pid_file(path)
    assert not path.exists()
    server_main.remove_pid_file(path)  # zweimal entfernen ist kein Fehler


def test_remove_keeps_file_of_other_process(tmp_path):
    path = tmp_path / "server.pid"
    path.write_text(json.dumps({"pid": os.getpid() + 100000, "executable": "x", "started": "x"}))
    server_main.remove_pid_file(path)
    assert path.exists()


def test_main_writes_pid_during_run_and_removes_it(env, monkeypatch):
    seen = {}

    def fake_run(app, **kwargs):
        seen["data"] = json.loads(env.read_text(encoding="utf-8"))
        seen["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert server_main.main() == 0
    assert seen["data"]["pid"] == os.getpid()
    assert seen["data"]["executable"] == sys.executable
    assert seen["kwargs"]["port"] == 8765
    # X-Forwarded-For (tailscale serve) nie übernehmen – IP-Filter und Sperre sehen die echte TCP-Quelle.
    assert seen["kwargs"]["proxy_headers"] is False
    assert not env.exists()


def test_main_removes_pid_when_server_crashes(env, monkeypatch):
    def failing_run(app, **kwargs):
        assert env.exists()
        raise SystemExit(1)  # z. B. Port belegt

    monkeypatch.setattr(uvicorn, "run", failing_run)
    with pytest.raises(SystemExit):
        server_main.main()
    assert not env.exists()


def test_config_error_writes_no_pid(env, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CONFIG", str(tmp_path / "fehlt.yaml"))
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("darf nicht starten"))
    assert server_main.main() == 2
    assert not env.exists()


def test_ansi_config_exits_2_without_traceback(env, monkeypatch, capsys):
    """config.yaml als ANSI gespeichert: Klartext und Exitcode 2 wie bei anderen Config-Fehlern."""
    config = env.parent.parent / "config.yaml"
    config.write_bytes(config.read_text(encoding="utf-8").encode("cp1252", errors="replace"))
    assert "–".encode("cp1252") in config.read_bytes()  # wirklich kein UTF-8 mehr
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("darf nicht starten"))
    assert server_main.main() == 2
    err = capsys.readouterr().err
    assert "nicht UTF-8-kodiert" in err and "Traceback" not in err
    assert not env.exists()


def test_refuses_second_instance(env, monkeypatch, capsys):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        env.parent.mkdir(parents=True)
        record = {"pid": other.pid, "executable": sys.executable, "started": "2026-01-01T00:00:00+00:00",
                  "create_time": psutil.Process(other.pid).create_time()}
        env.write_text(json.dumps(record))
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("zweite Instanz darf nicht starten"))
        assert server_main.main() == 1
        err = capsys.readouterr().err
        assert "läuft bereits" in err
        # Hinweis mit absolutem Pfad (läuft auch aus einem beliebigen Arbeitsordner) und Startmenü-Weg.
        assert str(PROJECT_DIR / "scripts" / "stop.ps1") in err and "Startmenü > JARVIS > JARVIS beenden" in err
        assert json.loads(env.read_text()) == record  # Datei der laufenden Instanz bleibt
    finally:
        other.kill()
        other.wait()


@pytest.mark.parametrize("record", [
    {"pid": 999_999_999, "create_time": 1.0},     # Prozess gibt es nicht mehr
    {"pid": os.getppid(), "create_time": 1.0},    # PID wiederverwendet (andere Startzeit)
    {"pid": os.getppid()},                        # alte Datei ohne create_time
    "kaputt",
])
def test_stale_pid_file_is_replaced(env, monkeypatch, record):
    env.parent.mkdir(parents=True)
    env.write_text(record if isinstance(record, str) else json.dumps(record))
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: seen.update(json.loads(env.read_text())))
    assert server_main.main() == 0
    assert seen["pid"] == os.getpid()
    assert not env.exists()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_signal_handlers_are_restored(env, monkeypatch):
    before = signal.getsignal(signal.SIGTERM)
    inside = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: inside.update(handler=signal.getsignal(signal.SIGTERM)))
    assert server_main.main() == 0
    assert inside["handler"] is server_main._exit_on_signal
    assert signal.getsignal(signal.SIGTERM) is before


def test_keyboard_interrupt_is_a_normal_stop(env, monkeypatch):
    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(uvicorn, "run", interrupted)
    assert server_main.main() == 0
    assert not env.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Signal-Test nur unter POSIX")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_real_server_writes_and_removes_pid(tmp_path, sig):
    port = _free_port()
    config = tmp_path / "config.yaml"
    text = (PROJECT_DIR / "config.example.yaml").read_text(encoding="utf-8")
    config.write_text(text.replace('bind: "0.0.0.0"', 'bind: "127.0.0.1"').replace("port: 8765", f"port: {port}"),
                      encoding="utf-8")
    state = tmp_path / "state"
    env = dict(os.environ, JARVIS_CONFIG=str(config), JARVIS_STATE=str(state),
               JARVIS_SECRETS=str(tmp_path / "secrets.yaml"))
    log = (tmp_path / "server.log").open("w+", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=PROJECT_DIR, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("Server ist nicht gestartet")
        data = json.loads((state / "server.pid").read_text(encoding="utf-8"))
        assert data["pid"] == proc.pid
        assert data["executable"] == sys.executable
        proc.send_signal(sig)
        assert proc.wait(timeout=20) == 0
        assert not (state / "server.pid").exists()
        log.seek(0)
        output = log.read()
        assert "Traceback" not in output and "Finished server process" in output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        log.close()
