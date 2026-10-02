"""Regelbasierter Parser für die Kernbefehle – funktioniert ohne LLM.

Unterstützt u. a.: "licht an/aus", "helligkeit 40", "farbe rot", "effekt rainbow",
"preset abend", "wie warm ist es", "starte/stoppe <skript>", "welche skripte laufen",
"pc ausschalten", "herunterfahren abbrechen". Mehrere LED-Befehle in einem Satz werden
kombiniert ("licht an, helligkeit 40 und farbe blau").
"""

from __future__ import annotations

import re
from typing import Any

from app.config import AppConfig
from app.tools.led import COLOR_NAMES, normalize
from app.tools.registry import Registry, run_recorded

HELP = (
    "Das habe ich nicht verstanden. Beispiele: „Licht an“, „Helligkeit 40“, „Farbe rot“, "
    "„Wie warm ist es?“, „Starte <Skript>“, „PC ausschalten“."
)

# Farbnamen, wie sie im Text vorkommen (vor normalize-Entfernung der Leerzeichen geprüft)
_COLOR_WORDS = sorted(COLOR_NAMES, key=len, reverse=True)


def _prep(text: str) -> str:
    text = text.strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(a, b)
    text = re.sub(r"[!?.,;:]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _find_script(config: AppConfig, phrase: str):
    phrase_n = normalize(re.sub(r"\b(das|den|die|mein|meine|meinen|skript|script|bitte)\b", " ", phrase))
    if not phrase_n:
        return None
    for s in config.scripts:
        if phrase_n in (normalize(s.id), normalize(s.label)):
            return s
    for s in config.scripts:
        if phrase_n in normalize(s.label) or normalize(s.id) in phrase_n:
            return s
    return None


def parse(message: str, config: AppConfig) -> list[tuple[str, dict]] | str:
    """Gibt eine Liste (tool, args) zurück oder einen Antworttext, wenn nichts passt."""
    t = _prep(message)
    words = set(t.split())

    # --- PC -------------------------------------------------------------------
    shutdown_words = re.search(r"herunterfahr|runterfahr|shutdown|ausschalt|ausmach|\baus\b", t) or re.search(
        r"\bfahr\w*\b.*\b(herunter|runter)\b", t
    )
    pc_words = re.search(r"\b(pc|rechner|computer)\b", t) or re.search(r"herunterfahr|shutdown", t)
    if pc_words and shutdown_words:
        if re.search(r"abbrech|abbruch|cancel|\bstopp?\b|nicht", t):
            return [("pc_shutdown_cancel", {})]
        return [("pc_shutdown", {})]

    # --- Skripte --------------------------------------------------------------
    if re.search(r"\b(skripte?|scripts?)\b", t) and re.search(r"welche|liste|status|laufen|zeig", t):
        return [("scripts_list", {})]
    verb = phrase = None
    m = re.match(r"^(starte|start|oeffne|stoppe|stopp|stop|beende|schliesse)\s+(.+)$", t)
    if m:
        verb, phrase = m.group(1), m.group(2)
    elif m := re.match(r"^(.+?)\s+(starten|stoppen|beenden)$", t):
        verb, phrase = m.group(2), m.group(1)
    if verb and phrase:
        script = _find_script(config, phrase)
        if script is not None:
            tool = "scripts_start" if verb.startswith(("start", "oeffne")) else "scripts_stop"
            return [(tool, {"script_id": script.id})]
        if not re.search(r"licht|led|lampe|effekt", phrase):
            known = ", ".join(s.label for s in config.scripts) or "keine"
            return f"Skript „{phrase}“ kenne ich nicht. Verfügbar: {known}."

    # --- Sensoren -------------------------------------------------------------
    if re.search(r"wie (warm|kalt)|temperatur|grad\b|luftfeucht|feuchtigkeit|sensor|raumklima", t):
        return [("sensors_read", {})]

    # --- LED ------------------------------------------------------------------
    led_context = re.search(r"\b(licht|led|leds|lampe|strip|beleuchtung)\b", t)
    actions: list[tuple[str, dict]] = []
    if led_context and re.search(r"\b(status|zustand)\b|^ist (das )?(licht|led)", t):
        return [("led_status", {})]

    if led_context and re.search(r"\b(aus|ausschalten|ausmachen)\b", t):
        return [("led_power", {"state": "off"})]
    if led_context and re.search(r"\b(umschalten|toggle)\b", t):
        actions.append(("led_power", {"state": "toggle"}))
    elif led_context and re.search(r"\b(an|ein|anmachen|einschalten|anschalten)\b", t):
        actions.append(("led_power", {"state": "on"}))

    m = re.search(r"(helligkeit|hell|dimme?n?|dimm)\D{0,12}?(\d{1,3})\s*(%|prozent)?", t) or (
        re.search(r"(\d{1,3})\s*(%|prozent)", t) if led_context else None
    )
    if m:
        number = next(g for g in m.groups() if g and g.isdigit())
        actions.append(("led_brightness", {"percent": int(number)}))

    color = None
    hex_m = re.search(r"#[0-9a-f]{6}\b", t)
    if hex_m:
        color = hex_m.group(0)
    else:
        compact = t.replace(" ", "")
        for word in _COLOR_WORDS:
            if re.search(rf"\b{word}\b", t) or (word == "warmweiss" and "warmweiss" in compact):
                color = word
                break
    if color and (led_context or "farbe" in words or len(words) <= 2):
        actions.append(("led_color", {"color": color}))

    m = re.search(r"\beffekt\s+(.+)$", t)
    if m:
        actions.append(("led_effect", {"effect": m.group(1).strip()}))
    m = re.search(r"\b(preset|szene)\s+(.+)$", t)
    if m:
        actions.append(("led_preset", {"preset": m.group(2).strip()}))

    return actions or HELP


def _describe(record: dict) -> str:
    tool, res = record["tool"], record.get("result") or {}
    if not record["ok"]:
        return record.get("error", "Fehler.")
    if tool == "led_power":
        return {"on": "Licht an.", "off": "Licht aus.", "toggle": "Licht umgeschaltet."}[record["args"]["state"]]
    if tool == "led_brightness":
        pct = max(0, min(100, record["args"]["percent"]))
        return "Licht aus." if pct == 0 else f"Helligkeit {pct} %."
    if tool == "led_color":
        return f"Farbe {record['args'].get('color', 'gesetzt')}."
    if tool == "led_effect":
        return f"Effekt {res.get('effect', record['args']['effect'])}."
    if tool == "led_preset":
        return f"Preset {res.get('preset', record['args']['preset'])}."
    if tool == "led_status":
        if not res.get("on"):
            return "Das Licht ist aus."
        return f"Das Licht ist an, Helligkeit {res.get('brightness_percent')} %."
    if tool == "sensors_read":
        parts = []
        for s in res.get("sensors", []):
            if s.get("error"):
                parts.append(f"{s['name']}: {s['error']}")
            else:
                value = f"{s['value']:.1f}".replace(".", ",")
                parts.append(f"{s['name']}: {value} {s['unit']}".strip())
        return " · ".join(parts) or res.get("message", "Keine Sensoren konfiguriert.")
    if tool == "scripts_start":
        return f"{res.get('label', 'Skript')} gestartet."
    if tool == "scripts_stop":
        if res.get("status") == "not_running":
            return f"{res.get('label', 'Skript')} lief nicht."
        return f"{res.get('label', 'Skript')} gestoppt."
    if tool == "scripts_list":
        items = [f"{s['label']}: {'läuft' if s['running'] else 'aus'}" for s in res.get("scripts", [])]
        return " · ".join(items) or "Keine Skripte konfiguriert."
    if tool == "pc_shutdown":
        return "Bitte das Herunterfahren in der Oberfläche bestätigen."
    if tool == "pc_shutdown_cancel":
        return res.get("message", "Herunterfahren abgebrochen.")
    return "Erledigt."


async def handle(message: str, registry: Registry) -> dict[str, Any]:
    parsed = parse(message, registry.ctx.config)
    if isinstance(parsed, str):
        return {"reply": parsed, "tool_calls": [], "source": "fallback"}
    records = [await run_recorded(registry, tool, args, source="fallback") for tool, args in parsed]
    reply = " ".join(_describe(r) for r in records)
    return {"reply": reply, "tool_calls": records, "source": "fallback"}
