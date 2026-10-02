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
