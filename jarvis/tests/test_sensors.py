import httpx
import pytest

from app.tools.sensors import legacy_object_id
from tests.conftest import client_for

pytestmark = pytest.mark.anyio

SENSORS = {
    "sensors": [
        {"name": "Temperatur", "type": "esphome_rest", "base_url": "http://esp.test",
         "entity_id": "BME280 Temperature", "unit": "°C"},
        {"name": "Luftfeuchte", "type": "esphome_rest", "base_url": "http://esp.test",
         "entity_id": "BME280 Humidity", "unit": "%"},
        {"name": "Kaputt", "type": "esphome_rest", "base_url": "http://down.test",
         "entity_id": "x", "unit": ""},
        {"name": "Leer", "type": "esphome_rest", "base_url": "TODO", "entity_id": "TODO", "unit": ""},
    ]
}


def handler(request: httpx.Request):
    assert request.method == "GET"
    if request.url.host == "down.test":
        raise httpx.ConnectTimeout("timeout")
    path = request.url.path
    if path == "/sensor/BME280 Temperature":
        return httpx.Response(200, json={"id": "sensor/BME280 Temperature", "state": "21.4 °C", "value": 21.437})
    if path == "/sensor/bme280_humidity":  # alte Firmware: nur object_id
        return httpx.Response(200, json={"id": "sensor-bme280_humidity", "state": "45 %", "value": 45.0})
    return httpx.Response(404)


async def test_read_all_with_individual_errors(factory):
    factory.device_handler = handler
    app, token = factory(SENSORS)
    async with client_for(app, token=token) as c:
        r = await c.post("/api/tools/sensors_read")
    assert r.status_code == 200
    by_name = {s["name"]: s for s in r.json()["result"]["sensors"]}
    assert by_name["Temperatur"]["value"] == 21.4
    assert by_name["Temperatur"]["unit"] == "°C"
    assert by_name["Temperatur"]["timestamp"]
    assert by_name["Temperatur"]["error"] is None
    assert by_name["Luftfeuchte"]["value"] == 45.0
    assert "Timeout" in by_name["Kaputt"]["error"]
    assert by_name["Kaputt"]["value"] is None
    assert "nicht eingerichtet" in by_name["Leer"]["error"]
    # Nur GET-Anfragen, nie gegen TODO-Hosts
    assert all(req.method == "GET" for req in factory.device_requests)
    assert all("TODO" not in str(req.url) for req in factory.device_requests)


async def test_nan_and_404(factory):
    def h(request):
        if "nan" in request.url.path:
            # ESPHome/ArduinoJson kann NaN unquotiert senden – das ist kein striktes JSON.
            return httpx.Response(200, content=b'{"id":"sensor/nan","state":"NA","value":NaN}')
        return httpx.Response(404)

    factory.device_handler = h
    app, token = factory({"sensors": [
        {"name": "A", "base_url": "http://e.test", "entity_id": "nan", "unit": "°C"},
        {"name": "B", "base_url": "http://e.test", "entity_id": "fehlt", "unit": "°C"},
    ]})
    async with client_for(app, token=token) as c:
        sensors = (await c.post("/api/tools/sensors_read")).json()["result"]["sensors"]
    assert "kein Messwert" in sensors[0]["error"]
    assert "404" in sensors[1]["error"]


def test_legacy_object_id():
    assert legacy_object_id("BME280 Temperature") == "bme280_temperature"
    assert legacy_object_id("Zimmer-Temperatur (°C)") == "zimmer_temperatur_c"
