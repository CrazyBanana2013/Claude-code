import subprocess

import pytest

from app.tools import pc
from tests.conftest import client_for

pytestmark = pytest.mark.anyio


@pytest.fixture
def calls(monkeypatch):
    """subprocess.run mocken: zeichnet jeden Prozessstart auf, startet nie etwas."""
    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(pc.subprocess, "run", fake_run)
    monkeypatch.setattr(pc, "is_windows", lambda: True)
    return recorded


async def test_shutdown_tool_only_requests_confirmation(factory, calls):
    app, token = factory()
    async with client_for(app, token=token) as c:
        r = await c.post("/api/tools/pc_shutdown")
    body = r.json()["result"]
    assert body["status"] == "confirm_required"
    assert body["expires_in"] == 30
    assert body["confirm_id"]
    assert calls == []


async def test_confirm_runs_shutdown_once(factory, calls):
    app, token = factory()
    async with client_for(app, token=token) as c:
        cid = (await c.post("/api/tools/pc_shutdown")).json()["result"]["confirm_id"]
        r = await c.post(f"/api/confirm/{cid}")
        assert r.status_code == 200
        assert calls == [["shutdown", "/s", "/t", "15", "/c", "JARVIS"]]
        # Einmalig verwendbar
        r2 = await c.post(f"/api/confirm/{cid}")
        assert r2.status_code == 400
    assert len(calls) == 1


async def test_invalid_confirm_never_starts_process(factory, calls):
    app, token = factory()
    async with client_for(app, token=token) as c:
        await c.post("/api/tools/pc_shutdown")
        r = await c.post("/api/confirm/erfunden")
    assert r.status_code == 400
    assert calls == []


async def test_confirm_without_token_never_starts_process(factory, calls):
    app, token = factory()
    async with client_for(app, token=token) as c:
        cid = (await c.post("/api/tools/pc_shutdown")).json()["result"]["confirm_id"]
    async with client_for(app) as anon:
        assert (await anon.post(f"/api/confirm/{cid}")).status_code == 401
    assert calls == []


def test_confirm_expires():
    now = [0.0]
    store = pc.ConfirmStore(ttl=30, clock=lambda: now[0])
    cid = store.create("pc_shutdown")
    now[0] = 31
    assert store.consume(cid) is None


async def test_expired_confirm_via_api(factory, calls, monkeypatch):
    app, token = factory()
    now = [0.0]
    app.state.registry.ctx.extras["confirm"] = pc.ConfirmStore(clock=lambda: now[0])
    async with client_for(app, token=token) as c:
        cid = (await c.post("/api/tools/pc_shutdown")).json()["result"]["confirm_id"]
        now[0] = 30.5
        assert (await c.post(f"/api/confirm/{cid}")).status_code == 400
    assert calls == []


async def test_cancel_runs_shutdown_abort(factory, calls):
    app, token = factory()
    async with client_for(app, token=token) as c:
        r = await c.post("/api/tools/pc_shutdown_cancel")
    assert r.json()["result"]["status"] == "cancelled"
    assert calls == [["shutdown", "/a"]]


async def test_non_windows_refuses(factory, monkeypatch):
    started = []
    monkeypatch.setattr(pc.subprocess, "run", lambda *a, **k: started.append(a))
    monkeypatch.setattr(pc, "is_windows", lambda: False)
    app, token = factory()
    async with client_for(app, token=token) as c:
        r = await c.post("/api/tools/pc_shutdown_cancel")
    assert r.status_code == 400
    assert started == []


# --- Verallgemeinerte Bestätigungen -------------------------------------------------------


def test_confirm_store_carries_params_once():
    store = pc.ConfirmStore()
    params = {"app_id": "editor", "nested": {"a": 1}}
    cid = store.create("desktop_app_close", params)
    params["nested"]["a"] = 2  # spätere Änderung darf die gespeicherte Aktion nicht verändern
    pending = store.consume(cid)
    assert pending.action == "desktop_app_close" and pending.params == {"app_id": "editor", "nested": {"a": 1}}
    assert store.consume(cid) is None


def test_confirm_store_is_bounded():
    store = pc.ConfirmStore(clock=lambda: 0.0)
    ids = [store.create("pc_shutdown") for _ in range(pc.MAX_PENDING + 5)]
    assert store.consume(ids[0]) is None  # älteste verworfen
    assert store.consume(ids[-1]) is not None


def test_confirm_action_registry_rejects_foreign_override():
    def other(_ctx, _params):
        return {}

    with pytest.raises(ValueError):
        pc.register_confirm_action("pc_shutdown", other)
    assert pc.CONFIRM_ACTIONS["pc_shutdown"] is pc._do_shutdown


def test_request_confirmation_needs_registered_action(factory):
    app, _ = factory()
    with pytest.raises(pc.ToolError):
        pc.request_confirmation(app.state.registry.ctx, "format_c", None, prompt="?", message="?")


async def test_shutdown_result_has_prompt_and_action(factory, calls):
    app, token = factory()
    async with client_for(app, token=token) as c:
        res = (await c.post("/api/tools/pc_shutdown")).json()["result"]
        assert res["action"] == "pc_shutdown" and "15 s" in res["prompt"]
        confirmed = (await c.post(f"/api/confirm/{res['confirm_id']}")).json()["result"]
    assert confirmed["status"] == "shutdown_scheduled" and confirmed["action"] == "pc_shutdown"
    assert calls == [["shutdown", "/s", "/t", "15", "/c", "JARVIS"]]


async def test_confirm_runs_only_the_stored_action(factory, calls, monkeypatch):
    """Eine Bestätigung für 'Programm schließen' fährt nie den PC herunter (und umgekehrt)."""
    from app.tools import desktop

    closed = []
    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    monkeypatch.setattr(desktop, "_user_processes", lambda: [(101, "notepad.exe")])
    monkeypatch.setattr(desktop, "_close_processes", lambda pids, name, grace=5: closed.append(pids) or False)
    app, token = factory()
    async with client_for(app, token=token) as c:
        close_id = (await c.post("/api/tools/desktop_app_close", json={"app_id": "editor"})).json()["result"]["confirm_id"]
        shutdown_id = (await c.post("/api/tools/pc_shutdown")).json()["result"]["confirm_id"]
        assert (await c.post(f"/api/confirm/{close_id}")).json()["result"]["status"] == "closed"
        assert calls == [] and closed == [[101]]
        assert (await c.post(f"/api/confirm/{shutdown_id}")).json()["result"]["status"] == "shutdown_scheduled"
    assert len(calls) == 1 and closed == [[101]]
