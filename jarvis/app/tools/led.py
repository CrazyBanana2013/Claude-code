"""WLED-LED-Strip über die WLED-JSON-API (https://kno.wled.ge/interfaces/json-api/).

Verwendete Endpunkte (laut Doku):
- GET  /json/state   aktueller Zustand
- POST /json/state   Teil-Zustand setzen; mit "v": true kommt der neue Zustand zurück
- GET  /json/eff     Liste der Effektnamen (Index = Effekt-ID, "RSVD"/"-" = reserviert)
- GET  /presets.json gespeicherte Presets ({"<id>": {"n": "<Name>", ...}})
"seg" als Objekt (nicht Array) wirkt auf alle ausgewählten Segmente.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import is_todo
from app.tools.registry import Registry, ToolContext, ToolError

# Farben für LEDs abgestimmt (LED-Orange/Gelb wirken mit reinen Web-Werten zu grünlich).
COLOR_NAMES: dict[str, tuple[int, int, int]] = {
    "rot": (255, 0, 0),
    "gruen": (0, 255, 0),
    "blau": (0, 0, 255),
    "weiss": (255, 255, 255),
    "warmweiss": (255, 170, 80),
    "lila": (140, 0, 255),
    "violett": (140, 0, 255),
    "orange": (255, 90, 0),
    "pink": (255, 20, 120),
    "rosa": (255, 20, 120),
    "gelb": (255, 190, 0),
    "tuerkis": (0, 255, 200),
    "cyan": (0, 255, 255),
}

RESERVED_EFFECTS = {"RSVD", "-"}


def normalize(text: str) -> str:
    """Kleinbuchstaben, Umlaute ausschreiben, Leer- und Bindestriche entfernen."""
    text = text.strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(a, b)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[\s\-_]+", "", text)


def parse_color(value: str) -> tuple[int, int, int]:
    key = normalize(value)
    if key in COLOR_NAMES:
        return COLOR_NAMES[key]
    hex_match = re.fullmatch(r"#?([0-9a-f]{6})", key)
    if hex_match:
        h = hex_match.group(1)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    names = ", ".join(sorted(k for k in COLOR_NAMES if k not in ("violett", "rosa")))
    raise ToolError(f"Unbekannte Farbe '{value}'. Möglich: {names} oder #RRGGBB.")


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PowerParams(_Params):
    state: Literal["on", "off", "toggle"] = Field(description="on = an, off = aus, toggle = umschalten")


class BrightnessParams(_Params):
    percent: float = Field(allow_inf_nan=False, description="Helligkeit in Prozent, 0 bis 100")


class ColorParams(_Params):
    color: str | None = Field(
        None, description="Farbname (rot, grün, blau, weiß, warmweiß, lila, orange, pink, gelb) oder #RRGGBB"
    )
    r: int | None = Field(None, description="Rot 0-255 (alternativ zu color)")
    g: int | None = Field(None, description="Grün 0-255")
    b: int | None = Field(None, description="Blau 0-255")

    @model_validator(mode="after")
    def _need_color(self):
        rgb = (self.r, self.g, self.b)
        if self.color is None and None in rgb:
            raise ValueError("entweder 'color' oder alle drei Werte r, g, b angeben")
        return self


class _NameOrId(_Params):
    @field_validator("*", mode="before")
    @classmethod
    def _to_str(cls, v):
        if isinstance(v, bool):
            raise ValueError("Name oder Nummer erwartet")
        return str(v) if isinstance(v, (int, float)) else v


class EffectParams(_NameOrId):
    effect: str = Field(min_length=1, max_length=64, description="Effektname (z. B. Rainbow) oder Effekt-ID")


class PresetParams(_NameOrId):
    preset: str = Field(min_length=1, max_length=64, description="Preset-Name oder Preset-ID (1-250)")


def _base_url(ctx: ToolContext) -> str:
    url = ctx.config.wled.base_url
    if is_todo(url):
        raise ToolError("WLED ist noch nicht eingerichtet: 'wled.base_url' in config.yaml eintragen.")
    return url.rstrip("/")


async def _request(ctx: ToolContext, method: str, path: str, payload: dict | None = None):
    url = _base_url(ctx) + path
    try:
        resp = await ctx.http.request(method, url, json=payload, timeout=ctx.config.wled.timeout)
    except httpx.TimeoutException:
        raise ToolError(f"WLED antwortet nicht (Timeout nach {ctx.config.wled.timeout:g} s).") from None
    except httpx.HTTPError as exc:
        raise ToolError(f"WLED nicht erreichbar ({type(exc).__name__}).") from None
    if resp.status_code >= 400:
        raise ToolError(f"WLED meldet HTTP {resp.status_code} für {path}.")
    try:
        return resp.json()
    except ValueError:
        raise ToolError(f"WLED lieferte keine gültige JSON-Antwort für {path}.") from None


def summarize(state: dict) -> dict:
    seg = state.get("seg") or [{}]
    first = seg[0] if isinstance(seg, list) and seg else {}
    cols = first.get("col") or [[0, 0, 0]]
    color = cols[0] if cols and isinstance(cols[0], list) else None
    bri = state.get("bri", 0)
    return {
        "on": bool(state.get("on")),
        "brightness_percent": round(bri * 100 / 255) if isinstance(bri, (int, float)) else None,
        "color": color[:3] if color else None,
        "effect_id": first.get("fx"),
        "preset_id": state.get("ps"),
    }


async def _set(ctx: ToolContext, payload: dict) -> dict:
    state = await _request(ctx, "POST", "/json/state", {**payload, "v": True})
    if not isinstance(state, dict):
        raise ToolError("Unerwartete Antwort von WLED.")
    return summarize(state) if "on" in state else {"success": state.get("success", True)}


async def led_power(ctx: ToolContext, p: PowerParams) -> dict:
    value = {"on": True, "off": False, "toggle": "t"}[p.state]
    return await _set(ctx, {"on": value})


async def led_brightness(ctx: ToolContext, p: BrightnessParams) -> dict:
    percent = clamp(p.percent, 0, 100)
    if percent == 0:
        # Laut Doku besser "on": false statt bri 0.
        return await _set(ctx, {"on": False})
    bri = int(clamp(round(percent * 255 / 100), 1, 255))
    return await _set(ctx, {"on": True, "bri": bri})


async def led_color(ctx: ToolContext, p: ColorParams) -> dict:
    if p.color is not None:
        rgb = parse_color(p.color)
    else:
        rgb = tuple(int(clamp(v, 0, 255)) for v in (p.r, p.g, p.b))
    return await _set(ctx, {"on": True, "seg": {"col": [list(rgb)]}})


async def _effect_names(ctx: ToolContext) -> list[str]:
    names = await _request(ctx, "GET", "/json/eff")
    if not isinstance(names, list):
        raise ToolError("WLED lieferte keine Effektliste.")
    return [str(n) for n in names]


async def led_effect(ctx: ToolContext, p: EffectParams) -> dict:
    names = await _effect_names(ctx)
    if p.effect.strip().isdigit():
        fx = int(p.effect)
        if not 0 <= fx < len(names) or names[fx] in RESERVED_EFFECTS:
            raise ToolError(f"Effekt-ID {fx} gibt es nicht (0–{len(names) - 1}).")
    else:
        wanted = normalize(p.effect)
        matches = [i for i, n in enumerate(names) if n not in RESERVED_EFFECTS and normalize(n) == wanted]
        if not matches:
            matches = [i for i, n in enumerate(names) if n not in RESERVED_EFFECTS and wanted in normalize(n)]
        if not matches:
            raise ToolError(f"Effekt '{p.effect}' nicht gefunden.")
        fx = matches[0]
    result = await _set(ctx, {"on": True, "seg": {"fx": fx}})
    return {**result, "effect": names[fx]}


async def led_preset(ctx: ToolContext, p: PresetParams) -> dict:
    if p.preset.strip().isdigit():
        ps = int(p.preset)
        if not 1 <= ps <= 250:
            raise ToolError("Preset-IDs gehen von 1 bis 250.")
        name = None
    else:
        presets = await _request(ctx, "GET", "/presets.json")
        if not isinstance(presets, dict):
            raise ToolError("WLED lieferte keine Preset-Liste.")
        wanted = normalize(p.preset)
        found = [
            (int(k), v.get("n", ""))
            for k, v in presets.items()
            if k.isdigit() and int(k) > 0 and isinstance(v, dict) and normalize(str(v.get("n", ""))) == wanted
        ]
        if not found:
            raise ToolError(f"Preset '{p.preset}' nicht gefunden.")
        ps, name = found[0]
    result = await _set(ctx, {"ps": ps})
    return {**result, "preset": name or ps}


async def led_status(ctx: ToolContext, _p) -> dict:
    state = await _request(ctx, "GET", "/json/state")
    if not isinstance(state, dict):
        raise ToolError("Unerwartete Antwort von WLED.")
    return summarize(state)


def register(registry: Registry) -> None:
    registry.tool("led_power", "LED-Strip an- oder ausschalten oder umschalten.", PowerParams)(led_power)
    registry.tool("led_brightness", "Helligkeit des LED-Strips in Prozent (0-100) setzen.", BrightnessParams)(
        led_brightness
    )
    registry.tool("led_color", "Farbe des LED-Strips setzen (Farbname oder RGB).", ColorParams)(led_color)
    registry.tool("led_effect", "WLED-Effekt per Name oder ID aktivieren.", EffectParams)(led_effect)
    registry.tool("led_preset", "Gespeichertes WLED-Preset per Name oder ID laden.", PresetParams)(led_preset)
    registry.tool("led_status", "Aktuellen Zustand des LED-Strips lesen.")(led_status)
