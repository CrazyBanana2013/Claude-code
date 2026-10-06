"""Regelbasierter Parser für die Kernbefehle – funktioniert ohne LLM.

Unterstützt u. a.: "licht an/aus", "helligkeit 40", "farbe rot", "effekt rainbow",
"preset abend", "wie warm ist es", "starte/stoppe <skript>", "welche skripte laufen",
"pc ausschalten", "herunterfahren abbrechen", "lauter"/"leiser"/"lautstärke 30"/"stumm"/"ton an",
"pause"/"weiter"/"nächster titel"/"vorheriger titel", "pc sperren", "öffne <domain oder url>",
"starte/schließe <programm>" (Skripte haben Vorrang), "was ist auf dem bildschirm".
Mehrere Befehle in einem Satz werden Teil für Teil ausgeführt ("licht an, helligkeit 40 und farbe blau",
"licht aus und pc sperren"); nicht verstandene Teile nennt die Antwort.
"""

from __future__ import annotations

import re
from typing import Any

from app.config import AppConfig
from app.tools.led import COLOR_NAMES, normalize
from app.tools.registry import Registry, run_recorded

HELP = (
    "Das habe ich nicht verstanden. Beispiele: „Licht an“, „Helligkeit 40“, „Farbe rot“, "
    "„Wie warm ist es?“, „Starte <Skript>“, „Lauter“, „Pause“, „PC sperren“, „PC ausschalten“."
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


def _find_app(config: AppConfig, phrase: str):
    phrase_n = normalize(re.sub(r"\b(das|den|die|mein|meine|meinen|programm|app|fenster|bitte)\b", " ", phrase))
    if not phrase_n:
        return None
    apps = config.desktop.apps
    for a in apps:
        if phrase_n in (normalize(a.id), normalize(a.label)):
            return a
    for a in apps:
        if phrase_n in normalize(a.label):
            return a
    # Sonst das Programm, das am weitesten vorne steht (direkt nach dem Verb) – nicht das erste der Liste.
    hits = [(pos, i, a) for i, a in enumerate(apps) for key in {normalize(a.id), normalize(a.label)}
            if key and (pos := phrase_n.find(key)) >= 0]
    return min(hits, key=lambda h: (h[0], h[1]))[2] if hits else None


# "öffne youtube.com", "geh auf https://…", "ruf wikipedia.org auf" – auf dem Originaltext, weil
# _prep Punkte und Doppelpunkte entfernt.
_URL_REQUEST = re.compile(
    r"^\s*(?:bitte\s+)?(?:öffne|oeffne|geh(?:e)?\s+(?:auf|zu)|ruf(?:e)?)\s+(?:mir\s+)?(?:(?:die|den)\s+)?"
    r"(?:(?:web)?seite\s+|website\s+|url\s+|adresse\s+|link\s+)?(\S+)"
    r"(?:\s+(?:im|in\s+dem|mit\s+dem)\s+browser)?(?:\s+auf)?(?:\s+bitte)?\s*[.!?]*\s*$",
    re.I,
)
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)
_DOMAIN_LIKE = re.compile(r"^[\w-]+(?:\.[\w-]+)*\.([a-z]{2,63})(?::\d{1,5})?(?:[/?#]\S*)?$", re.I)
# Dateiendungen, die wie eine Top-Level-Domain aussehen – das sind Programme, keine Webseiten.
_FILE_SUFFIXES = {"exe", "bat", "cmd", "ps1", "lnk", "msi", "txt", "dll", "vbs", "py", "ahk"}


def _url_request(message: str) -> str | None:
    m = _URL_REQUEST.match(message)
    if not m:
        return None
    target = m.group(1).rstrip(".,;!?")
    if _SCHEME.match(target) or target.startswith("\\\\") or "\\" in target:
        return target  # desktop_open_url prüft und lehnt alles außer http/https ab
    d = _DOMAIN_LIKE.match(target)
    if d and d.group(1).lower() not in _FILE_SUFFIXES:
        return "https://" + target
    return None


_SCREEN_WORDS = {
    "was", "ist", "auf", "dem", "den", "der", "die", "das", "meinem", "meinen", "mein", "deinem", "bildschirm",
    "monitor", "screen", "beschreibe", "beschreib", "beschreiben", "zeig", "zeige", "mir", "siehst", "du",
    "sieht", "man", "zu", "sehen", "gerade", "jetzt", "bitte", "los", "an", "da", "im", "am", "gibt", "es",
    "passiert", "laeuft", "kannst", "mal", "drauf", "darauf",
}


def _screen_args(message: str, t: str) -> dict:
    """Allgemeine Frage → {}; spezielle Frage ("was steht in der Fehlermeldung …") wird weitergegeben."""
    if set(t.split()) - _SCREEN_WORDS:
        return {"question": message.strip()[:300]}
    return {}


def _volume(t: str) -> dict | None:
    if re.search(r"\bunmute\b|\bton (wieder )?(an|ein)\b|\bton einschalten\b|stumm aus\b|"
                 r"nicht (mehr )?stumm|stummschaltung (aus|aufheben|beenden)", t):
        return {"action": "unmute"}
    if re.search(r"\bstumm\b|stummschalt|\bmute\b|\bton (aus|ausschalten|ausmachen|ab)\b", t):
        return {"action": "mute"}
    m = re.search(r"(lautstaerke|volume|\blaut\b)\D{0,12}?(\d{1,3})\s*(%|prozent)?", t) or re.search(
        r"\b(\d{1,3})\s*(%|prozent)?\s*(lautstaerke|volume)\b", t
    )
    if m:
        number = next(g for g in m.groups() if g and g.isdigit())
        return {"action": "set", "percent": int(number)}
    if re.search(r"\blauter\b", t):
        return {"action": "up"}
    if re.search(r"\bleiser\b", t):
        return {"action": "down"}
    if re.search(r"\bwie laut\b|^(lautstaerke|volume)$", t):
        return {"action": "get"}
    return None


def _media(t: str) -> str | None:
    media = r"(musik|wiedergabe|video|lied|song|titel|track|player)"
    if re.search(rf"^(stopp?e?|stop|halte?)\s+(die |das |den )?{media}( an)?$", t) or re.search(
        rf"^{media} (stoppen|anhalten|beenden|aus)$", t
    ):
        return "stop"
    if re.search(r"(naechste[nrsm]?|next) (titel|lied|song|track)|\bskip\b|ueberspring|^naechste[nrsm]?$", t):
        return "next"
    if re.search(r"(vorherige[nrsm]?|letzte[nrsm]?|vorige[nrsm]?|previous) (titel|lied|song|track)|"
                 r"(titel|lied|song|track) zurueck", t):
        return "previous"
    if re.search(rf"\bpause\b|\bpausier\w*|\bplay\b|^(mach )?weiter( bitte)?$|^(abspielen|fortsetzen)$|"
                 rf"{media} (abspielen|fortsetzen|weiter|an)$", t):
        return "play_pause"
    return None


# Teilsätze: "Licht aus und PC sperren", "Licht aus, Pause", "Schließe Rechner und starte Editor".
# Nur an Leerraum/Komma-mit-Leerraum – Adressen enthalten keine Leerzeichen und bleiben ganz.
_CLAUSE_SPLIT = re.compile(r"\s*[,;]\s+|\s+(?:und|sowie|dann|danach|anschließend|anschliessend)\s+", re.I)
_LEADING_FILLER = re.compile(r"^(?:(?:und|dann|danach|bitte|auch|noch|außerdem|ausserdem)\s+)+", re.I)
_FILLER_WORDS = {"bitte", "danke", "jarvis", "mal", "jetzt", "sofort", "gleich", "noch", "auch", "ok", "okay",
                 "hey", "hallo", "hi", "und", "dann", "danach"}
_LED_CONTEXT = re.compile(r"\b(licht|led|leds|lampe|strip|beleuchtung)\b")


def _clauses(message: str) -> list[str]:
    parts = [_LEADING_FILLER.sub("", p.strip()) for p in _CLAUSE_SPLIT.split(message)]
    return [p for p in parts if set(_prep(p).split()) - _FILLER_WORDS]


def plan(message: str, config: AppConfig) -> tuple[list[tuple[str, dict]] | str, list[str]]:
    """Wie parse(), zusätzlich die Teilsätze, die nicht verstanden wurden (für einen Hinweis in der Antwort).

    Sätze mit mehreren Teilen werden Teil für Teil gelesen ("Licht aus und PC sperren" = beides). Ein Teil
    ohne eigenen Bezug erbt das Licht ("Licht an und 50 %"). Passt ein Teil gar nicht, gilt der ganze Satz
    wie bisher (z. B. Preset-Namen mit "und") – und was dann noch fehlt, nennt die Antwort.
    """
    whole = _parse_one(message, config)
    clauses = _clauses(message)
    if len(clauses) < 2:
        return whole, []
    led_context = bool(_LED_CONTEXT.search(_prep(message)))
    actions: list[tuple[str, dict]] = []
    missed: list[str] = []
    for clause in clauses:
        result = _parse_one(clause, config)
        if isinstance(result, str) and led_context and not _LED_CONTEXT.search(_prep(clause)):
            inherited = _parse_one("licht " + clause, config)
            result = inherited if not isinstance(inherited, str) else result
        if isinstance(result, str):
            missed.append(clause.strip(" .!?"))
            continue
        for item in result:
            if item not in actions:
                actions.append(item)
    if not missed:
        return actions, []
    if not actions or (isinstance(whole, list) and whole != actions):
        return whole, []  # der ganze Satz ergibt mehr Sinn (z. B. "Preset Abend und Nacht")
    return actions, missed


def parse(message: str, config: AppConfig) -> list[tuple[str, dict]] | str:
    """Gibt eine Liste (tool, args) zurück oder einen Antworttext, wenn nichts passt."""
    return plan(message, config)[0]


def _parse_one(message: str, config: AppConfig) -> list[tuple[str, dict]] | str:
    t = _prep(message)
    words = set(t.split())

    # --- Webseite öffnen (vor allem anderen: "öffne <domain>" ist kein Programm) --------
    url = _url_request(message)
    if url is not None:
        return [("desktop_open_url", {"url": url})]

    # --- PC sperren -----------------------------------------------------------
    if re.search(r"\bsperr", t) and re.search(r"\b(pc|rechner|computer|bildschirm|windows|laptop)\b", t):
        return [("desktop_lock", {})]

    # --- PC -------------------------------------------------------------------
    shutdown_words = re.search(r"herunterfahr|runterfahr|shutdown|ausschalt|ausmach|\baus\b", t) or re.search(
        r"\bfahr\w*\b.*\b(herunter|runter)\b", t
    )
    pc_words = re.search(r"\b(pc|rechner|computer)\b", t) or re.search(r"herunterfahr|shutdown", t)
    if pc_words and shutdown_words:
        if re.search(r"abbrech|abbruch|cancel|\bstopp?\b|nicht", t):
            return [("pc_shutdown_cancel", {})]
        return [("pc_shutdown", {})]

    # --- Bildschirm beschreiben --------------------------------------------------
    if re.search(r"\b(bildschirm|monitor|screen)\b", t) and re.search(
        r"\b(was|beschreib\w*|zeig\w*|siehst|sehen|steht|erkennst)\b", t
    ):
        return [("screen_describe", _screen_args(message, t))]

    # --- Lautstärke und Medientasten ---------------------------------------------
    volume = _volume(t)
    if volume is not None:
        return [("desktop_volume", volume)]
    media = _media(t)
    if media is not None:
        return [("desktop_media", {"action": media})]

    # --- Skripte und Programme --------------------------------------------------
    if re.search(r"\b(skripte?|scripts?)\b", t) and re.search(r"welche|liste|status|laufen|zeig", t):
        return [("scripts_list", {})]
    if re.search(r"\b(programme|apps)\b", t) and re.search(r"welche|liste|status|laufen|zeig", t):
        return [("desktop_apps_list", {})]
    m = re.match(r"^(?:wechsle|wechsel|fokussiere)\s+(?:zu[mr]?\s+|auf\s+)?(.+)$", t) or re.match(
        r"^(?:hol|hole)\s+(.+?)\s+(?:nach vorne|in den vordergrund)$", t
    )
    if m and (app := _find_app(config, m.group(1))) is not None:
        return [("desktop_focus", {"app_id": app.id})]
    verb = phrase = None
    m = re.match(r"^(starte|start|oeffne|stoppe|stopp|stop|beende|schliesse|schliess)\s+(.+)$", t)
    if m:
        verb, phrase = m.group(1), m.group(2)
    elif m := re.match(r"^(.+?)\s+(starten|stoppen|beenden|schliessen|oeffnen)$", t):
        verb, phrase = m.group(2), m.group(1)
    if verb and phrase:
        starting = verb.startswith(("start", "oeffne"))
        script = _find_script(config, phrase)
        if script is not None:
            tool = "scripts_start" if starting else "scripts_stop"
            return [(tool, {"script_id": script.id})]
        app = _find_app(config, phrase)
        if app is not None:
            return [("desktop_app_start" if starting else "desktop_app_close", {"app_id": app.id})]
        if not re.search(r"licht|led|lampe|effekt", phrase):
            known = ", ".join(s.label for s in config.scripts) or "keine"
            programs = ", ".join(a.label for a in config.desktop.apps) or "keine"
            return f"„{phrase}“ kenne ich nicht. Skripte: {known}. Programme: {programs}."

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
    if tool == "desktop_volume":
        if res.get("muted"):
            return f"Ton aus (Lautstärke {res.get('percent')} %)."
        return f"Lautstärke {res.get('percent')} %."
    if tool == "desktop_media":
        return {
            "play_pause": "Wiedergabe umgeschaltet.",
            "next": "Nächster Titel.",
            "previous": "Vorheriger Titel.",
            "stop": "Wiedergabe gestoppt.",
        }.get(res.get("action"), "Erledigt.")
    if tool == "desktop_app_close" and res.get("status") == "confirm_required":
        return f"Bitte das Schließen von {res.get('label', 'dem Programm')} in der Oberfläche bestätigen."
    if tool == "desktop_apps_list":
        items = [f"{a['label']}: {'läuft' if a['running'] else 'aus'}" for a in res.get("apps", [])]
        return " · ".join(items) or "Keine Programme freigegeben."
    if tool == "screen_describe":
        return res.get("description", "Keine Beschreibung.")
    if tool.startswith("desktop_") and res.get("message"):
        return res["message"]
    return "Erledigt."


async def handle(message: str, registry: Registry) -> dict[str, Any]:
    parsed, missed = plan(message, registry.ctx.config)
    if isinstance(parsed, str):
        return {"reply": parsed, "tool_calls": [], "source": "fallback"}
    records = [await run_recorded(registry, tool, args, source="fallback") for tool, args in parsed]
    reply = " ".join(_describe(r) for r in records)
    if missed:
        parts = ", ".join(f"„{m}“" for m in missed)
        reply += f" Nicht verstanden (bitte einzeln sagen): {parts}."
    return {"reply": reply, "tool_calls": records, "source": "fallback"}
