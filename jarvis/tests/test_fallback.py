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
