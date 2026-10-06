import json

import httpx
import pytest

from app.tools.led import parse_color
from app.tools.registry import ToolError
from tests.conftest import client_for

pytestmark = pytest.mark.anyio

WLED = {"wled": {"base_url": "http://wled.test"}}
EFFECTS = ["Solid", "Blink", "Breathe", "RSVD", "Rainbow", "Fire 2012"]
PRESETS = {"0": {}, "1": {"n": "Abend"}, "3": {"n": "Gaming Modus"}}


def wled_mock(state=None):
    state = state or {"on": True, "bri": 128, "ps": -1, "seg": [{"col": [[255, 0, 0]], "fx": 0}]}
    posted = []

    def handler(request: httpx.Request):
        path = request.url.path
        if request.method == "GET" and path == "/json/state":
            return httpx.Response(200, json=state)
        if request.method == "GET" and path == "/json/eff":
            return httpx.Response(200, json=EFFECTS)
        if request.method == "GET" and path == "/presets.json":
            return httpx.Response(200, json=PRESETS)
        if request.method == "POST" and path == "/json/state":
            body = json.loads(request.content)
            posted.append(body)
            new = {**state, **{k: v for k, v in body.items() if k in ("on", "bri", "ps")}}
            return httpx.Response(200, json=new)
        return httpx.Response(404)

    return handler, posted


async def call(factory, tool, args=None, overrides=WLED):
    handler, posted = wled_mock()
    factory.device_handler = handler
    app, token = factory(overrides)
    async with client_for(app, token=token) as c:
        r = await c.post(f"/api/tools/{tool}", json=args or {})
    return r, posted


async def test_power_on_off_toggle(factory):
    for state, expected in (("on", True), ("off", False), ("toggle", "t")):
        r, posted = await call(factory, "led_power", {"state": state})
        assert r.status_code == 200, r.text
        assert posted == [{"on": expected, "v": True}]


async def test_brightness_is_scaled_and_clamped(factory):
    r, posted = await call(factory, "led_brightness", {"percent": 40})
    assert posted[-1] == {"on": True, "bri": 102, "v": True}
    r, posted = await call(factory, "led_brightness", {"percent": 250})
    assert posted[-1]["bri"] == 255
    r, posted = await call(factory, "led_brightness", {"percent": 0.1})
    assert posted[-1]["bri"] == 1
    r, posted = await call(factory, "led_brightness", {"percent": -5})
    assert posted[-1] == {"on": False, "v": True}


async def test_brightness_rejects_text(factory):
    r, posted = await call(factory, "led_brightness", {"percent": "hell"})
    assert r.status_code == 422
    assert posted == []


async def test_color_names_and_rgb(factory):
    r, posted = await call(factory, "led_color", {"color": "Grün"})
    assert posted[-1]["seg"] == {"col": [[0, 255, 0]]}
    r, posted = await call(factory, "led_color", {"color": "warmweiss"})
    assert posted[-1]["seg"]["col"][0] == list(parse_color("warmweiß"))
    r, posted = await call(factory, "led_color", {"r": 300, "g": -1, "b": 10})
    assert posted[-1]["seg"] == {"col": [[255, 0, 10]]}
    r, posted = await call(factory, "led_color", {"color": "#00ff88"})
    assert posted[-1]["seg"] == {"col": [[0, 255, 136]]}


async def test_unknown_color_and_missing_values(factory):
    r, posted = await call(factory, "led_color", {"color": "kariert"})
    assert r.status_code == 400 and "Unbekannte Farbe" in r.json()["error"]
    r, posted = await call(factory, "led_color", {"r": 10})
    assert r.status_code == 422
    assert posted == []


@pytest.mark.parametrize("name", ["rot", "grün", "blau", "weiß", "warmweiß", "lila", "orange", "pink", "gelb"])
def test_all_required_color_names(name):
    assert len(parse_color(name)) == 3


async def test_effect_by_name_and_id(factory):
    r, posted = await call(factory, "led_effect", {"effect": "rainbow"})
    assert posted[-1]["seg"] == {"fx": 4}
    assert r.json()["result"]["effect"] == "Rainbow"
    r, posted = await call(factory, "led_effect", {"effect": 2})
    assert posted[-1]["seg"] == {"fx": 2}
    r, posted = await call(factory, "led_effect", {"effect": "3"})  # RSVD
    assert r.status_code == 400 and posted == []
    r, posted = await call(factory, "led_effect", {"effect": "Disco"})
    assert r.status_code == 400 and posted == []


async def test_preset_by_name_and_id(factory):
    r, posted = await call(factory, "led_preset", {"preset": "gaming modus"})
    assert posted[-1] == {"ps": 3, "v": True}
    r, posted = await call(factory, "led_preset", {"preset": 7})
    assert posted[-1] == {"ps": 7, "v": True}
    r, posted = await call(factory, "led_preset", {"preset": "999"})
    assert r.status_code == 400 and posted == []


async def test_status(factory):
    r, posted = await call(factory, "led_status")
    assert r.json()["result"] == {
        "on": True, "brightness_percent": 50, "color": [255, 0, 0], "effect_id": 0, "preset_id": -1,
    }
    assert posted == []


async def test_not_configured(factory):
    r, _ = await call(factory, "led_power", {"state": "on"}, overrides=None)
    assert r.status_code == 400
    assert "noch nicht eingerichtet" in r.json()["error"]


async def test_unreachable(factory):
    def boom(request):
        raise httpx.ConnectError("down")

    factory.device_handler = boom
    app, token = factory(WLED)
    async with client_for(app, token=token) as c:
        r = await c.post("/api/tools/led_status")
    assert r.status_code == 400 and "nicht erreichbar" in r.json()["error"]


def test_parse_color_error():
    with pytest.raises(ToolError):
        parse_color("")


@pytest.mark.parametrize("base_url", ["http://192.168.1.300", "http://10.0.0.1000"])
async def test_invalid_wled_address_is_a_clear_tool_error(factory, base_url):
    """Von Hand eingetragene, ungültige IP: verständlicher Fehler statt HTTP 500 (httpx.InvalidURL)."""
    r, posted = await call(factory, "led_power", {"state": "on"}, overrides={"wled": {"base_url": base_url}})
    assert r.status_code == 400, r.text
    assert "Ungültige WLED-Adresse" in r.json()["error"]
    assert posted == []
