"""Feste PC-Aktionen (app/tools/desktop.py) – Windows komplett gemockt."""

import ctypes
import ipaddress
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import psutil
import pytest

from app.config import parse_config
from app.tools import desktop, pc
from app.tools.registry import ToolError
from tests.conftest import client_for, example_config_dict

pytestmark = pytest.mark.anyio

NOT_WINDOWS = "nur auf dem Windows-PC verfügbar"


@pytest.fixture
def win(monkeypatch):
    """Windows simulieren: alle Windows-Wrapper ersetzt, jeder Aufruf wird aufgezeichnet."""
    calls = []
    state = {"volume": 0.5, "muted": False, "procs": [(101, "notepad.exe"), (102, "Notepad.exe"), (7, "other.exe")],
             "focus": "focused", "lock": True, "forced": False}

    def vset(x):
        calls.append(("volume", round(x, 4)))
        state["volume"] = x

    def vmute(m):
        calls.append(("mute", m))
        state["muted"] = m

    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    monkeypatch.setattr(desktop, "_user_processes", lambda: list(state["procs"]))
    monkeypatch.setattr(desktop, "_start_process", lambda cmd: calls.append(("start", cmd)) or 4242)
    monkeypatch.setattr(desktop, "_close_processes",
                        lambda pids, name, grace=5: calls.append(("close", sorted(pids), name)) or state["forced"])
    monkeypatch.setattr(desktop, "_focus_window", lambda pids, title: calls.append(("focus", pids, title)) or state["focus"])
    monkeypatch.setattr(desktop, "_volume_get", lambda: (state["volume"], state["muted"]))
    monkeypatch.setattr(desktop, "_volume_set", vset)
    monkeypatch.setattr(desktop, "_volume_mute", vmute)
    monkeypatch.setattr(desktop, "_send_media_key", lambda vk, scan: calls.append(("key", vk, scan)))
    monkeypatch.setattr(desktop, "_lock_workstation", lambda: calls.append(("lock",)) or state["lock"])
    monkeypatch.setattr(desktop, "_open_in_browser", lambda url: calls.append(("open", url)))
    # DNS: Vorgabe öffentliche Adresse (die Dokumentationsbereiche wie 192.0.2.0/24 gelten nicht als global)
    state["dns"], state["resolved"] = {}, []
    monkeypatch.setattr(desktop, "_resolve_host", lambda host, timeout=desktop.DNS_TIMEOUT: (
        state["resolved"].append(host) or list(state["dns"].get(host, ["8.8.4.4"]))))
    # Schutz: pc_shutdown darf in diesen Tests nie einen Prozess starten
    monkeypatch.setattr(pc.subprocess, "run", lambda *a, **k: calls.append(("subprocess", a)))
    return SimpleNamespace(calls=calls, state=state)


async def run(factory, tool, args=None, overrides=None):
    app, token = factory(overrides)
    async with client_for(app, token=token) as c:
        r = await c.post(f"/api/tools/{tool}", json=args or {})
    return r


# --------------------------------------------------------------------------------------------
# Programme
# --------------------------------------------------------------------------------------------


async def test_apps_list_shows_running_state(factory, win):
    r = await run(factory, "desktop_apps_list")
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["available"] is True and res["enabled"] is True
    assert res["apps"] == [
        {"id": "editor", "label": "Editor", "configured": True, "running": True},
        {"id": "rechner", "label": "Rechner", "configured": True, "running": False},
    ]


async def test_apps_list_off_windows_does_not_scan(factory, monkeypatch):
    monkeypatch.setattr(desktop, "is_windows", lambda: False)
    monkeypatch.setattr(desktop, "_user_processes", lambda: pytest.fail("Prozessliste auf Nicht-Windows"))
    res = (await run(factory, "desktop_apps_list")).json()["result"]
    assert res["available"] is False
    assert all(a["running"] is False for a in res["apps"])


async def test_app_start_runs_configured_command(factory, win):
    r = await run(factory, "desktop_app_start", {"app_id": "editor"})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["status"] == "started"
    assert win.calls == [("start", ["notepad.exe"])]


@pytest.mark.parametrize("app_id", ["notepad", "cmd", "powershell", "../editor", "EDITOR; calc", "x" * 33])
async def test_unknown_app_ids_are_rejected(factory, win, app_id):
    for tool in ("desktop_app_start", "desktop_app_close", "desktop_focus"):
        r = await run(factory, tool, {"app_id": app_id})
        assert r.status_code in (400, 422), r.text
    assert win.calls == []


async def test_app_id_is_case_insensitive(factory, win):
    r = await run(factory, "desktop_app_start", {"app_id": " Editor "})
    assert r.status_code == 200 and win.calls == [("start", ["notepad.exe"])]


async def test_todo_app_is_not_started(factory, win):
    apps = [{"id": "spotify", "label": "Spotify", "command": ["TODO_PFAD\\Spotify.exe"], "process_name": "Spotify.exe"}]
    r = await run(factory, "desktop_app_start", {"app_id": "spotify"}, {"desktop": {"apps": apps}})
    assert r.status_code == 400 and "noch nicht eingerichtet" in r.json()["error"]
    assert win.calls == []


async def test_app_start_program_missing(factory, win, monkeypatch):
    def missing(cmd):
        raise FileNotFoundError(2, "nicht gefunden")

    monkeypatch.setattr(desktop, "_start_process", missing)
    r = await run(factory, "desktop_app_start", {"app_id": "editor"})
    assert r.status_code == 400 and "nicht gefunden" in r.json()["error"]


async def test_app_close_only_requests_confirmation(factory, win):
    r = await run(factory, "desktop_app_close", {"app_id": "editor"})
    res = r.json()["result"]
    assert res["status"] == "confirm_required" and res["action"] == "desktop_app_close"
    assert res["confirm_id"] and res["expires_in"] == 30
    assert "Editor" in res["prompt"] and "verloren" in res["prompt"]
    assert win.calls == []


async def test_app_close_runs_after_confirm_once(factory, win):
    app, token = factory()
    async with client_for(app, token=token) as c:
        cid = (await c.post("/api/tools/desktop_app_close", json={"app_id": "editor"})).json()["result"]["confirm_id"]
        assert win.calls == []
        r = await c.post(f"/api/confirm/{cid}")
        assert r.status_code == 200, r.text
        res = r.json()["result"]
        assert res["status"] == "closed" and res["action"] == "desktop_app_close"
        assert win.calls == [("close", [101, 102], "notepad.exe")]
        assert (await c.post(f"/api/confirm/{cid}")).status_code == 400
    assert len(win.calls) == 1  # kein Herunterfahren, kein zweites Schließen


async def test_app_close_reports_forced_kill(factory, win):
    win.state["forced"] = True
    app, token = factory()
    async with client_for(app, token=token) as c:
        cid = (await c.post("/api/tools/desktop_app_close", json={"app_id": "editor"})).json()["result"]["confirm_id"]
        res = (await c.post(f"/api/confirm/{cid}")).json()["result"]
    assert res["status"] == "killed" and "hart beendet" in res["message"]


async def test_app_close_not_running_needs_no_confirmation(factory, win):
    r = await run(factory, "desktop_app_close", {"app_id": "rechner"})
    res = r.json()["result"]
    assert res["status"] == "not_running" and "confirm_id" not in res
    assert win.calls == []


async def test_app_closed_meanwhile(factory, win):
    app, token = factory()
    async with client_for(app, token=token) as c:
        cid = (await c.post("/api/tools/desktop_app_close", json={"app_id": "editor"})).json()["result"]["confirm_id"]
        win.state["procs"] = []
        res = (await c.post(f"/api/confirm/{cid}")).json()["result"]
    assert res["status"] == "not_running" and win.calls == []


async def test_app_close_without_token_or_expired_never_closes(factory, win):
    app, token = factory()
    now = [0.0]
    app.state.registry.ctx.extras["confirm"] = pc.ConfirmStore(clock=lambda: now[0])
    async with client_for(app, token=token) as c:
        cid = (await c.post("/api/tools/desktop_app_close", json={"app_id": "editor"})).json()["result"]["confirm_id"]
    async with client_for(app) as anon:
        assert (await anon.post(f"/api/confirm/{cid}")).status_code == 401
    now[0] = 31
    async with client_for(app, token=token) as c:
        assert (await c.post(f"/api/confirm/{cid}")).status_code == 400
    assert win.calls == []


async def test_llm_cannot_close_app_without_ui_confirmation(factory, win):
    from tests.test_llm import scripted, text, tool_call

    factory.llm_handler = scripted(
        tool_call("desktop_app_close", {"app_id": "editor"}),
        {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "confirm", "arguments": {"confirm_id": "x"}}},
            {"function": {"name": "desktop_confirm", "arguments": {"app_id": "editor"}}},
            {"function": {"name": "pc_confirm", "arguments": {}}}]}},
        text("Bitte bestätigen."),
    )
    app, token = factory({"llm": {"model": "test-modell:1b"}})
    async with client_for(app, token=token) as c:
        body = (await c.post("/api/chat", json={"message": "Schließ den Editor"})).json()
    assert body["tool_calls"][0]["result"]["status"] == "confirm_required"
    assert all(r.get("rejected") for r in body["tool_calls"][1:])
    assert win.calls == []


async def test_confirm_action_revalidates_app_id(factory, win):
    """Eine ausstehende Aktion mit unbekannter ID (z. B. nach Config-Änderung) schließt nichts."""
    app, token = factory()
    ctx = app.state.registry.ctx
    store = ctx.extras.setdefault("confirm", pc.ConfirmStore())
    cid = store.create("desktop_app_close", {"app_id": "explorer"})
    async with client_for(app, token=token) as c:
        r = await c.post(f"/api/confirm/{cid}")
    assert r.status_code == 400 and "Unbekanntes Programm" in r.json()["error"]
    assert win.calls == []


@pytest.mark.parametrize("outcome,status,text_part", [
    ("focused", "focused", "Vordergrund"),
    ("refused", "refused", "verweigert"),
    ("no_window", "no_window", "Kein sichtbares Fenster"),
])
async def test_focus(factory, win, outcome, status, text_part):
    win.state["focus"] = outcome
    res = (await run(factory, "desktop_focus", {"app_id": "editor"})).json()["result"]
    assert res["status"] == status and text_part in res["message"]
    assert win.calls == [("focus", [101, 102], "Editor")]


async def test_focus_not_running(factory, win):
    res = (await run(factory, "desktop_focus", {"app_id": "rechner"})).json()["result"]
    assert res["status"] == "not_running" and win.calls == []


# --------------------------------------------------------------------------------------------
# Lautstärke, Medien, Sperren
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("args,expected_calls,percent,muted", [
    ({"action": "get"}, [], 50, False),
    ({"action": "set", "percent": 30}, [("volume", 0.3)], 30, False),
    ({"action": "set", "percent": 150}, [("volume", 1.0)], 100, False),
    ({"action": "set", "percent": -5}, [("volume", 0.0)], 0, False),
    ({"action": "set", "percent": 33.6}, [("volume", 0.34)], 34, False),
    ({"action": "up"}, [("volume", 0.6)], 60, False),
    ({"action": "down", "step": 25}, [("volume", 0.25)], 25, False),
    ({"action": "mute"}, [("mute", True)], 50, True),
    ({"action": "unmute"}, [("mute", False)], 50, False),
])
async def test_volume(factory, win, args, expected_calls, percent, muted):
    r = await run(factory, "desktop_volume", args)
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert win.calls == expected_calls
    assert res["percent"] == percent and res["muted"] is muted


async def test_volume_up_clamps_and_unmutes(factory, win):
    win.state.update(volume=0.95, muted=True)
    res = (await run(factory, "desktop_volume", {"action": "up"})).json()["result"]
    assert win.calls == [("volume", 1.0), ("mute", False)]
    assert res == {"action": "up", "percent": 100, "muted": False}


@pytest.mark.parametrize("args", [
    {"action": "set"}, {"action": "laut"}, {"action": "up", "step": 0}, {"action": "up", "step": 101},
    {"action": "get", "extra": 1}, {"action": "set", "percent": "viel"},
])
async def test_volume_invalid_params(factory, win, args):
    r = await run(factory, "desktop_volume", args)
    assert r.status_code == 422, r.text
    assert win.calls == []


@pytest.mark.parametrize("action,vk,scan", [
    ("play_pause", 0xB3, 0x22), ("next", 0xB0, 0x19), ("previous", 0xB1, 0x10), ("stop", 0xB2, 0x24),
])
async def test_media_keys(factory, win, action, vk, scan):
    r = await run(factory, "desktop_media", {"action": action})
    assert r.status_code == 200 and r.json()["result"] == {"status": "sent", "action": action}
    assert win.calls == [("key", vk, scan)]


@pytest.mark.parametrize("action", ["volume_up", "a", "enter", "alt+f4", ""])
async def test_media_rejects_other_keys(factory, win, action):
    assert (await run(factory, "desktop_media", {"action": action})).status_code == 422
    assert win.calls == []


async def test_lock(factory, win):
    res = (await run(factory, "desktop_lock")).json()["result"]
    assert res["status"] == "locked" and win.calls == [("lock",)]


async def test_lock_refused_by_windows(factory, win):
    win.state["lock"] = False
    r = await run(factory, "desktop_lock")
    assert r.status_code == 400 and "abgelehnt" in r.json()["error"]


# --------------------------------------------------------------------------------------------
# Webseite öffnen
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("https://example.org", "https://example.org"),
    ("HTTPS://Example.ORG/Pfad?q=1#x", "https://example.org/Pfad?q=1#x"),
    ("http://example.org:8080/a", "http://example.org:8080/a"),
    ("https://de.wikipedia.org/wiki/Bär", "https://de.wikipedia.org/wiki/B%C3%A4r"),
    ("https://bücher.de/", "https://xn--bcher-kva.de/"),
    ("https://www.youtube.com/watch?v=abc&t=10", "https://www.youtube.com/watch?v=abc&t=10"),
    ("https://8.8.8.8/", "https://8.8.8.8/"),
])
async def test_open_url_accepts_http_and_https(factory, win, url, expected):
    r = await run(factory, "desktop_open_url", {"url": url})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["url"] == expected
    assert win.calls == [("open", expected)]


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/System32/cmd.exe",
    "file://host/share/x.exe",
    "javascript:alert(1)",
    "ms-settings:",
    "ms-settings:windowsupdate",
    "data:text/html,<script>alert(1)</script>",
    "\\\\host\\share\\x.exe",
    "//host/share",
    "\\\\?\\C:\\Windows",
    "ftp://example.org",
    "shell:startup",
    "steam://run/730",
    "vbscript:msgbox",
    "http:example.org",
    "https://",
    "https:///pfad",
    "http://localhost:8765/api/confirm/x",
    "http://LOCALHOST/",
    "http://127.0.0.1:8765/",
    "http://192.168.1.1/",
    "http://10.0.0.5/win&T=0",
    "http://100.100.100.100/",
    "http://169.254.1.1/",
    "http://[::1]/",
    "http://[fe80::1]/",
    "http://2130706433/",
    "http://0x7f.1/",
    "http://127.1/",
    "http://fritz.box/",
    "http://wled.local/win&T=0",
    "http://router.lan/",
    "http://intranet/",
    "http://user:pass@example.org/",
    "https://example.org@evil.example/",
    "http://exa mple.org/",
    "https://example.org/\x00",
    "https://example.org/\npfad",
    "https://example.org/a\\b",
    'https://example.org/"onload',
    "https://example.org/<x>",
    "https://exa_mple.org/",
    "https://-example.org/",
    "https://example.org:99999/",
    "http://[::1/",
    "http://[example.org]/",
    "http://exam%70le.org/",
    "  ",
])
async def test_open_url_rejects_dangerous_targets(factory, win, url):
    r = await run(factory, "desktop_open_url", {"url": url})
    assert r.status_code in (400, 422), (url, r.text)
    assert win.calls == []


async def test_open_url_length_limit(factory, win):
    r = await run(factory, "desktop_open_url", {"url": "https://example.org/" + "a" * 2100})
    assert r.status_code == 422 and win.calls == []


@pytest.mark.parametrize("url,ok", [
    ("https://youtube.com/", True),
    ("https://www.youtube.com/watch?v=1", True),
    ("https://music.youtube.com", True),
    ("https://wikipedia.org", True),
    ("https://evilyoutube.com/", False),
    ("https://youtube.com.evil.example/", False),
    ("https://example.org/", False),
    ("https://8.8.8.8/", False),
])
async def test_open_url_domain_allowlist(factory, win, url, ok):
    overrides = {"desktop": {"allowed_domains": ["YouTube.com", "wikipedia.org"]}}
    r = await run(factory, "desktop_open_url", {"url": url}, overrides)
    assert (r.status_code == 200) is ok, r.text
    assert bool(win.calls) is ok
    if not ok:
        assert "nicht freigegeben" in r.json()["error"]


async def test_open_url_local_name_only_when_allowlisted(factory, win):
    overrides = {"desktop": {"allowed_domains": ["fritz.box"]}}
    assert (await run(factory, "desktop_open_url", {"url": "http://fritz.box/"}, overrides)).status_code == 200
    # Private IP-Adressen bleiben auch dann gesperrt
    assert (await run(factory, "desktop_open_url", {"url": "http://192.168.1.1/"}, overrides)).status_code == 400
    assert win.calls == [("open", "http://fritz.box/")]


@pytest.mark.parametrize("addresses", [
    ["127.0.0.1"], ["192.168.1.50"], ["10.0.0.5"], ["100.101.102.103"], ["::1"], ["fe80::1%12"],
    ["8.8.4.4", "192.168.1.50"], ["::ffff:192.168.1.50"], ["64:ff9b::c0a8:132"],
])
async def test_open_url_rejects_domains_resolving_to_home_network(factory, win, addresses):
    """z. B. 192.168.1.50.nip.io oder localtest.me: der Name sieht öffentlich aus, zeigt aber ins Heimnetz."""
    win.state["dns"]["trick.example"] = addresses
    r = await run(factory, "desktop_open_url", {"url": "http://trick.example/win&T=0"})
    assert r.status_code == 400 and "zeigt auf eine Adresse im Heimnetz" in r.json()["error"], r.text
    assert win.calls == [] and win.state["resolved"] == ["trick.example"]


async def test_open_url_dns_check_skipped_for_allowlisted_and_unresolvable(factory, win):
    overrides = {"desktop": {"allowed_domains": ["fritz.box", "example.org"]}}
    win.state["dns"]["fritz.box"] = ["192.168.1.1"]
    assert (await run(factory, "desktop_open_url", {"url": "http://fritz.box/"}, overrides)).status_code == 200
    assert win.state["resolved"] == []  # ausdrücklich freigegeben: keine DNS-Prüfung
    win.state["dns"]["nirgends.example.org"] = []  # nicht auflösbar → Browser zeigt seine Fehlerseite
    r = await run(factory, "desktop_open_url", {"url": "https://nirgends.example.org/"})
    assert r.status_code == 200, r.text
    assert win.calls == [("open", "http://fritz.box/"), ("open", "https://nirgends.example.org/")]


async def test_open_url_ip_literal_is_not_resolved(factory, win):
    assert (await run(factory, "desktop_open_url", {"url": "https://8.8.8.8/"})).status_code == 200
    assert win.state["resolved"] == []


@pytest.mark.parametrize("url", [
    "http://[::127.0.0.1]/", "http://[64:ff9b::c0a8:101]/", "http://[64:ff9b:1::a00:5]/", "http://[2002:c0a8:101::1]/",
    "http://[::ffff:7f00:1]/",
])
def test_validate_url_rejects_ipv4_embedded_in_ipv6(url):
    cfg = parse_config(example_config_dict()).desktop
    with pytest.raises(ToolError, match="Heimnetz"):
        desktop.validate_url(url, cfg)


def test_resolve_host_wrapper_reads_local_names():
    """Echter Wrapper (ohne Netz): localhost steht in der hosts-Datei; Unsinn liefert eine leere Liste."""
    assert any(ipaddress.ip_address(a.split("%")[0]).is_loopback for a in desktop._resolve_host("localhost"))
    assert desktop._resolve_host("ungueltig..name", timeout=2) == []


async def test_open_url_can_be_disabled(factory, win):
    r = await run(factory, "desktop_open_url", {"url": "https://example.org"}, {"desktop": {"allow_open_url": False}})
    assert r.status_code == 400 and "ausgeschaltet" in r.json()["error"]
    assert win.calls == []


def test_validate_url_messages():
    cfg = parse_config(example_config_dict()).desktop
    with pytest.raises(ToolError, match="Nur http- und https"):
        desktop.validate_url("ms-settings:", cfg)
    with pytest.raises(ToolError, match="Heimnetz"):
        desktop.validate_url("http://192.168.1.1", cfg)
    with pytest.raises(ToolError, match="Benutzername"):
        desktop.validate_url("https://a:b@example.org", cfg)
    with pytest.raises(ToolError, match="ungültige Zeichen"):
        desktop.validate_url("\\\\host\\share", cfg)


# --------------------------------------------------------------------------------------------
# Nicht-Windows und abgeschaltet
# --------------------------------------------------------------------------------------------

ACTIONS = [
    ("desktop_app_start", {"app_id": "editor"}),
    ("desktop_app_close", {"app_id": "editor"}),
    ("desktop_focus", {"app_id": "editor"}),
    ("desktop_volume", {"action": "get"}),
    ("desktop_volume", {"action": "mute"}),
    ("desktop_media", {"action": "play_pause"}),
    ("desktop_lock", {}),
    ("desktop_open_url", {"url": "https://example.org"}),
]


@pytest.mark.parametrize("tool,args", ACTIONS)
async def test_non_windows_refuses_cleanly(factory, monkeypatch, tool, args):
    monkeypatch.setattr(desktop, "is_windows", lambda: False)
    started = []
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda *a, **k: started.append(a))
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: started.append(a))
    r = await run(factory, tool, args)
    assert r.status_code == 400, r.text
    assert NOT_WINDOWS in r.json()["error"]
    assert started == []


@pytest.mark.parametrize("call", [
    lambda: desktop._send_media_key(0xB3, 0x22),
    lambda: desktop._lock_workstation(),
    lambda: desktop._focus_window([1], "x"),
    lambda: desktop._volume_get(),
    lambda: desktop._volume_set(0.5),
    lambda: desktop._volume_mute(True),
    lambda: desktop._start_process(["notepad.exe"]),
    lambda: desktop._user_processes(),
    lambda: desktop._close_processes([1], "notepad.exe"),
    lambda: desktop._open_in_browser("https://example.org"),
])
def test_wrappers_refuse_off_windows(monkeypatch, call):
    monkeypatch.setattr(desktop, "is_windows", lambda: False)
    with pytest.raises(ToolError, match=NOT_WINDOWS):
        call()


@pytest.mark.parametrize("tool,args", ACTIONS)
async def test_disabled_desktop_refuses(factory, win, tool, args):
    r = await run(factory, tool, args, {"desktop": {"enabled": False}})
    assert r.status_code == 400 and "ausgeschaltet" in r.json()["error"]
    assert win.calls == []


async def test_disabled_desktop_list(factory, win):
    res = (await run(factory, "desktop_apps_list", {}, {"desktop": {"enabled": False}})).json()["result"]
    assert res["enabled"] is False and res["available"] is False


# --------------------------------------------------------------------------------------------
# Sicherheit: keine generische Eingabe-/Befehlsfähigkeit für das LLM
# --------------------------------------------------------------------------------------------

EXPECTED_LLM_TOOLS = {
    "pc_shutdown", "pc_shutdown_cancel",
    "led_power", "led_brightness", "led_color", "led_effect", "led_preset", "led_status",
    "sensors_read", "scripts_list", "scripts_status", "scripts_start", "scripts_stop",
    "desktop_apps_list", "desktop_app_start", "desktop_app_close", "desktop_focus", "desktop_volume",
    "desktop_media", "desktop_lock", "desktop_open_url", "screen_describe",
}
# Freie Text-Parameter (ohne enum), die es geben darf – alles andere wäre eine neue Angriffsfläche.
ALLOWED_FREE_STRINGS = {"color", "effect", "preset", "script_id", "app_id", "url", "question"}


async def test_llm_sees_only_fixed_tools(factory):
    app, _ = factory()
    tools = app.state.registry.ollama_tools()
    names = {t["function"]["name"] for t in tools}
    assert names == EXPECTED_LLM_TOOLS
    forbidden = ("shell", "exec", "command", "cmd", "powershell", "eval", "type_text", "keyboard", "mouse",
                 "click", "input", "keys", "hotkey", "process", "run_")
    assert not [n for n in names if any(f in n for f in forbidden)]
    for tool in tools:
        props = tool["function"]["parameters"].get("properties", {})
        for pname, schema in props.items():
            assert pname not in {"x", "y", "text", "keys", "command", "cmd", "args", "path", "program"}, pname
            if schema.get("type") == "string" and "enum" not in schema:
                assert pname in ALLOWED_FREE_STRINGS, (tool["function"]["name"], pname)
                if tool["function"]["name"].startswith(("desktop_", "screen_")):
                    assert schema.get("maxLength", 10_000) <= 2048, (tool["function"]["name"], pname)


async def test_llm_cannot_call_ui_only_or_unknown_tools(factory):
    from tests.test_llm import scripted, text

    factory.llm_handler = scripted(
        {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": n, "arguments": a}} for n, a in [
                ("screen_status", {}), ("desktop_type_text", {"text": "format c:"}),
                ("desktop_click", {"x": 1, "y": 2}), ("desktop_run", {"command": "cmd.exe"}),
                ("desktop_keys", {"keys": "win+r"})]]}},
        text("Das geht nicht."),
    )
    app, token = factory({"llm": {"model": "test-modell:1b"}})
    async with client_for(app, token=token) as c:
        body = (await c.post("/api/chat", json={"message": "tipp format c:"})).json()
    assert len(body["tool_calls"]) == 5 and all(r["rejected"] for r in body["tool_calls"])


def test_send_media_key_rejects_other_virtual_keys(monkeypatch):
    fake = FakeUser32()
    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    monkeypatch.setattr(desktop, "_user32", lambda: fake)
    for vk in (0x41, 0x5B, 0x0D, 0x12, 0x2E, 0xAD, 0xAF, 0):
        with pytest.raises(ToolError):
            desktop._send_media_key(vk, 0)
    assert fake.sent == []


# --------------------------------------------------------------------------------------------
# Windows-Wrapper mit nachgebautem user32
# --------------------------------------------------------------------------------------------


class FakeUser32:
    """Nachbau der benutzten user32-Funktionen. windows: (hwnd, pid, sichtbar, owner, titel, minimiert)."""

    def __init__(self, windows=(), foreground_ok=True, send_ok=True, lock_ok=True):
        self.windows = list(windows)
        self.foreground_ok = foreground_ok
        self.send_ok = send_ok
        self.lock_ok = lock_ok
        self.sent = []
        self.shown = []
        self.foreground = []
        self.locked = 0

    def _win(self, hwnd):
        return next(w for w in self.windows if w[0] == hwnd)

    def SendInput(self, n, inputs, size):
        self.sent.append((n, [(i.type, i.u.ki.wVk, i.u.ki.wScan, i.u.ki.dwFlags) for i in inputs[:n]], size))
        return n if self.send_ok else 0

    def LockWorkStation(self):
        self.locked += 1
        return 1 if self.lock_ok else 0

    def EnumWindows(self, callback, _lparam):
        for w in self.windows:
            if not callback(w[0], None):
                break
        return 1

    def IsWindowVisible(self, hwnd):
        return self._win(hwnd)[2]

    def GetWindow(self, hwnd, cmd):
        assert cmd == desktop.GW_OWNER
        return self._win(hwnd)[3]

    def GetWindowThreadProcessId(self, hwnd, pid_ptr):
        pid_ptr.contents.value = self._win(hwnd)[1]
        return 1

    def GetWindowTextLengthW(self, hwnd):
        return len(self._win(hwnd)[4])

    def GetWindowTextW(self, hwnd, buf, size):
        buf.value = self._win(hwnd)[4][: size - 1]
        return len(buf.value)

    def IsIconic(self, hwnd):
        return self._win(hwnd)[5]

    def ShowWindow(self, hwnd, cmd):
        self.shown.append((hwnd, cmd))
        return 1

    def SetForegroundWindow(self, hwnd):
        self.foreground.append(hwnd)
        return 1 if self.foreground_ok else 0


@pytest.fixture
def fake_user32(monkeypatch):
    holder = {}

    def make(**kwargs):
        holder["fake"] = FakeUser32(**kwargs)
        return holder["fake"]

    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    monkeypatch.setattr(desktop, "_user32", lambda: holder["fake"])
    return make


def test_input_struct_matches_windows_layout():
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("Layout-Prüfung für 64 bit")
    # Windows x64: sizeof(INPUT) = 40, KEYBDINPUT = 24, Union ab Offset 8 – sonst schlägt SendInput fehl.
    assert ctypes.sizeof(desktop.INPUT) == 40
    assert ctypes.sizeof(desktop.KEYBDINPUT) == 24
    assert desktop.INPUT.u.offset == 8


def test_send_media_key_uses_sendinput(fake_user32):
    fake = fake_user32()
    desktop._send_media_key(0xB3, 0x22)
    (n, events, size), = fake.sent
    assert n == 2 and size == ctypes.sizeof(desktop.INPUT)
    assert events == [(1, 0xB3, 0x22, 0x0001), (1, 0xB3, 0x22, 0x0001 | 0x0002)]


def test_send_media_key_reports_failure(fake_user32):
    fake_user32(send_ok=False)
    with pytest.raises(ToolError, match="nicht angenommen"):
        desktop._send_media_key(0xB0, 0x19)


def test_lock_workstation(fake_user32):
    fake = fake_user32()
    assert desktop._lock_workstation() is True and fake.locked == 1
    fake_user32(lock_ok=False)
    assert desktop._lock_workstation() is False


def test_focus_prefers_visible_main_window_of_pid(fake_user32):
    fake = fake_user32(windows=[
        (1, 500, True, None, "Editor", False),  # fremder Prozess, passender Titel
        (2, 101, False, None, "unsichtbar", False),
        (3, 101, True, 99, "Dialog (owned)", False),
        (4, 101, True, None, "Unbenannt – Editor", True),
    ])
    assert desktop._focus_window([101], "Editor") == "focused"
    assert fake.shown == [(4, desktop.SW_RESTORE)] and fake.foreground == [4]


def test_focus_falls_back_to_title(fake_user32):
    """Store-Apps (Rechner): Fenster gehört ApplicationFrameHost, gefunden wird es über den Titel."""
    fake = fake_user32(windows=[(1, 500, True, None, "Firefox", False), (2, 600, True, None, "Rechner", False)])
    assert desktop._focus_window([101], "rechner") == "focused"
    assert fake.foreground == [2] and fake.shown == []


def test_focus_refused_and_no_window(fake_user32):
    fake_user32(windows=[(4, 101, True, None, "Editor", False)], foreground_ok=False)
    assert desktop._focus_window([101], "") == "refused"
    fake_user32(windows=[(1, 500, True, None, "Firefox", False)])
    assert desktop._focus_window([101], "") == "no_window"
    assert desktop._focus_window([101], "Editor") == "no_window"


def test_open_in_browser_uses_startfile(monkeypatch):
    opened = []
    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    monkeypatch.setattr(desktop.os, "startfile", opened.append, raising=False)
    desktop._open_in_browser("https://example.org/")
    assert opened == ["https://example.org/"]


def test_start_process_uses_argument_list(monkeypatch):
    seen = []

    class FakePopen:
        pid = 4711

        def __init__(self, cmd, **kwargs):
            seen.append((cmd, kwargs))

        def poll(self):
            return None

    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    monkeypatch.setattr(desktop.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(desktop, "_STARTED", {})
    assert desktop._start_process(["notepad.exe"]) == 4711
    (cmd, kwargs), = seen
    assert cmd == ["notepad.exe"] and kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL


# --------------------------------------------------------------------------------------------
# Prozesse (echtes psutil, Hilfsprozesse dieses Tests)
# --------------------------------------------------------------------------------------------


@pytest.fixture
def sleeper():
    procs = []

    def start(ignore_term=False):
        code = "import signal, time\n"
        if ignore_term:
            code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        code += "time.sleep(60)\n"
        proc = subprocess.Popen([sys.executable, "-c", code])
        procs.append(proc)
        time.sleep(0.2)
        return proc

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_user_processes_lists_own_user_without_jarvis(monkeypatch, sleeper):
    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    child = sleeper()
    procs = dict(desktop._user_processes())
    assert child.pid in procs
    assert os.getpid() not in procs


def test_close_processes_soft_then_hard(monkeypatch, sleeper):
    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    soft = []
    monkeypatch.setattr(desktop, "_soft_close", lambda pid: soft.append(pid) or os.kill(pid, 15))
    polite = sleeper()
    name = psutil.Process(polite.pid).name()
    assert desktop._close_processes([polite.pid], name, grace=3) is False
    assert soft == [polite.pid]
    assert polite.wait(timeout=5) is not None

    stubborn = sleeper(ignore_term=True)
    soft.clear()
    assert desktop._close_processes([stubborn.pid], name, grace=0.3) is True
    assert soft == [stubborn.pid]
    assert stubborn.wait(timeout=5) is not None


def test_close_processes_never_touches_jarvis_or_other_names(monkeypatch, sleeper):
    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    soft = []
    monkeypatch.setattr(desktop, "_soft_close", soft.append)
    child = sleeper()
    me = psutil.Process()
    assert desktop._close_processes([os.getpid(), me.ppid()], me.name(), grace=0.1) is False
    assert desktop._close_processes([child.pid], "anderer-name.exe", grace=0.1) is False
    assert soft == [] and child.poll() is None


# --------------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------------


def test_tool_descriptions_list_app_ids(factory):
    app, _ = factory()
    tool = app.state.registry.get("desktop_app_start")
    assert "editor (Editor)" in tool.description and "rechner (Rechner)" in tool.description


def test_desktop_tools_registered_with_json_schema(factory):
    app, _ = factory()
    schema = app.state.registry.get("desktop_volume").json_schema()
    assert schema["properties"]["action"]["enum"] == ["set", "up", "down", "mute", "unmute", "get"]
    assert json.dumps(schema)  # serialisierbar für Ollama


def test_audio_errors_become_tool_errors(monkeypatch):
    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    monkeypatch.setattr(desktop, "_com_thread_init", lambda: None)
    monkeypatch.setattr(desktop, "_AUDIO_EXECUTOR", None)

    def broken():
        raise OSError("kein Gerät")

    with pytest.raises(ToolError, match="Lautstärke nicht verfügbar"):
        desktop._audio_call(broken)
    assert desktop._audio_call(lambda: 42) == 42
    desktop._AUDIO_EXECUTOR.shutdown(wait=True)
