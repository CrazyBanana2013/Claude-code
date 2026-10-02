import json
import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from app.tools.scripts import ScriptManager
from tests.conftest import client_for

pytestmark = pytest.mark.anyio

DUMMY = str(Path(__file__).with_name("dummy_script.py"))


def scripts_cfg(tmp_path, *extra_args, single=True):
    return {"scripts": [
        {"id": "dummy", "label": "Dummy", "cwd": str(tmp_path),
         "command": [sys.executable, DUMMY, str(tmp_path / "pid.txt"), *extra_args],
         "single_instance": single},
        {"id": "cs2", "label": "CS2-Skript", "cwd": "TODO_ORDNER", "command": ["TODO_PROGRAMM"]},
    ]}


def wait_for(path: Path, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if path.exists() and path.read_text():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError("Dummy-Skript ist nicht gestartet")


async def test_start_status_stop(factory, tmp_path):
    app, token = factory(scripts_cfg(tmp_path))
    async with client_for(app, token=token) as c:
        r = await c.post("/api/tools/scripts_start", json={"script_id": "dummy"})
        assert r.status_code == 200, r.text
        res = r.json()["result"]
        assert res["running"] and res["pid"]
        pid = wait_for(tmp_path / "pid.txt")
        assert pid == res["pid"]

        r = await c.post("/api/tools/scripts_start", json={"script_id": "dummy"})
        assert r.status_code == 400 and "läuft bereits" in r.json()["error"]

        listing = (await c.post("/api/tools/scripts_list")).json()["result"]["scripts"]
        assert {s["id"]: s["running"] for s in listing} == {"dummy": True, "cs2": False}

        r = await c.post("/api/tools/scripts_stop", json={"script_id": "dummy"})
        assert r.json()["result"]["status"] == "stopped"
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        status = (await c.post("/api/tools/scripts_status", json={"script_id": "dummy"})).json()["result"]
        assert status["running"] is False


async def test_status_survives_server_restart(factory, tmp_path):
    app, token = factory(scripts_cfg(tmp_path))
    async with client_for(app, token=token) as c:
        await c.post("/api/tools/scripts_start", json={"script_id": "dummy"})
    wait_for(tmp_path / "pid.txt")
    app2, token2 = factory(scripts_cfg(tmp_path))  # neue App-Instanz, gleicher state-Ordner
    async with client_for(app2, token=token2) as c:
        status = (await c.post("/api/tools/scripts_status", json={"script_id": "dummy"})).json()["result"]
        assert status["running"] is True
        r = await c.post("/api/tools/scripts_stop", json={"script_id": "dummy"})
        assert r.json()["result"]["running"] is False


async def test_hard_kill_after_grace(factory, tmp_path):
    app, token = factory(scripts_cfg(tmp_path, "--ignore-term"))
    app.state.registry.ctx.extras["scripts"] = ScriptManager(tmp_path / "state", grace=0.5)
    async with client_for(app, token=token) as c:
        await c.post("/api/tools/scripts_start", json={"script_id": "dummy"})
        pid = wait_for(tmp_path / "pid.txt")
        r = await c.post("/api/tools/scripts_stop", json={"script_id": "dummy"})
    assert r.json()["result"]["status"] == "killed"
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


async def test_todo_script_gives_clear_error(factory, tmp_path):
    app, token = factory(scripts_cfg(tmp_path))
    async with client_for(app, token=token) as c:
        r = await c.post("/api/tools/scripts_start", json={"script_id": "cs2"})
    assert r.status_code == 400
    assert "noch nicht eingerichtet" in r.json()["error"]


async def test_unknown_script_and_missing_program(factory, tmp_path):
    cfg = scripts_cfg(tmp_path)
    cfg["scripts"][0]["command"] = ["programm-das-es-nicht-gibt-123"]
    app, token = factory(cfg)
    async with client_for(app, token=token) as c:
        r = await c.post("/api/tools/scripts_start", json={"script_id": "rm -rf"})
        assert r.status_code == 400 and "Unbekanntes Skript" in r.json()["error"]
        r = await c.post("/api/tools/scripts_start", json={"script_id": "dummy"})
        assert r.status_code == 400 and "nicht gefunden" in r.json()["error"]


async def test_stop_not_running(factory, tmp_path):
    app, token = factory(scripts_cfg(tmp_path))
    async with client_for(app, token=token) as c:
        r = await c.post("/api/tools/scripts_stop", json={"script_id": "dummy"})
    assert r.json()["result"]["status"] == "not_running"


def test_reused_pid_is_not_running(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    # PID existiert (dieser Prozess), aber Startzeit passt nicht → fremder Prozess
    (state / "scripts.json").write_text(json.dumps({"dummy": {"pid": os.getpid(), "create_time": 1.0}}))
    assert ScriptManager(state).process("dummy") is None
