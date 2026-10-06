import pytest

from app.config import parse_config
from app.fallback import HELP, parse
from tests.conftest import client_for, example_config_dict
from tests.test_led import wled_mock

pytestmark = pytest.mark.anyio

CFG = parse_config(example_config_dict())


@pytest.mark.parametrize("text,expected", [
    ("Licht an", [("led_power", {"state": "on"})]),
    ("mach das Licht aus!", [("led_power", {"state": "off"})]),
    ("LED umschalten", [("led_power", {"state": "toggle"})]),
    ("Helligkeit 40", [("led_brightness", {"percent": 40})]),
    ("helligkeit auf 75 %", [("led_brightness", {"percent": 75})]),
    ("Licht auf 20 Prozent", [("led_brightness", {"percent": 20})]),
    ("Farbe rot", [("led_color", {"color": "rot"})]),
    ("mach das licht grün", [("led_color", {"color": "gruen"})]),
    ("Licht warmweiß", [("led_color", {"color": "warmweiss"})]),
    ("licht an, helligkeit 40 und farbe blau", [
        ("led_power", {"state": "on"}), ("led_brightness", {"percent": 40}), ("led_color", {"color": "blau"})]),
    ("Effekt Rainbow", [("led_effect", {"effect": "rainbow"})]),
    ("preset abend", [("led_preset", {"preset": "abend"})]),
    ("Ist das Licht an?", [("led_status", {})]),
    ("Wie warm ist es?", [("sensors_read", {})]),
    ("Luftfeuchtigkeit", [("sensors_read", {})]),
    ("Starte CS2", [("scripts_start", {"script_id": "cs2"})]),
    ("starte das CS2-Skript", [("scripts_start", {"script_id": "cs2"})]),
    ("stoppe cs2", [("scripts_stop", {"script_id": "cs2"})]),
    ("CS2 beenden", [("scripts_stop", {"script_id": "cs2"})]),
    ("welche Skripte laufen", [("scripts_list", {})]),
    ("PC ausschalten", [("pc_shutdown", {})]),
    ("fahr den Rechner herunter", [("pc_shutdown", {})]),
    ("Herunterfahren abbrechen", [("pc_shutdown_cancel", {})]),
])
def test_parse(text, expected):
    assert parse(text, CFG) == expected


def test_unknown_script():
    result = parse("starte minecraft", CFG)
    assert isinstance(result, str) and "CS2-Skript" in result


def test_nonsense():
    assert parse("erzähl mir einen witz", CFG) == HELP


async def test_fallback_endpoint_executes_and_replies(factory):
    handler, posted = wled_mock()
    factory.device_handler = handler
    app, token = factory({"wled": {"base_url": "http://wled.test"}})
    async with client_for(app, token=token) as c:
        body = (await c.post("/api/chat", json={"message": "Licht an und Helligkeit 50"})).json()
    assert body["source"] == "fallback"
    assert body["reply"] == "Licht an. Helligkeit 50 %."
    assert posted == [{"on": True, "v": True}, {"on": True, "bri": 128, "v": True}]


async def test_fallback_reports_tool_errors(factory):
    app, token = factory()
    async with client_for(app, token=token) as c:
        body = (await c.post("/api/chat", json={"message": "starte cs2"})).json()
    assert "noch nicht eingerichtet" in body["reply"]
    assert body["tool_calls"][0]["ok"] is False


# --- PC-Steuerung, Bildschirm ----------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Lauter", [("desktop_volume", {"action": "up"})]),
    ("etwas leiser bitte", [("desktop_volume", {"action": "down"})]),
    ("Lautstärke 30", [("desktop_volume", {"action": "set", "percent": 30})]),
    ("Lautstärke auf 75 %", [("desktop_volume", {"action": "set", "percent": 75})]),
    ("stell die Lautstärke auf 5 Prozent", [("desktop_volume", {"action": "set", "percent": 5})]),
    ("Stumm", [("desktop_volume", {"action": "mute"})]),
    ("mach den Ton aus", [("desktop_volume", {"action": "mute"})]),
    ("Ton an", [("desktop_volume", {"action": "unmute"})]),
    ("Ton wieder an", [("desktop_volume", {"action": "unmute"})]),
    ("stumm aus", [("desktop_volume", {"action": "unmute"})]),
    ("Wie laut ist es?", [("desktop_volume", {"action": "get"})]),
    ("Pause", [("desktop_media", {"action": "play_pause"})]),
    ("Play", [("desktop_media", {"action": "play_pause"})]),
    ("weiter", [("desktop_media", {"action": "play_pause"})]),
    ("Musik pausieren", [("desktop_media", {"action": "play_pause"})]),
    ("Nächster Titel", [("desktop_media", {"action": "next"})]),
    ("nächstes Lied", [("desktop_media", {"action": "next"})]),
    ("Vorheriger Titel", [("desktop_media", {"action": "previous"})]),
    ("letztes Lied", [("desktop_media", {"action": "previous"})]),
    ("stopp die Musik", [("desktop_media", {"action": "stop"})]),
    ("PC sperren", [("desktop_lock", {})]),
    ("Bildschirm sperren", [("desktop_lock", {})]),
    ("sperr den Rechner", [("desktop_lock", {})]),
    ("Öffne youtube.com", [("desktop_open_url", {"url": "https://youtube.com"})]),
    ("öffne https://de.wikipedia.org/wiki/Bär", [("desktop_open_url", {"url": "https://de.wikipedia.org/wiki/Bär"})]),
    ("geh auf heise.de", [("desktop_open_url", {"url": "https://heise.de"})]),
    ("öffne die Seite www.example.org/test?x=1 im Browser", [
        ("desktop_open_url", {"url": "https://www.example.org/test?x=1"})]),
    ("Starte Editor", [("desktop_app_start", {"app_id": "editor"})]),
    ("starte den Rechner", [("desktop_app_start", {"app_id": "rechner"})]),
    ("öffne den Editor", [("desktop_app_start", {"app_id": "editor"})]),
    ("öffne rechner.exe", [("desktop_app_start", {"app_id": "rechner"})]),
    ("Schließe den Editor", [("desktop_app_close", {"app_id": "editor"})]),
    ("Rechner schließen", [("desktop_app_close", {"app_id": "rechner"})]),
    ("beende den Editor", [("desktop_app_close", {"app_id": "editor"})]),
    ("wechsle zum Editor", [("desktop_focus", {"app_id": "editor"})]),
    ("hol den Rechner nach vorne", [("desktop_focus", {"app_id": "rechner"})]),
    ("welche Programme laufen", [("desktop_apps_list", {})]),
    ("Was ist auf dem Bildschirm?", [("screen_describe", {})]),
    ("Beschreibe den Bildschirm", [("screen_describe", {})]),
    ("was siehst du auf meinem Monitor", [("screen_describe", {})]),
    ("Was steht in der Fehlermeldung auf dem Bildschirm?", [
        ("screen_describe", {"question": "Was steht in der Fehlermeldung auf dem Bildschirm?"})]),
    # bestehende Regeln bleiben unverändert
    ("fahr den Rechner herunter", [("pc_shutdown", {})]),
    ("mach den Rechner aus", [("pc_shutdown", {})]),
    ("starte cs2", [("scripts_start", {"script_id": "cs2"})]),
    ("öffne cs2", [("scripts_start", {"script_id": "cs2"})]),
    ("schließe cs2", [("scripts_stop", {"script_id": "cs2"})]),
    ("Licht auf 20 Prozent", [("led_brightness", {"percent": 20})]),
])
def test_parse_desktop(text, expected):
    assert parse(text, CFG) == expected


@pytest.mark.parametrize("text", [
    "öffne file:///C:/Windows/System32/cmd.exe", "öffne javascript:alert(1)", "öffne ms-settings:",
    "öffne data:text/html,hi", "öffne \\\\host\\share",
])
def test_parse_hands_dangerous_urls_to_validating_tool(text):
    (tool, args), = parse(text, CFG)
    assert tool == "desktop_open_url" and not args["url"].startswith("https://")


def test_scripts_take_precedence_over_apps():
    data = example_config_dict()
    data["desktop"] = {"apps": [{"id": "cs2", "label": "CS2", "command": ["cs2.exe"], "process_name": "cs2.exe"}]}
    cfg = parse_config(data)
    assert parse("starte cs2", cfg) == [("scripts_start", {"script_id": "cs2"})]


def test_unknown_program_lists_scripts_and_programs():
    result = parse("starte minecraft", CFG)
    assert isinstance(result, str) and "CS2-Skript" in result and "Editor" in result and "Rechner" in result


async def test_fallback_desktop_volume_endpoint(factory, monkeypatch):
    from app.tools import desktop

    state = {"v": 0.5}
    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    monkeypatch.setattr(desktop, "_volume_get", lambda: (state["v"], False))
    monkeypatch.setattr(desktop, "_volume_set", lambda x: state.update(v=x))
    app, token = factory()
    async with client_for(app, token=token) as c:
        body = (await c.post("/api/chat", json={"message": "Lautstärke 30"})).json()
    assert body["source"] == "fallback" and body["reply"] == "Lautstärke 30 %."
    assert state["v"] == pytest.approx(0.3)


async def test_fallback_rejects_dangerous_url_with_message(factory, monkeypatch):
    from app.tools import desktop

    opened = []
    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    monkeypatch.setattr(desktop, "_open_in_browser", opened.append)
    app, token = factory()
    async with client_for(app, token=token) as c:
        body = (await c.post("/api/chat", json={"message": "öffne ms-settings:"})).json()
        ok = (await c.post("/api/chat", json={"message": "öffne example.org"})).json()
    assert "Nur http- und https" in body["reply"] and body["tool_calls"][0]["ok"] is False
    assert ok["reply"] == "example.org im Browser geöffnet."
    assert opened == ["https://example.org"]


async def test_fallback_close_needs_confirmation(factory, monkeypatch):
    from app.tools import desktop

    closed = []
    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    monkeypatch.setattr(desktop, "_user_processes", lambda: [(101, "notepad.exe")])
    monkeypatch.setattr(desktop, "_close_processes", lambda *a, **k: closed.append(a))
    app, token = factory()
    async with client_for(app, token=token) as c:
        body = (await c.post("/api/chat", json={"message": "schließe den Editor"})).json()
    assert body["tool_calls"][0]["result"]["status"] == "confirm_required"
    assert "bestätigen" in body["reply"] and closed == []


async def test_fallback_media_and_lock_off_windows(factory, monkeypatch):
    from app.tools import desktop

    monkeypatch.setattr(desktop, "is_windows", lambda: False)
    app, token = factory()
    async with client_for(app, token=token) as c:
        body = (await c.post("/api/chat", json={"message": "PC sperren"})).json()
    assert "nur auf dem Windows-PC verfügbar" in body["reply"]


# --- Sätze mit mehreren Teilen (vor allem gesprochen, ohne LLM) ----------------------------------
@pytest.mark.parametrize("text,expected", [
    ("Licht aus und PC sperren", [("led_power", {"state": "off"}), ("desktop_lock", {})]),
    ("Licht an und lauter", [("led_power", {"state": "on"}), ("desktop_volume", {"action": "up"})]),
    ("Licht aus, Pause", [("led_power", {"state": "off"}), ("desktop_media", {"action": "play_pause"})]),
    ("Schließe Rechner und starte Editor",
     [("desktop_app_close", {"app_id": "rechner"}), ("desktop_app_start", {"app_id": "editor"})]),
    # früher: "rechner" + "aus" im selben Satz = Herunterfahren
    ("Schließe Rechner und mach das Licht aus",
     [("desktop_app_close", {"app_id": "rechner"}), ("led_power", {"state": "off"})]),
    ("Licht an und 50 %", [("led_power", {"state": "on"}), ("led_brightness", {"percent": 50})]),
    ("öffne example.org und mach lauter",
     [("desktop_open_url", {"url": "https://example.org"}), ("desktop_volume", {"action": "up"})]),
    ("Starte CS2 und mach das Licht aus", [("scripts_start", {"script_id": "cs2"}), ("led_power", {"state": "off"})]),
    # Teile, die allein nichts bedeuten, ändern nichts
    ("Hey Jarvis, mach das Licht an", [("led_power", {"state": "on"})]),
    ("öffne example.org, bitte", [("desktop_open_url", {"url": "https://example.org"})]),
    # Namen mit "und": dann gilt der ganze Satz
    ("Preset Abend und Nacht", [("led_preset", {"preset": "abend und nacht"})]),
])
def test_parse_mixed_commands(text, expected):
    assert parse(text, CFG) == expected


def test_app_named_right_after_the_verb_wins():
    from app.fallback import _find_app

    assert _find_app(CFG, "rechner editor").id == "rechner"
    assert _find_app(CFG, "editor rechner").id == "editor"


async def test_unknown_part_is_named_in_the_reply(factory):
    handler, posted = wled_mock()
    factory.device_handler = handler
    app, token = factory({"wled": {"base_url": "http://wled.test"}})
    async with client_for(app, token=token) as c:
        body = (await c.post("/api/chat", json={"message": "Licht an und koch mir einen Kaffee"})).json()
    assert posted == [{"on": True, "v": True}]
    assert body["reply"].startswith("Licht an.") and "Nicht verstanden" in body["reply"]
    assert "„koch mir einen Kaffee“" in body["reply"]
