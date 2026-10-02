"""Zimmersensoren über austauschbare Adapter lesen.

Adapter `esphome_rest` nutzt die REST-API des ESPHome-Webservers (Komponente `web_server:`):
GET /sensor/<entity_name>  →  {"id": "sensor/<name>", "state": "21.4 °C", "value": 21.4}
Ab ESPHome 2026.x ist <entity_name> der Name aus der YAML (URL-kodiert); ältere Firmware nutzt
die object_id (z. B. bme280_temperature). Bei 404 wird deshalb die object_id-Form probiert.
"""

from __future__ import annotations

import asyncio
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import quote

import httpx

from app.config import SensorConfig, is_todo
from app.tools.registry import Registry, ToolContext

SENSOR_TIMEOUT = 3.0


class SensorReadError(Exception):
    pass


def legacy_object_id(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


async def read_esphome_rest(http: httpx.AsyncClient, cfg: SensorConfig) -> float:
    base = cfg.base_url.rstrip("/")
    candidates = [cfg.entity_id]
    legacy = legacy_object_id(cfg.entity_id)
    if legacy and legacy != cfg.entity_id:
        candidates.append(legacy)
    resp = None
    for entity in candidates:
        url = f"{base}/sensor/{quote(entity, safe='')}"
        try:
            resp = await http.get(url, timeout=SENSOR_TIMEOUT)
        except httpx.TimeoutException:
            raise SensorReadError(f"Timeout nach {SENSOR_TIMEOUT:g} s") from None
        except httpx.HTTPError as exc:
            raise SensorReadError(f"nicht erreichbar ({type(exc).__name__})") from None
        if resp.status_code != 404:
            break
    assert resp is not None
    if resp.status_code == 404:
        raise SensorReadError(
            f"Entität '{cfg.entity_id}' nicht gefunden (404) – Name in config.yaml und "
            "'web_server:' in der ESPHome-Config prüfen"
        )
    if resp.status_code >= 400:
        raise SensorReadError(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise SensorReadError("keine gültige JSON-Antwort") from None
    value = data.get("value") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
        raise SensorReadError("kein Messwert vorhanden")
    return float(value)


Adapter = Callable[[httpx.AsyncClient, SensorConfig], Awaitable[float]]
ADAPTERS: dict[str, Adapter] = {"esphome_rest": read_esphome_rest}


async def _read_one(ctx: ToolContext, cfg: SensorConfig) -> dict:
    entry = {"name": cfg.name, "value": None, "unit": cfg.unit, "timestamp": None, "error": None}
    if is_todo(cfg.base_url) or is_todo(cfg.entity_id):
        entry["error"] = "noch nicht eingerichtet (base_url/entity_id in config.yaml eintragen)"
        return entry
    adapter = ADAPTERS.get(cfg.type)
    if adapter is None:
        entry["error"] = f"unbekannter Sensortyp '{cfg.type}'"
        return entry
    try:
        value = await asyncio.wait_for(adapter(ctx.http, cfg), timeout=SENSOR_TIMEOUT + 1)
    except SensorReadError as exc:
        entry["error"] = str(exc)
    except asyncio.TimeoutError:
        entry["error"] = f"Timeout nach {SENSOR_TIMEOUT:g} s"
    except Exception as exc:  # ein kaputter Sensor darf die Antwort nie kippen
        entry["error"] = f"Fehler: {type(exc).__name__}"
    else:
        entry["value"] = round(value, 1)
        entry["timestamp"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return entry


async def sensors_read(ctx: ToolContext, _p) -> dict:
    sensors = ctx.config.sensors
    if not sensors:
        return {"sensors": [], "message": "Keine Sensoren konfiguriert."}
    results = await asyncio.gather(*(_read_one(ctx, s) for s in sensors))
    return {"sensors": list(results)}


def register(registry: Registry) -> None:
    registry.tool(
        "sensors_read",
        "Liest alle Zimmersensoren (z. B. Temperatur, Luftfeuchte) mit Wert, Einheit und Zeitstempel.",
    )(sensors_read)
