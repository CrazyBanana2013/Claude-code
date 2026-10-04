"""Einrichtungsassistent für JARVIS: config.yaml ausfüllen, API-Token anlegen, Infos für den Installer.

Aufruf im Installationsordner mit dem Python der virtuellen Umgebung:
    python -m app.setup_wizard configure --config PATH [--example PATH] [--non-interactive]
    python -m app.setup_wizard token --secrets PATH
    python -m app.setup_wizard info --config PATH

configure  Exit 0 = gespeichert oder unverändert gelassen, 1 = abgebrochen (Datei unberührt), 2 = Fehler.
token      Legt secrets.yaml mit einem neuen Token an, falls noch keiner existiert (zeigt ihn einmal an).
info       Eine Zeile JSON: {"bind", "port", "local_url", "warnings", "desktop_enabled", "vision_model",
           "tts_enabled", "stt_enabled", "stt_model", "stt_model_size"} (vision_model = null, solange TODO).

Grundsätze:
- Geräte (WLED, ESPHome) werden höchstens lesend abgefragt (GET), Ollama nur über GET /api/tags und
  POST /api/show. Es wird nichts geschaltet und kein Modell gezogen.
- Das CS2-Skript wird nie geöffnet oder gelesen – es wird nur geprüft, ob die Datei existiert. Dasselbe
  gilt für Programme der PC-Steuerung (desktop.apps): nur Existenzprüfung, nie starten.
- Es wird nichts heruntergeladen: Zusatzpaket und Whisper-Modell für die Spracheingabe lädt erst der
  Installer, und nur nach Zustimmung.
- Eine vorhandene config.yaml wird vor dem Überschreiben nach config.yaml.bak gesichert und atomar
  ersetzt. Alle Werte, die der Assistent nicht abfragt, bleiben erhalten.
"""

from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import math
import ntpath
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import quote, urlsplit

import httpx
import yaml

from pydantic import ValidationError

from app.auth import load_or_create_token
from app.config import (
    DEFAULT_NETWORKS,
    PROJECT_DIR,
    ConfigError,
    DesktopAppConfig,
    config_warnings,
    is_todo,
    load_config,
    parse_config,
)
from app.ollama_models import ModelInfo, OllamaUnavailable, list_models, tool_models, vision_models
from app.tools.screen import is_local_url
from app.tools.sensors import legacy_object_id
from app.voice.stt import APPROX_SIZES

PROBE_TIMEOUT = 3.0
OLLAMA_TIMEOUT = 10.0
SKIP = "-"
CS2_ID = "cs2"

TODO_WLED = "http://TODO-WLED-IP"
TODO_ESPHOME = "http://TODO-ESPHOME-IP"
TODO_CWD = "TODO_ORDNER_DES_CS2_SKRIPTS"
TODO_COMMAND = ["TODO_PROGRAMM", "TODO_ARGUMENT"]
TODO_VISION = "TODO_VISIONMODELL"
TOTAL_STEPS = 8
TOKEN_EXISTS_MESSAGE = "Token vorhanden (steht in secrets.yaml)"

EXIT_OK, EXIT_ABORTED, EXIT_ERROR = 0, 1, 2


# --------------------------------------------------------------------------------------------
# Ein-/Ausgabe (austauschbar für Tests)
# --------------------------------------------------------------------------------------------


class WizardIO(Protocol):
    def ask(self, prompt: str) -> str: ...

    def say(self, text: str = "") -> None: ...


class ConsoleIO:
    """Konsole: input()/print(). Strg+C bzw. Dateiende lösen KeyboardInterrupt/EOFError aus."""

    def ask(self, prompt: str) -> str:
        return input(prompt)

    def say(self, text: str = "") -> None:
        print(text, flush=True)


# --------------------------------------------------------------------------------------------
# Standardwerte
# --------------------------------------------------------------------------------------------


def builtin_defaults() -> dict:
    """Wie config.example.yaml – Rückfall, falls die Beispieldatei fehlt (ein Test hält beide gleich)."""
    data = {
        "server": {"bind": "0.0.0.0", "port": 8765, "allowed_networks": list(DEFAULT_NETWORKS)},
        "llm": {
            "enabled": True,
            "force_fallback": False,
            "base_url": "http://127.0.0.1:11434",
            "model": "TODO_MODELLNAME",
            "keep_alive": "2m",
            "timeout": 60,
            "temperature": 0.2,
            "max_tool_rounds": 4,
            "think": None,
        },
        "wled": {"base_url": TODO_WLED, "timeout": 3},
        "sensors": [
            {"name": "Temperatur", "type": "esphome_rest", "base_url": TODO_ESPHOME,
             "entity_id": "TODO_ENTITAET_TEMPERATUR", "unit": "°C"},
            {"name": "Luftfeuchte", "type": "esphome_rest", "base_url": TODO_ESPHOME,
             "entity_id": "TODO_ENTITAET_LUFTFEUCHTE", "unit": "%"},
        ],
        "scripts": [
            {"id": CS2_ID, "label": "CS2-Skript", "cwd": TODO_CWD, "command": list(TODO_COMMAND),
             "single_instance": True, "hide_window": False},
        ],
    }
    return parse_config(data).model_dump()


def load_existing(path: Path) -> dict:
    """Vorhandene config.yaml als vollständiges dict (alle Felder). Wirft ConfigError mit Klartext.

    Kodierungs- und Lesefehler übersetzt load_config selbst (gleiche Meldung wie beim Serverstart).
    """
    return load_config(path).model_dump()


def load_defaults(example: Path | None) -> tuple[dict, str | None]:
    """Standardwerte aus config.example.yaml, sonst eingebaute. Zweiter Wert: Hinweis für den User."""
    if example is not None and example.is_file():
        try:
            return load_existing(example), None
        except ConfigError as exc:
            note = f"{example.name} ist ungültig ({exc}) – nutze eingebaute Standardwerte."
            return builtin_defaults(), note
    return builtin_defaults(), None


def default_example_path(config_path: Path) -> Path:
    candidate = config_path.parent / "config.example.yaml"
    return candidate if candidate.is_file() else PROJECT_DIR / "config.example.yaml"


def todo_entity(sensor_name: str) -> str:
    suffix = legacy_object_id(sensor_name).upper()
    return f"TODO_ENTITAET_{suffix}" if suffix else "TODO_ENTITAET"


# --------------------------------------------------------------------------------------------
# config.yaml schreiben (kommentiert, Reihenfolge wie config.example.yaml)
# --------------------------------------------------------------------------------------------

_HEADER = [
    "# JARVIS – Konfiguration (config.yaml)",
    "# Geschrieben vom Einrichtungsassistenten (python -m app.setup_wizard configure).",
    "# Darf von Hand bearbeitet werden (UTF-8). In \"...\" steht ein Backslash doppelt: \"C:\\\\Pfad\".",
    "# Werte, die \"TODO\" enthalten, gelten als \"noch nicht eingerichtet\": Der Server startet trotzdem,",
    "# das betroffene Tool meldet dann eine verständliche Fehlermeldung.",
]

_KEY_ORDER: dict[str, list[str]] = {
    "": ["server", "llm", "wled", "sensors", "scripts", "desktop", "vision", "voice"],
    "server": ["bind", "port", "allowed_networks"],
    "llm": ["enabled", "force_fallback", "base_url", "model", "keep_alive", "timeout", "temperature",
            "max_tool_rounds", "think"],
    "wled": ["base_url", "timeout"],
    "sensors[]": ["name", "type", "base_url", "entity_id", "unit"],
    "scripts[]": ["id", "label", "cwd", "command", "single_instance", "hide_window"],
    "desktop": ["enabled", "allow_open_url", "allowed_domains", "apps"],
    "desktop.apps[]": ["id", "label", "command", "process_name", "window_title"],
    "vision": ["model", "max_side", "jpeg_quality", "monitor", "timeout"],
    "voice": ["tts", "stt"],
    "voice.tts": ["enabled", "speak_replies", "voice", "rate", "volume"],
    "voice.stt": ["enabled", "model", "device", "compute_type", "language", "max_seconds", "download_root"],
}

# Kommentarzeilen über einem Schlüssel (exakter Pfad, Listenindex mit [n]).
_COMMENT_ABOVE: dict[str, list[str]] = {
    "server.allowed_networks": [
        "Nur Anfragen aus diesen Netzen werden angenommen, alles andere bekommt 403.",
    ],
    "llm.model": ["Kleinstes installiertes Modell mit Tool-Support, siehe README (\"Modell auswählen\")."],
    "sensors[0].entity_id": [
        "Name der Entität genau wie in der ESPHome-YAML (neuere ESPHome-Versionen);",
        "ältere Firmware nutzt die object_id (z. B. bme280_temperature) – wird automatisch probiert.",
    ],
    "scripts[0].command": [
        "Argumentliste, KEIN Shell-String. Beispiele:",
        "  [\"C:\\\\Pfad\\\\python.exe\", \"skript.py\"]",
        "  [\"powershell.exe\", \"-NoProfile\", \"-ExecutionPolicy\", \"Bypass\", \"-File\", \"skript.ps1\"]",
    ],
    "desktop": [
        "PC-Steuerung: nur diese festen Aktionen (Lautstärke, Medientasten, PC sperren, Webseite öffnen,",
        "Programme aus der Liste unten starten/schließen/nach vorne holen). Es gibt bewusst KEINE freie",
        "Maus-/Tastatursteuerung, kein Tippen von Text und keine Befehlszeile (siehe README \"Sicherheit\").",
    ],
    "desktop.allowed_domains": [
        "Leer = alle Domains. Sonst nur diese Domains und ihre Subdomains, z. B. [\"youtube.com\", \"wikipedia.org\"]",
    ],
    "desktop.apps": [
        "Feste Programmliste – das LLM kann nur diese IDs benutzen. command = Argumentliste (kein",
        "Shell-String), process_name = Dateiname des laufenden Prozesses (zum Erkennen und Schließen),",
        "window_title = Teil des Fenstertitels (für \"nach vorne holen\"). Beispiele: config.example.yaml.",
    ],
    "vision": [
        "\"Bildschirm beschreiben\": Ein Bildschirmfoto geht NUR an das lokale Ollama (llm.base_url muss",
        "127.0.0.1/localhost sein), wird nicht gespeichert und nie ans Handy geschickt.",
    ],
    "vision.model": [
        "Ollama-Modell mit capability \"vision\" (ollama show <name>); darf gleich llm.model sein –",
        "ein eigenes Modell braucht zusätzlich VRAM.",
    ],
    "voice.tts": ["Sprachausgabe über die PC-Lautsprecher (Windows-Sprachausgabe, offline)"],
    "voice.stt": [
        "Spracherkennung (Mikrofon-Button) lokal mit Whisper – braucht das Extra \"voice\"",
        "und lädt das Modell einmalig herunter (Modell \"small\" etwa 500 MB).",
    ],
}

# Kommentar am Zeilenende (Listenindex als []).
_COMMENT_INLINE: dict[str, str] = {
    "server.bind": "0.0.0.0 = LAN + Tailscale (IPv4). Niemals per Router freigeben!",
    "llm.force_fallback": "true = nie Ollama fragen, nur den Regel-Parser nutzen",
    "llm.keep_alive": "danach gibt Ollama den VRAM wieder frei",
    "llm.timeout": "Sekunden für die gesamte Chat-Anfrage",
    "llm.think": "bei Thinking-Modellen (z. B. qwen3) auf false setzen = schneller",
    "wled.base_url": "z. B. http://<IP des WLED-ESP32>",
    "scripts[].hide_window": "true = ohne Konsolenfenster starten (nur Windows)",
    "desktop.allow_open_url": "Webseiten im Standardbrowser öffnen (nur http/https)",
    "vision.max_side": "längste Bildseite in Pixeln (wird verkleinert)",
    "vision.monitor": "1 = Hauptbildschirm, 2 = zweiter ..., 0 = alle zusammen",
    "vision.timeout": "Sekunden für die Beschreibung",
    "voice.tts.speak_replies": "Antworten im Chat vorlesen (in der Oberfläche umschaltbar)",
    "voice.tts.voice": "\"\" = erste installierte deutsche Stimme",
    "voice.tts.rate": "-10 (langsam) bis 10 (schnell)",
    "voice.tts.volume": "0 bis 100",
    "voice.stt.max_seconds": "längste Aufnahme in Sekunden",
    "voice.stt.download_root": "\"\" = state\\whisper",
}

_VALUE_COMMENT = {"100.64.0.0/10": "Tailscale (CGNAT-Bereich)"}
_BLOCK_LISTS = {"server.allowed_networks"}
_COMMENT_COLUMN = 27

# Zeichen, die JSON ungeschützt lässt, YAML in "..." aber nicht roh erlaubt (oder als Umbruch liest).
_YAML_UNSAFE = re.compile("[\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff\ud800-\udfff]")
_PLAIN_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


def yaml_str(value: str) -> str:
    """String als YAML-Skalar in doppelten Anführungszeichen (JSON-Escapes sind gültiges YAML)."""
    text = json.dumps(value, ensure_ascii=False)
    return _YAML_UNSAFE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return ".nan"
        if math.isinf(value):
            return ".inf" if value > 0 else "-.inf"
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        text = repr(value)
        mantissa, e, exponent = text.partition("e")
        if e and "." not in mantissa:  # PyYAML erkennt 1e+16 nur als 1.0e+16 als Zahl
            text = f"{mantissa}.0e{exponent}"
        return text
    if isinstance(value, str):
        return yaml_str(value)
    raise TypeError(f"nicht darstellbar: {type(value).__name__}")


def _key(key: Any) -> str:
    key = str(key)
    return key if _PLAIN_KEY.fullmatch(key) else yaml_str(key)


def _flow(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_flow(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{yaml_str(str(k))}: {_flow(v)}" for k, v in value.items()) + "}"
    return _scalar(value)


def _generic(path: str) -> str:
    return re.sub(r"\[\d+\]", "[]", path)


def _with_comment(text: str, comment: str | None) -> str:
    if not comment:
        return text
    if len(text) < _COMMENT_COLUMN - 1:
        return f"{text.ljust(_COMMENT_COLUMN - 1)} # {comment}"
    return f"{text}  # {comment}"


def _ordered(mapping: Mapping, order: list[str]) -> list:
    return [k for k in order if k in mapping] + [k for k in mapping if k not in order]


def _emit(lines: list[str], indent: int, key: Any, value: Any, path: str) -> None:
    pad = " " * indent
    for comment in _COMMENT_ABOVE.get(path, ()):
        lines.append(f"{pad}# {comment}")
    inline = _COMMENT_INLINE.get(_generic(path))
    head = f"{pad}{_key(key)}:"
    nested_list = isinstance(value, list) and value and (
        _generic(path) in _BLOCK_LISTS or any(isinstance(v, (dict, list)) for v in value)
    )
    if isinstance(value, dict) and value:
        lines.append(_with_comment(head, inline))
        _emit_mapping(lines, indent + 2, value, path)
    elif nested_list:
        lines.append(_with_comment(head, inline))
        for i, item in enumerate(value):
            _emit_item(lines, indent + 2, item, f"{path}[{i}]")
    else:
        lines.append(_with_comment(f"{head} {_flow(value)}", inline))


def _emit_mapping(lines: list[str], indent: int, mapping: Mapping, path: str) -> None:
    order = _KEY_ORDER.get(_generic(path), [])
    for k in _ordered(mapping, order):
        _emit(lines, indent, k, mapping[k], f"{path}.{k}" if path else str(k))


def _emit_item(lines: list[str], indent: int, item: Any, path: str) -> None:
    pad = " " * indent
    if isinstance(item, dict) and item:
        sub: list[str] = []
        _emit_mapping(sub, indent + 2, item, path)
        lines.append(f"{pad}- {sub[0][indent + 2:]}")
        lines.extend(sub[1:])
    elif isinstance(item, list) and item and any(isinstance(v, (dict, list)) for v in item):
        lines.append(f"{pad}- {_flow(item)}")
    else:
        comment = _VALUE_COMMENT.get(item) if isinstance(item, str) else None
        lines.append(_with_comment(f"{pad}- {_flow(item)}", comment))


def render_config(data: dict) -> str:
    """Rendert die Konfiguration als kommentiertes YAML (Reihenfolge wie config.example.yaml)."""
    lines = list(_HEADER)
    for key in _ordered(data, _KEY_ORDER[""]):
        lines.append("")
        _emit(lines, 0, key, data[key], str(key))
    return "\n".join(lines) + "\n"


def checked_render(data: dict) -> str:
    """Prüft mit parse_config, rendert und stellt sicher, dass das Ergebnis exakt zurückgelesen wird."""
    expected = parse_config(data, "Neue Konfiguration").model_dump()
    text = render_config(expected)
    reread = parse_config(yaml.safe_load(text), "Neue Konfiguration").model_dump()
    if reread != expected:  # Schutz gegen Fehler im Renderer – lieber gar nicht schreiben
        raise ConfigError("Interner Fehler: Die erzeugte config.yaml würde Werte verändern. Nichts gespeichert.")
    return text


def write_atomic(path: Path, text: str) -> None:
    """Schreibt in eine temporäre Datei im selben Ordner und ersetzt dann in einem Schritt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():  # Rechte der bisherigen Datei behalten (mkstemp legt 0600 an)
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


# --------------------------------------------------------------------------------------------
# Adressen und lesende Proben
# --------------------------------------------------------------------------------------------

_LABEL = r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
_HOST_RE = re.compile(rf"(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*\.?")


_SCHEME_TYPO = re.compile(r"(?i)^https?(?::/?(?!/)|/)")


def _is_ipv6(text: str) -> bool:
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return True


def normalize_base_url(raw: str) -> str:
    """'<IP>', 'wled.local:8080', 'http://<host>/json' → 'http://<host>[:port]'. ValueError mit Klartext."""
    text = strip_quotes(raw.strip())
    if not text:
        raise ValueError("Keine Adresse eingegeben.")
    if any(c.isspace() for c in text):
        raise ValueError("Die Adresse darf keine Leerzeichen enthalten.")
    if "\\" in text:
        raise ValueError("Bitte / statt \\ verwenden, z. B. http://<adresse>.")
    if "://" not in text:
        if _SCHEME_TYPO.match(text):  # 'http//…', 'http:/…', 'https//…' – lieber nachfragen als raten
            raise ValueError("Bitte als http://<adresse> eingeben (mit Doppelpunkt und zwei Schrägstrichen).")
        if "%" in text:
            raise ValueError("IPv6-Adressen mit Zonen-ID (%…) werden nicht unterstützt.")
        if _is_ipv6(text):  # IPv6 ohne eckige Klammern, z. B. fd00::5
            text = f"[{text}]"
        text = "http://" + text
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Nur http:// oder https:// möglich, nicht '{parts.scheme}://'.")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Bitte ohne Benutzername/Passwort angeben.")
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        raise ValueError("Ungültiger Port (erlaubt: 1 bis 65535).") from None
    if port == 0:
        raise ValueError("Ungültiger Port (erlaubt: 1 bis 65535).")
    if ":" in host:  # IPv6 in eckigen Klammern
        if "%" in host:
            raise ValueError("IPv6-Adressen mit Zonen-ID (%…) werden nicht unterstützt.")
        if not _is_ipv6(host):
            raise ValueError(f"'{host}' ist keine gültige IPv6-Adresse.")
        netloc = f"[{host}]"
    else:
        if not host or not _HOST_RE.fullmatch(host):
            raise ValueError("Kein gültiger Hostname oder keine gültige IP-Adresse erkennbar.")
        if host in ("http", "https"):
            raise ValueError("Bitte als http://<adresse> eingeben (mit Doppelpunkt und zwei Schrägstrichen).")
        if re.fullmatch(r"[0-9.]+", host):  # sieht nach IPv4 aus – dann muss es auch eine gültige sein
            try:
                ipaddress.IPv4Address(host)  # lehnt auch führende Nullen ab (192.168.001.050)
            except ValueError:
                raise ValueError(
                    f"'{host}' ist keine gültige IP-Adresse (vier Zahlen von 0 bis 255, ohne führende Nullen)."
                ) from None
        netloc = host
    if port is not None:
        netloc += f":{port}"
    return f"{scheme}://{netloc}"


_AUTH_MESSAGE = "Der ESPHome-Webserver verlangt ein Passwort (auth:) – das unterstützt JARVIS noch nicht."


@dataclass
class ProbeResult:
    ok: bool
    message: str
    reachable: bool = True


def _get(http: httpx.Client, url: str) -> httpx.Response | ProbeResult:
    try:
        return http.get(url, timeout=PROBE_TIMEOUT)
    except httpx.TimeoutException:
        return ProbeResult(False, f"Keine Antwort nach {PROBE_TIMEOUT:g} s ({url}) – Gerät aus oder Adresse falsch?",
                           reachable=False)
    except httpx.ConnectError:
        return ProbeResult(False, f"Nicht erreichbar: {url} – Verbindung abgelehnt bzw. Gerät aus oder Adresse falsch?",
                           reachable=False)
    except httpx.HTTPError as exc:
        return ProbeResult(False, f"Nicht erreichbar ({type(exc).__name__}): {url}", reachable=False)
    except (httpx.InvalidURL, ValueError, UnicodeError) as exc:  # z. B. von Hand eingetragene 192.168.1.300
        return ProbeResult(False, f"Ungültige Adresse: {url} ({exc})", reachable=False)


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def probe_wled(http: httpx.Client, base: str) -> ProbeResult:
    """Nur lesend: GET <base>/json/info."""
    resp = _get(http, f"{base.rstrip('/')}/json/info")
    if isinstance(resp, ProbeResult):
        return resp
    if resp.status_code != 200:
        return ProbeResult(False, f"HTTP {resp.status_code} von /json/info – ist das ein WLED-Gerät?")
    info = _json(resp)
    if not isinstance(info, dict):
        return ProbeResult(False, "Keine gültige JSON-Antwort von /json/info – ist das ein WLED-Gerät?")
    leds = info.get("leds") if isinstance(info.get("leds"), dict) else {}
    return ProbeResult(
        True,
        f"WLED gefunden: Name '{info.get('name', '?')}', Version {info.get('ver', '?')}, "
        f"{leds.get('count', '?')} LEDs.",
    )


def probe_esphome_device(http: httpx.Client, base: str) -> ProbeResult:
    """Nur lesend: GET <base>/ – antwortet dort der ESPHome-Webserver?"""
    resp = _get(http, f"{base.rstrip('/')}/")
    if isinstance(resp, ProbeResult):
        return resp
    if resp.status_code == 401:
        return ProbeResult(False, _AUTH_MESSAGE)
    if resp.status_code >= 400:
        return ProbeResult(
            False,
            f"HTTP {resp.status_code} – kein ESPHome-Webserver? In der ESPHome-YAML muss 'web_server:' "
            "(z. B. port: 80) aktiv sein; das Gerät danach selbst neu flashen.",
        )
    return ProbeResult(True, f"ESPHome-Webserver antwortet (HTTP {resp.status_code}).")


def _sensor_value(resp: httpx.Response) -> tuple[Any, Any]:
    data = _json(resp)
    if not isinstance(data, dict):
        return None, None
    return data.get("value"), data.get("state")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isnan(value)


def _describe_value(value: Any, state: Any) -> str:
    if not _is_number(value):
        return "Sensor gefunden, liefert aber gerade keinen Messwert."
    text = f"Wert: {value}"
    if isinstance(state, str) and state:
        text += f" (Anzeige: '{state}')"
    return text


def probe_esphome_sensor(http: httpx.Client, base: str, entity: str) -> ProbeResult:
    """Nur lesend: GET <base>/sensor/<Name> – bei 404 zusätzlich die object_id-Form (ältere Firmware)."""
    base = base.rstrip("/")
    resp = _get(http, f"{base}/sensor/{quote(entity, safe='')}")
    if isinstance(resp, ProbeResult):
        return resp
    legacy = legacy_object_id(entity)
    if resp.status_code == 404 and legacy and legacy != entity:
        alt = _get(http, f"{base}/sensor/{quote(legacy, safe='')}")
        if isinstance(alt, httpx.Response) and alt.status_code == 200:
            value, state = _sensor_value(alt)
            return ProbeResult(
                True,
                f"Gefunden unter der object_id '{legacy}' (ältere ESPHome-Firmware) – JARVIS probiert "
                f"diese Form automatisch. {_describe_value(value, state)}",
            )
    if resp.status_code == 404:
        return ProbeResult(
            False,
            "Sensor nicht gefunden (HTTP 404). Den Namen genau wie in der ESPHome-YAML angeben "
            "(name: ..., Groß-/Kleinschreibung beachten); ältere Firmware erwartet die object_id "
            f"(z. B. '{legacy or 'bme280_temperature'}'). In der ESPHome-YAML muss außerdem "
            "'web_server:' aktiv sein.",
        )
    if resp.status_code == 401:
        return ProbeResult(False, _AUTH_MESSAGE)
    if resp.status_code >= 400:
        return ProbeResult(False, f"HTTP {resp.status_code} beim Lesen des Sensors.")
    value, state = _sensor_value(resp)
    return ProbeResult(True, f"OK. {_describe_value(value, state)}")


# --------------------------------------------------------------------------------------------
# CS2-Skript: Startbefehl vorschlagen (Datei wird nie geöffnet)
# --------------------------------------------------------------------------------------------

_WIN_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _pathmod(path: str):
    """ntpath für Windows-Pfade (auch beim Testen unter Linux), sonst os.path."""
    return ntpath if _WIN_ABS.match(path) or os.name == "nt" else os.path


def strip_quotes(text: str) -> str:
    """Entfernt ein umschließendes Paar "..." oder '...' (z. B. von 'Als Pfad kopieren')."""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'" and text[0] not in text[1:-1]:
        return text[1:-1]
    return text


def clean_path(raw: str) -> str:
    text = strip_quotes(raw.strip()).strip()
    if text.startswith("~"):
        text = os.path.expanduser(text)
    if "%" in text:
        text = ntpath.expandvars(text)
    return text


def absolute_path(path: str) -> str:
    if _WIN_ABS.match(path):
        return ntpath.normpath(path)
    return os.path.abspath(path)  # berührt die Datei nicht (kein resolve())


_AHK_RELATIVE = [
    r"AutoHotkey\v2\AutoHotkey64.exe",
    r"AutoHotkey\v2\AutoHotkey.exe",
    r"AutoHotkey\v2\AutoHotkey32.exe",
    r"AutoHotkey\AutoHotkey.exe",
    r"AutoHotkey\AutoHotkeyU64.exe",
]


def find_autohotkey(
    *, is_file: Callable[[str], bool], which: Callable[[str], str | None], env: Mapping[str, str]
) -> str | None:
    roots: list[str] = []
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if env.get(var):
            roots.append(env[var])
    if env.get("LOCALAPPDATA"):
        roots.append(ntpath.join(env["LOCALAPPDATA"], "Programs"))
    seen: set[str] = set()
    for root in roots:
        if root.lower() in seen:
            continue
        seen.add(root.lower())
        for rel in _AHK_RELATIVE:
            candidate = ntpath.join(root, rel)
            if is_file(candidate):
                return candidate
    for name in ("AutoHotkey64.exe", "AutoHotkey.exe"):
        found = which(name)
        if found:
            return found
    return None


@dataclass
class Suggestion:
    cwd: str
    command: list[str] | None
    notes: list[str] = field(default_factory=list)
    kind: str = ""


def suggest_command(
    path: str,
    *,
    is_file: Callable[[str], bool] = os.path.isfile,
    which: Callable[[str], str | None] = shutil.which,
    env: Mapping[str, str] | None = None,
) -> Suggestion:
    """Arbeitsordner = Ordner der Datei; Befehl je nach Endung. Liest die Datei nicht."""
    env = os.environ if env is None else env
    pm = _pathmod(path)
    folder = pm.dirname(path) or "."
    ext = pm.splitext(path)[1].lower()
    notes: list[str] = []
    command: list[str] | None
    if ext in (".py", ".pyw"):
        launcher = "pyw" if ext == ".pyw" else "py"
        command = [launcher, "-3", path]
        if which(launcher) is None:
            notes.append(
                f"Der Python-Launcher '{launcher}' wurde nicht gefunden. Dann den Vorschlag ablehnen und "
                "als Programm den vollständigen Pfad zu python.exe angeben."
            )
        else:
            notes.append(
                f"Alternative: statt '{launcher} -3' den vollständigen Pfad zu einer bestimmten python.exe "
                "angeben (z. B. aus der venv des Skripts) – dazu den Vorschlag ablehnen."
            )
    elif ext == ".ps1":
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]
    elif ext == ".ahk":
        ahk = find_autohotkey(is_file=is_file, which=which, env=env)
        command = [ahk, path] if ahk else None
        if ahk:
            notes.append(f"AutoHotkey gefunden: {ahk} (für ein v1-Skript ggf. die v1-Version angeben).")
        else:
            notes.append("AutoHotkey wurde in den üblichen Ordnern nicht gefunden.")
    elif ext == ".exe":
        command = [path]
    elif ext in (".bat", ".cmd"):
        # "call" vorn: Der Text nach /c beginnt dann nicht mit einem Anführungszeichen, cmd lässt die
        # Anführungszeichen um den Pfad stehen – Pfade wie "C:\Program Files (x86)\..." funktionieren.
        # /d: keine AutoRun-Befehle aus der Registry.
        command = ["cmd.exe", "/d", "/c", "call", path]
        quoted = any(c.isspace() for c in path)  # nur dann setzt Windows den Pfad in Anführungszeichen
        if any(c in path for c in "%^!") or (not quoted and any(c in path for c in "&()")):
            notes.append(
                "Der Pfad enthält Sonderzeichen wie % ^ ! (bzw. & ( ) ohne Leerzeichen im Pfad), die cmd.exe "
                "auswertet – falls der Start nicht klappt, die Datei in einen Ordner ohne diese Zeichen legen."
            )
    else:
        command = None
        notes.append(f"Unbekannte Dateiendung '{ext or '(keine)'}' – Programm und Argumente selbst eingeben.")
    return Suggestion(cwd=folder, command=command, notes=notes, kind=ext)


def format_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


# --------------------------------------------------------------------------------------------
# Der interaktive Assistent
# --------------------------------------------------------------------------------------------


def _display(value: Any) -> str:
    if is_todo(value):
        return "nicht eingerichtet"
    if isinstance(value, list):
        return format_command([str(v) for v in value])
    return str(value)


def _header(step: int, title: str) -> str:
    return f"\n--- Schritt {step}/{TOTAL_STEPS}: {title} ---"


def _skip_hint(configured: bool) -> str:
    """Bedeutung von Enter und '-' – je nachdem, ob schon ein Wert eingerichtet ist (wie beim CS2-Skript)."""
    if configured:
        return "Enter = unverändert lassen, '-' = auf TODO zurücksetzen (nicht eingerichtet)."
    return "Enter bzw. '-' = überspringen (bleibt TODO)."


class Wizard:
    """Fragt die wichtigsten Werte ab und ändert `self.data` (vollständiges Config-dict)."""

    def __init__(
        self,
        io: WizardIO,
        http: httpx.Client,
        data: dict,
        defaults: dict,
        *,
        is_file: Callable[[str], bool] = os.path.isfile,
        is_dir: Callable[[str], bool] = os.path.isdir,
        which: Callable[[str], str | None] = shutil.which,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.io = io
        self.http = http
        self.data = copy.deepcopy(data)
        self.defaults = defaults
        self.is_file = is_file
        self.is_dir = is_dir
        self.which = which
        self.env = os.environ if env is None else env
        self.probe = True
        self._unreachable: set[str] = set()
        self._models: list[ModelInfo] | None = None
        self._models_error: str | None = None

    # --- kleine Helfer ---------------------------------------------------------------------

    def say(self, text: str = "") -> None:
        self.io.say(text)

    def ask(self, label: str, current: Any = None, *, show: str | None = None) -> str:
        shown = show if show is not None else (_display(current) if current is not None else "")
        prompt = f"{label} [{shown}]: " if shown else f"{label}: "
        return self.io.ask(prompt).strip()

    def confirm(self, label: str, default: bool) -> bool:
        hint = "J/n" if default else "j/N"
        while True:
            answer = self.io.ask(f"{label} [{hint}]: ").strip().lower()
            if not answer:
                return default
            if answer in ("j", "ja", "y", "yes"):
                return True
            if answer in ("n", "nein", "no"):
                return False
            self.say("  Bitte 'j' für ja oder 'n' für nein eingeben.")

    # --- Ablauf ----------------------------------------------------------------------------

    def run(self) -> dict:
        self.probe = self.confirm(
            "Geräte während der Einrichtung kurz testen (nur lesend, es wird nichts geschaltet)?", True
        )
        self.step_wled()
        self.step_esphome()
        self.step_script()
        self.step_model()
        self.step_port()
        self.step_vision()
        self.step_desktop()
        self.step_voice()
        return self.data

    # --- a) WLED ---------------------------------------------------------------------------

    def step_wled(self) -> None:
        wled = self.data["wled"]
        self.say(_header(1, "WLED (LED-Strip)"))
        self.say("IP-Adresse (z. B. aus der WLED-App), Hostname oder URL des WLED-Controllers.")
        self.say(_skip_hint(not is_todo(wled["base_url"])))
        while True:
            raw = self.ask("WLED-Adresse", wled["base_url"])
            if raw == SKIP or (not raw and is_todo(wled["base_url"])):
                if not is_todo(wled["base_url"]):
                    wled["base_url"] = TODO_WLED
                    self.say("  WLED ist jetzt nicht eingerichtet (TODO).")
                else:
                    self.say("  WLED bleibt nicht eingerichtet.")
                return
            url = self._parse_url(raw, wled["base_url"])
            if url is None:
                continue
            if not self.probe:
                wled["base_url"] = url
                return
            self.say(f"  Teste (nur lesend) GET {url}/json/info ...")
            result = probe_wled(self.http, url)
            self.say(f"  {result.message}")
            # Bisherigen Wert behalten ist Vorgabe (Gerät evtl. nur gerade aus), neue Eingaben nicht.
            if result.ok or self.confirm("Adresse trotzdem übernehmen?", url == wled["base_url"]):
                wled["base_url"] = url
                return

    def _parse_url(self, raw: str, current: str) -> str | None:
        if not raw:
            return current
        try:
            url = normalize_base_url(raw)
        except ValueError as exc:
            self.say(f"  {exc}")
            return None
        if url != raw:
            self.say(f"  -> {url}")
        return url

    # --- b) ESPHome ------------------------------------------------------------------------

    def step_esphome(self) -> None:
        self.say(_header(2, "ESPHome-Sensoren"))
        sensors = self.data["sensors"]
        created = not sensors
        if created:
            self.say("In der Konfiguration sind keine Sensoren eingetragen. Vorschlag: Temperatur, Luftfeuchte.")
            sensors = copy.deepcopy(self.defaults.get("sensors", [])[:2]) or copy.deepcopy(
                builtin_defaults()["sensors"]
            )
        managed = sensors[:2]
        names = " und ".join(f"'{s['name']}'" for s in managed)
        self.say(f"Ein ESPHome-Gerät liefert normalerweise beide Werte ({names}).")
        configured = any(not is_todo(s["base_url"]) for s in managed)
        self.say("Adresse: IP, Hostname oder URL. " + _skip_hint(configured))

        bases = {s["base_url"] for s in managed if not is_todo(s["base_url"])}
        if len(bases) <= 1:
            current = next(iter(bases), managed[0]["base_url"])
            base = self._ask_device("ESPHome-Gerät", current)
            for s in managed:
                if not (is_todo(base) and is_todo(s["base_url"])):
                    s["base_url"] = base
        else:
            for s in managed:
                s["base_url"] = self._ask_device(f"ESPHome-Gerät für '{s['name']}'", s["base_url"])

        if all(is_todo(s["base_url"]) for s in managed):
            self.say("  ESPHome ist jetzt nicht eingerichtet (TODO)." if configured else "  ESPHome bleibt nicht eingerichtet.")
            return
        self.say("Entitätsname = 'name:' des Sensors genau wie in der ESPHome-YAML,")
        self.say("z. B. BME280 Temperature (Anführungszeichen sind nicht nötig).")
        for s in managed:
            if not is_todo(s["base_url"]):
                self._ask_entity(s)
        if created:
            self.data["sensors"] = sensors

    def _ask_device(self, label: str, current: str) -> str:
        while True:
            raw = self.ask(label, current)
            if raw == SKIP or (not raw and is_todo(current)):
                return current if is_todo(current) else TODO_ESPHOME
            url = self._parse_url(raw, current)
            if url is None:
                continue
            if not self.probe:
                return url
            self.say(f"  Teste (nur lesend) GET {url}/ ...")
            result = probe_esphome_device(self.http, url)
            self.say(f"  {result.message}")
            if result.ok:
                self._unreachable.discard(url)
                return url
            if self.confirm("Adresse trotzdem übernehmen?", url == current):
                if not result.reachable:
                    self._unreachable.add(url)
                return url

    def _ask_entity(self, sensor: dict) -> None:
        while True:
            raw = strip_quotes(self.ask(f"Entitätsname für '{sensor['name']}'", sensor["entity_id"]))
            if raw == SKIP or (not raw and is_todo(sensor["entity_id"])):
                if not is_todo(sensor["entity_id"]):
                    sensor["entity_id"] = todo_entity(sensor["name"])
                    self.say(f"  '{sensor['name']}' ist jetzt nicht eingerichtet (TODO).")
                else:
                    self.say(f"  '{sensor['name']}' bleibt nicht eingerichtet.")
                return
            entity = raw or sensor["entity_id"]
            if not self.probe or sensor["base_url"] in self._unreachable:
                sensor["entity_id"] = entity
                return
            self.say(f"  Teste (nur lesend) GET {sensor['base_url']}/sensor/{quote(entity, safe='')} ...")
            result = probe_esphome_sensor(self.http, sensor["base_url"], entity)
            self.say(f"  {result.message}")
            if result.ok or self.confirm("Namen trotzdem übernehmen?", entity == sensor["entity_id"]):
                sensor["entity_id"] = entity
                return

    # --- c) CS2-Skript ---------------------------------------------------------------------

    def _script_entry(self) -> tuple[int | None, dict]:
        scripts = self.data["scripts"]
        for i, s in enumerate(scripts):
            if s.get("id") == CS2_ID:
                return i, copy.deepcopy(s)
        template = next((s for s in self.defaults.get("scripts", []) if s.get("id") == CS2_ID), None)
        if template is None:
            template = builtin_defaults()["scripts"][0]
        return None, copy.deepcopy(template)

    def _store_script(self, index: int | None, entry: dict) -> None:
        if index is None:
            self.data["scripts"].append(entry)
        else:
            self.data["scripts"][index] = entry

    def step_script(self) -> None:
        self.say(_header(3, "CS2-Skript"))
        index, entry = self._script_entry()
        configured = not is_todo(entry["cwd"]) and not is_todo(entry["command"])
        if configured:
            self.say(f"Aktuell: Arbeitsordner {entry['cwd']}")
            self.say(f"         Befehl      {format_command(entry['command'])}")
        self.say("Pfad zur Skript- oder Programmdatei (.py, .ps1, .ahk, .exe, .bat, .cmd).")
        self.say("Die Datei wird nur auf Existenz geprüft, nie geöffnet oder verändert.")
        self.say("Tipp: Im Explorer Umschalt + Rechtsklick auf die Datei -> 'Als Pfad kopieren'.")
        if configured:
            self.say("Enter = unverändert lassen, '-' = Eintrag auf TODO zurücksetzen.")
        else:
            self.say("Enter = überspringen (bleibt TODO).")

        while True:
            shown = "unverändert" if configured else "überspringen"
            raw = clean_path(self.ask("Pfad zum CS2-Skript", show=shown))
            if raw == SKIP and configured:
                entry["cwd"], entry["command"] = TODO_CWD, list(TODO_COMMAND)
                self._store_script(index, entry)
                self.say("  CS2-Skript ist jetzt nicht eingerichtet (TODO).")
                return
            if not raw or raw == SKIP:
                self.say("  CS2-Skript bleibt " + ("unverändert." if configured else "nicht eingerichtet."))
                return
            path = absolute_path(raw)
            if self.is_file(path):
                break
            self.say(f"  Datei nicht gefunden: {path}")

        suggestion = suggest_command(path, is_file=self.is_file, which=self.which, env=self.env)
        for note in suggestion.notes:
            self.say(f"  Hinweis: {note}")
        if suggestion.command is None and suggestion.kind == ".ahk":
            ahk = self._ask_autohotkey()
            if ahk:
                suggestion.command = [ahk, path]

        result: tuple[str, list[str]] | None = None
        if suggestion.command:
            self.say("Vorschlag:")
            self.say(f"  Arbeitsordner: {suggestion.cwd}")
            self.say(f"  Befehl:        {format_command(suggestion.command)}")
            if self.confirm("Vorschlag übernehmen?", True):
                result = (suggestion.cwd, suggestion.command)
        if result is None:
            result = self._manual_command(suggestion.cwd)
        if result is None:
            self.say("  Keine Eingabe – CS2-Skript bleibt unverändert.")
            return
        entry["cwd"], entry["command"] = result
        entry["hide_window"] = self.confirm(
            "Ohne Konsolenfenster starten (hide_window, Ausgabe dann in state\\logs\\cs2.log)?",
            bool(entry.get("hide_window", False)),
        )
        self._store_script(index, entry)

    def _ask_autohotkey(self) -> str | None:
        while True:
            raw = clean_path(self.ask("Pfad zu AutoHotkey.exe (leer = Befehl selbst eingeben)"))
            if not raw:
                return None
            path = absolute_path(raw)
            if self.is_file(path):
                return path
            self.say(f"  Datei nicht gefunden: {path}")

    def _manual_command(self, default_cwd: str) -> tuple[str, list[str]] | None:
        self.say("Programm und Argumente eingeben, eins pro Zeile (Anführungszeichen sind nicht nötig).")
        self.say("Leere Zeile = fertig.")
        parts: list[str] = []
        while True:
            label = "  Programm" if not parts else f"  Argument {len(parts)}"
            line = self.io.ask(f"{label}: ").strip()
            if not line:
                break
            parts.append(strip_quotes(line))
        if not parts:
            return None
        program = parts[0]
        if not _WIN_ABS.match(program) and not os.path.isabs(program) and self.which(program) is None:
            self.say(f"  Hinweis: '{program}' wurde im PATH nicht gefunden – ggf. vollständigen Pfad angeben.")
        raw_cwd = clean_path(self.ask("Arbeitsordner", show=default_cwd))
        cwd = absolute_path(raw_cwd) if raw_cwd else default_cwd
        if not self.is_dir(cwd):
            self.say(f"  Hinweis: Ordner nicht gefunden: {cwd} – bitte prüfen.")
        return cwd, parts

    # --- d) Ollama-Modell ------------------------------------------------------------------

    def step_model(self) -> None:
        llm = self.data["llm"]
        self.say(_header(4, "Sprachmodell (Ollama)"))
        models = self._ollama_models()
        if models is None:
            self.say(f"  {self._models_error}.")
            self._explain_no_model(llm["model"])
            return
        if not llm["enabled"] or llm["force_fallback"]:
            self.say("  Hinweis: llm.enabled ist false oder llm.force_fallback true – das Modell wird erst")
            self.say("  genutzt, wenn das in config.yaml geändert wird. Bis dahin arbeitet der Regel-Parser.")
        tools = tool_models(models)
        if not tools:
            if models:
                self.say(f"  {len(models)} Modell(e) installiert, aber keins unterstützt Tools.")
            else:
                self.say("  Es ist noch kein Modell installiert.")
            self._explain_no_model(llm["model"])
            return
        self.say("Installierte Modelle mit Tool-Unterstützung (kleinstes zuerst):")
        for i, model in enumerate(tools, 1):
            extra = " – versteht auch Bilder (auch für 'Bildschirm beschreiben')" if model.supports_vision else ""
            self.say(f"  {i}) {model.name}  ({model.size_gb:.1f} GB){extra}")
        others = [m.name for m in models if not m.supports_tools]
        if others:
            self.say(f"  Ohne Tool-Unterstützung (für JARVIS nicht nutzbar): {', '.join(others)}")
        names = [m.name for m in tools]
        current = llm["model"]
        if current in names:
            default = names.index(current)
        else:
            default = 0
            if not is_todo(current):
                self.say(f"  Hinweis: '{current}' (bisher eingetragen) ist nicht installiert oder kann keine Tools.")
        while True:
            shown = f"{default + 1}: {names[default]}"
            raw = self.ask("Modell (Nummer oder Name, '-' = unverändert lassen)", show=shown)
            if raw == SKIP:
                self.say(f"  llm.model bleibt: {_display(current)}")
                return
            if not raw:
                choice = names[default]
            elif raw.isdigit() and 1 <= int(raw) <= len(names):
                choice = names[int(raw) - 1]
            elif raw in names:
                choice = raw
            else:
                self.say("  Bitte eine Nummer aus der Liste oder einen der Namen eingeben.")
                continue
            llm["model"] = choice
            self.say(f"  -> llm.model = {choice}")
            return

    def _ollama_models(self) -> list[ModelInfo] | None:
        """Installierte Modelle (einmal abgefragt, auch für das Vision-Modell); None = Ollama nicht erreichbar."""
        if self._models is None and self._models_error is None:
            base = str(self.data["llm"]["base_url"]).rstrip("/")
            self.say(f"Frage Ollama unter {base} ab (nur lesend: /api/tags, /api/show) ...")
            try:
                self._models = list_models(self.http, base, timeout=OLLAMA_TIMEOUT)
            except OllamaUnavailable as exc:
                self._models_error = str(exc)
        return self._models

    def _explain_no_model(self, current: str) -> None:
        self.say("  Kein Problem: JARVIS funktioniert auch ohne Sprachmodell. Buttons und Regel-Parser")
        self.say("  verstehen die Kernbefehle (z. B. 'Licht an', 'Helligkeit 40', 'Wie warm ist es?').")
        self.say("  Für den Chat mit Sprachmodell ein kleines Modell mit Tool-Support aussuchen")
        self.say("  (https://ollama.com/search?c=tools), selbst in einer Konsole 'ollama pull <name>'")
        self.say("  ausführen, danach Install.cmd erneut starten und 'Konfiguration jetzt anpassen?' mit j")
        self.say("  beantworten.")
        self.say(f"  llm.model bleibt: {_display(current)}")

    # --- e) Port ---------------------------------------------------------------------------

    def step_port(self) -> None:
        server = self.data["server"]
        self.say(_header(5, "Server-Port"))
        self.say("Port, unter dem JARVIS im Heimnetz und über Tailscale erreichbar ist (Standard: 8765).")
        self.say("Die Firewall-Regel muss denselben Port nutzen. Niemals im Router freigeben!")
        while True:
            raw = self.ask("Port", server["port"])
            if not raw or raw == SKIP:
                return
            if re.fullmatch(r"[0-9]{1,5}", raw) and 1 <= int(raw) <= 65535:
                server["port"] = int(raw)
                return
            self.say("  Bitte eine Zahl von 1 bis 65535 eingeben.")

    # --- f) Vision-Modell (Bildschirm beschreiben) -----------------------------------------

    def step_vision(self) -> None:
        vision, llm = self.data["vision"], self.data["llm"]
        self.say(_header(6, "Bildschirm beschreiben (Vision-Modell)"))
        self.say("Auf Wunsch beschreibt ein lokales Ollama-Modell, was auf dem Bildschirm zu sehen ist. Das")
        self.say("Bildschirmfoto geht nur an Ollama auf diesem PC, wird nicht gespeichert und nie ans Handy geschickt.")
        if not is_local_url(str(llm["base_url"])):
            self.say(f"  Hinweis: llm.base_url ({llm['base_url']}) zeigt nicht auf diesen PC – dann bleibt")
            self.say("  'Bildschirm beschreiben' gesperrt (das Bild darf den PC nicht verlassen).")
        current = vision["model"]
        models = self._ollama_models()
        if models is None:
            self.say(f"  Ollama ist nicht erreichbar – vision.model bleibt: {_display(current)}")
            return
        candidates = vision_models(models)
        if not candidates:
            if models:
                self.say(f"  {len(models)} Modell(e) installiert, aber keins versteht Bilder (capability 'vision').")
            else:
                self.say("  Es ist noch kein Modell installiert.")
            self.say("  Ohne Vision-Modell meldet 'Bildschirm beschreiben' nur 'noch nicht eingerichtet' – alles andere")
            self.say("  geht trotzdem. Später: ein Modell mit 'vision' aussuchen (https://ollama.com/search?c=vision),")
            self.say("  selbst 'ollama pull <name>' ausführen und Install.cmd erneut starten ('Konfiguration")
            self.say("  jetzt anpassen?' = j). Kann ein Modell 'vision' und 'tools', reicht eines für beides.")
            self.say(f"  vision.model bleibt: {_display(current)}")
            return
        self.say("Installierte Modelle, die Bilder verstehen (kleinstes zuerst):")
        for i, model in enumerate(candidates, 1):
            notes = []
            if model.name == llm["model"]:
                notes.append("= llm.model, kein zusätzlicher VRAM")
            elif model.supports_tools:
                notes.append("kann auch Tools")
            extra = f" – {', '.join(notes)}" if notes else ""
            self.say(f"  {i}) {model.name}  ({model.size_gb:.1f} GB){extra}")
        names = [m.name for m in candidates]
        if current in names:
            default = names.index(current)
        elif llm["model"] in names:
            default = names.index(llm["model"])  # gleiches Modell wie der Chat: braucht keinen zusätzlichen VRAM
        else:
            default = 0
            if not is_todo(current):
                self.say(f"  Hinweis: '{current}' (bisher eingetragen) ist nicht installiert oder versteht keine Bilder.")
        while True:
            shown = f"{default + 1}: {names[default]}"
            raw = self.ask("Vision-Modell (Nummer oder Name, '-' = unverändert lassen)", show=shown)
            if raw == SKIP:
                self.say(f"  vision.model bleibt: {_display(current)}")
                return
            if not raw:
                choice = names[default]
            elif raw.isdigit() and 1 <= int(raw) <= len(names):
                choice = names[int(raw) - 1]
            elif raw in names:
                choice = raw
            else:
                self.say("  Bitte eine Nummer aus der Liste oder einen der Namen eingeben.")
                continue
            vision["model"] = choice
            self.say(f"  -> vision.model = {choice}")
            return

    # --- g) PC-Steuerung (feste Aktionen, feste Programmliste) -----------------------------

    def step_desktop(self) -> None:
        desktop = self.data["desktop"]
        self.say(_header(7, "PC-Steuerung (feste Aktionen)"))
        self.say("JARVIS kann nur diese festen Aktionen: Lautstärke, Medientasten (Play/Pause, Titel vor/zurück),")
        self.say("PC sperren, Webseiten im Standardbrowser öffnen und Programme aus einer festen Liste starten,")
        self.say("schließen (nur nach Bestätigung in der Oberfläche) und nach vorne holen. Es gibt bewusst keine")
        self.say("freie Maus-/Tastatursteuerung, kein Tippen von Text und keine Befehlszeile.")
        desktop["enabled"] = self.confirm("PC-Steuerung einschalten?", bool(desktop["enabled"]))
        if not desktop["enabled"]:
            self.say("  PC-Steuerung ist aus (desktop.enabled: false).")
            return
        apps = desktop["apps"]
        self._say_apps(apps)
        self._offer_default_apps(apps)
        self._offer_found_apps(apps)
        self._ask_more_apps(apps)

    def _say_apps(self, apps: list[dict]) -> None:
        if apps:
            listed = ", ".join(f"{a['label']} ({a['process_name']})" for a in apps)
            self.say(f"Programme in der Liste: {listed}")
        else:
            self.say("Die Programmliste ist leer.")

    def _offer_default_apps(self, apps: list[dict]) -> None:
        missing = [copy.deepcopy(a) for a in self.defaults.get("desktop", {}).get("apps", [])
                   if not _app_known(apps, a)]
        if not missing:
            return
        labels = ", ".join(a["label"] for a in missing)
        # Leere Liste: Vorschlag ja. Fehlen nur einzelne, wurden sie wohl absichtlich entfernt.
        if self.confirm(f"Standard-Programme hinzufügen ({labels})?", not apps):
            for app in missing:
                app["id"] = _unique_app_id(app["id"], apps)
                apps.append(app)
            self.say(f"  -> hinzugefügt: {labels}")

    def _offer_found_apps(self, apps: list[dict]) -> None:
        for known in KNOWN_APPS:
            path = next((c for c in known.candidates(self.env) if self.is_file(c)), None)
            if path is None:
                continue
            entry = known.entry(path, apps)
            if _app_known(apps, entry):
                continue
            if self.confirm(f"Gefunden: {known.label} ({path}) – zur Liste hinzufügen?", True):
                apps.append(entry)

    def _ask_more_apps(self, apps: list[dict]) -> None:
        self.say("Weitere Programme: Pfad zur .exe (Tipp: Umschalt + Rechtsklick -> 'Als Pfad kopieren').")
        while True:
            raw = clean_path(self.ask("Weiteres Programm (Pfad zur .exe, Enter = fertig)"))
            if not raw or raw == SKIP:
                return
            path = absolute_path(raw)
            if _pathmod(path).splitext(path)[1].lower() != ".exe":
                self.say("  Bitte den Pfad zu einer .exe-Datei angeben (bei einer Verknüpfung: Rechtsklick ->")
                self.say("  Eigenschaften -> 'Ziel').")
                continue
            if not self.is_file(path):
                self.say(f"  Datei nicht gefunden: {path}")
                continue
            entry = app_entry_for_exe(path, apps)
            try:
                DesktopAppConfig.model_validate(entry)
            except ValidationError as exc:
                self.say(f"  Nicht möglich: {_first_error(exc)}")
                continue
            if _app_known(apps, entry):
                self.say(f"  {entry['process_name']} ist schon in der Liste.")
                continue
            label = self.ask("  Name in der Oberfläche", show=entry["label"])
            if label and label != SKIP:
                entry["label"] = label[:60]
                entry["window_title"] = label[:60]
            apps.append(entry)
            self.say(f"  -> hinzugefügt: {entry['label']} (ID {entry['id']}, Prozess {entry['process_name']})")

    # --- h) Stimme ---------------------------------------------------------------------------

    def step_voice(self) -> None:
        tts, stt = self.data["voice"]["tts"], self.data["voice"]["stt"]
        self.say(_header(8, "Stimme (Sprachausgabe und Spracheingabe)"))
        self.say("Sprachausgabe: JARVIS liest Antworten über die Lautsprecher des PCs vor (eingebaute")
        self.say("Windows-Stimme, offline, deutsch). In der Oberfläche schaltet 'PC spricht' das an und aus.")
        tts["enabled"] = self.confirm("Sprachausgabe einschalten?", bool(tts["enabled"]))
        self.say("Spracheingabe: Mikrofon-Knopf in der Oberfläche; erkannt wird lokal auf dem PC mit Whisper,")
        self.say("die Aufnahme verlässt den PC nicht. Dafür lädt der Installer einmalig – erst nach Rückfrage – das")
        size = APPROX_SIZES.get(str(stt["model"]))
        self.say(f"Zusatzpaket faster-whisper (ca. 90 MB, pypi.org) und das Whisper-Modell '{stt['model']}' "
                 f"({size or 'Größe unbekannt'}, Hugging Face).")
        self.say("Am Handy braucht das Mikrofon HTTPS (README, Abschnitt 12 'Spracheingabe am Handy').")
        label = f"Spracheingabe einschalten (Modell-Download {size})?" if size else "Spracheingabe einschalten?"
        stt["enabled"] = self.confirm(label, bool(stt["enabled"]))
        if stt["enabled"]:
            self._ask_whisper_model(stt)

    def _ask_whisper_model(self, stt: dict) -> None:
        names = list(APPROX_SIZES)
        half = (len(names) + 1) // 2
        self.say("Whisper-Modelle: " + ", ".join(f"{n} ({APPROX_SIZES[n]})" for n in names[:half]) + ",")
        self.say("                 " + ", ".join(f"{n} ({APPROX_SIZES[n]})" for n in names[half:]) + ".")
        self.say("Größer = genauer, aber langsamer (die Erkennung läuft auf der CPU). Empfehlung: small.")
        while True:
            raw = self.ask("Whisper-Modell ('-' = unverändert lassen)", stt["model"])
            if not raw or raw == SKIP:
                return
            if raw.lower() in names:
                stt["model"] = raw.lower()
                size = APPROX_SIZES[stt["model"]]
                self.say(f"  -> voice.stt.model = {stt['model']} ({size})")
                return
            self.say(f"  Bitte einen dieser Namen eingeben: {', '.join(names)}.")


# --------------------------------------------------------------------------------------------
# Programme für die PC-Steuerung (Vorschläge; die Dateien werden nie geöffnet oder gestartet)
# --------------------------------------------------------------------------------------------

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    message = str(errors[0].get("msg", "")) if errors else str(exc)
    return message.removeprefix("Value error, ")


def _app_known(apps: list[dict], entry: dict) -> bool:
    name = str(entry.get("process_name", "")).lower()
    return any(str(a.get("process_name", "")).lower() == name for a in apps)


def _unique_app_id(base: str, apps: list[dict]) -> str:
    taken = {str(a.get("id")) for a in apps}
    if base not in taken:
        return base
    for n in range(2, 1000):
        candidate = f"{base[:28]}-{n}"
        if candidate not in taken:
            return candidate
    raise ValueError("zu viele Programme mit gleichem Namen")


def app_id_from(label: str, apps: list[dict]) -> str:
    """ID nach ID_PATTERN (klein, a-z0-9_-) aus einem Namen, eindeutig in der Liste."""
    text = label.lower().translate(_UMLAUTS)
    text = re.sub(r"[^a-z0-9_-]+", "-", text).strip("-_")[:32] or "programm"
    if not text[0].isalnum():
        text = "p" + text[:31]
    return _unique_app_id(text, apps)


def app_entry_for_exe(path: str, apps: list[dict]) -> dict:
    """Eintrag für desktop.apps aus dem Pfad einer .exe (Name = Dateiname ohne Endung)."""
    pm = _pathmod(path)
    exe = pm.basename(path)
    label = pm.splitext(exe)[0][:60] or exe
    return {"id": app_id_from(label, apps), "label": label, "command": [path], "process_name": exe,
            "window_title": label}


@dataclass(frozen=True)
class KnownApp:
    """Häufiges Programm, dessen übliche Installationsorte der Assistent prüft (nur Existenz)."""

    id: str
    label: str
    process_name: str
    window_title: str
    locations: tuple[tuple[str, str], ...]  # (Umgebungsvariable, relativer Pfad)
    args: tuple[str, ...] = ()

    def candidates(self, env: Mapping[str, str]) -> list[str]:
        return [ntpath.join(env[var], rel) for var, rel in self.locations if env.get(var)]

    def entry(self, path: str, apps: list[dict]) -> dict:
        return {"id": _unique_app_id(self.id, apps), "label": self.label, "command": [path, *self.args],
                "process_name": self.process_name, "window_title": self.window_title}


KNOWN_APPS = (
    KnownApp("spotify", "Spotify", "Spotify.exe", "Spotify", (("APPDATA", r"Spotify\Spotify.exe"),)),
    # Discord startet über seinen Updater (so auch die Startmenü-Verknüpfung), der Prozess heißt Discord.exe.
    KnownApp("discord", "Discord", "Discord.exe", "Discord", (("LOCALAPPDATA", r"Discord\Update.exe"),),
             ("--processStart", "Discord.exe")),
    KnownApp("steam", "Steam", "steam.exe", "Steam",
             (("ProgramFiles(x86)", r"Steam\steam.exe"), ("ProgramFiles", r"Steam\steam.exe"))),
    KnownApp("firefox", "Firefox", "firefox.exe", "Firefox",
             (("ProgramFiles", r"Mozilla Firefox\firefox.exe"), ("ProgramFiles(x86)", r"Mozilla Firefox\firefox.exe"),
              ("LOCALAPPDATA", r"Mozilla Firefox\firefox.exe"))),
    KnownApp("chrome", "Chrome", "chrome.exe", "Chrome",
             (("ProgramFiles", r"Google\Chrome\Application\chrome.exe"),
              ("ProgramFiles(x86)", r"Google\Chrome\Application\chrome.exe"),
              ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe"))),
    KnownApp("edge", "Edge", "msedge.exe", "Edge",
             (("ProgramFiles(x86)", r"Microsoft\Edge\Application\msedge.exe"),
              ("ProgramFiles", r"Microsoft\Edge\Application\msedge.exe"))),
)


def summary_lines(data: dict) -> list[str]:
    def row(label: str, value: str) -> str:
        return f"  {(label + ':' if label else ''):<14} {value}"

    lines = ["", "=== Zusammenfassung ===", row("WLED", _display(data["wled"]["base_url"]))]
    sensors = data["sensors"]
    if not sensors:
        lines.append(row("Sensoren", "keine"))
    for s in sensors[:2]:
        entity = _display(s["entity_id"])
        lines.append(row(s["name"], f"{_display(s['base_url'])}, Entität {entity}"))
    if len(sensors) > 2:
        lines.append(row("", f"+ {len(sensors) - 2} weitere(r) Sensor(en), unverändert"))
    cs2 = next((s for s in data["scripts"] if s.get("id") == CS2_ID), None)
    if cs2 is None or is_todo(cs2["cwd"]) or is_todo(cs2["command"]):
        lines.append(row("CS2-Skript", "nicht eingerichtet"))
    else:
        lines.append(row("CS2-Skript", format_command(cs2["command"])))
        lines.append(row("", f"Arbeitsordner: {cs2['cwd']}"))
        lines.append(row("", f"ohne Fenster: {'ja' if cs2['hide_window'] else 'nein'}"))
    others = [s for s in data["scripts"] if s.get("id") != CS2_ID]
    if others:
        lines.append(row("", f"+ {len(others)} weitere(s) Skript(e), unverändert"))
    lines.append(row("LLM-Modell", _display(data["llm"]["model"])))
    lines.append(row("Server", f"{data['server']['bind']}, Port {data['server']['port']}"))
    lines.append(row("Bildschirm", _display(data["vision"]["model"])))
    desktop = data["desktop"]
    if desktop["enabled"]:
        labels = ", ".join(a["label"] for a in desktop["apps"]) or "keine Programme"
        lines.append(row("PC-Steuerung", f"an – {labels}"))
    else:
        lines.append(row("PC-Steuerung", "aus"))
    tts, stt = data["voice"]["tts"], data["voice"]["stt"]
    lines.append(row("Sprachausgabe", "an" if tts["enabled"] else "aus"))
    lines.append(row("Spracheingabe", f"an (Whisper-Modell {stt['model']})" if stt["enabled"] else "aus"))
    lines.append("  Alle übrigen Werte (allowed_networks, LLM-Einstellungen, Timeouts, Stimme, Lautstärke, erlaubte")
    lines.append("  Domains) bleiben erhalten.")
    return lines


# --------------------------------------------------------------------------------------------
# Unterbefehle
# --------------------------------------------------------------------------------------------


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def save_config(path: Path, text: str) -> Path | None:
    """Sichert eine vorhandene Datei nach .bak und schreibt atomar. Gibt den Sicherungspfad zurück."""
    backup = None
    if path.exists():
        backup = backup_path(path)
        shutil.copy2(path, backup)
    write_atomic(path, text)
    return backup


def configure(
    config_path: Path,
    example_path: Path | None = None,
    *,
    non_interactive: bool = False,
    io: WizardIO | None = None,
    http: httpx.Client | None = None,
    is_file: Callable[[str], bool] = os.path.isfile,
    is_dir: Callable[[str], bool] = os.path.isdir,
    which: Callable[[str], str | None] = shutil.which,
    env: Mapping[str, str] | None = None,
) -> int:
    example_path = example_path or default_example_path(config_path)
    if non_interactive:
        return _configure_non_interactive(config_path, example_path)

    io = io or ConsoleIO()
    own_http = http is None
    if own_http:
        http = httpx.Client(timeout=PROBE_TIMEOUT, trust_env=False)
    try:
        return _configure_interactive(config_path, example_path, io, http, is_file, is_dir, which, env)
    except (KeyboardInterrupt, EOFError):
        try:
            io.say("")
            io.say(f"Abgebrochen – {config_path.name} wurde nicht verändert.")
        except (KeyboardInterrupt, EOFError):
            pass
        return EXIT_ABORTED
    finally:
        if own_http:
            http.close()


def _configure_non_interactive(config_path: Path, example_path: Path) -> int:
    if config_path.exists():
        try:
            cfg = parse_config(load_existing(config_path), config_path.name)
        except ConfigError as exc:
            _err(f"{exc}\nBitte {config_path} korrigieren (oder den Assistenten interaktiv starten).")
            return EXIT_ERROR
        print(f"{config_path.name} ist gültig und bleibt unverändert: {config_path}")
        for warning in config_warnings(cfg):
            print(f"  Hinweis: {warning}")
        return EXIT_OK
    data, note = load_defaults(example_path)
    if note:
        print(f"Hinweis: {note}")
    write_atomic(config_path, checked_render(data))
    print(f"{config_path.name} mit Standardwerten angelegt: {config_path}")
    print("TODO-Werte später mit dem Assistenten oder im Editor ausfüllen; JARVIS startet auch so.")
    return EXIT_OK


def _configure_interactive(config_path, example_path, io, http, is_file, is_dir, which, env) -> int:
    defaults, note = load_defaults(example_path)
    original: dict | None = None
    io.say("=" * 64)
    io.say(" JARVIS – Einrichtung")
    io.say("=" * 64)
    io.say("Dieser Assistent fragt die wichtigsten Einstellungen ab und speichert sie in:")
    io.say(f"  {config_path}")
    io.say("In eckigen Klammern steht der aktuelle Wert – Enter übernimmt ihn. Gespeichert wird erst nach")
    io.say(f"der Zusammenfassung. Abbrechen mit Strg+C: {config_path.name} bleibt dann unverändert (fragt")
    io.say("Windows danach 'Batchvorgang abbrechen (J/N)?', mit J bestätigen).")
    if note:
        io.say(f"Hinweis: {note}")
    if config_path.exists():
        try:
            original = load_existing(config_path)
        except ConfigError as exc:
            io.say("")
            io.say(f"Die vorhandene {config_path.name} ist fehlerhaft:")
            io.say(str(exc))
            wizard = Wizard(io, http, defaults, defaults)
            if not wizard.confirm(
                f"Mit den Standardwerten neu beginnen? (Die alte Datei wird beim Speichern als "
                f"{backup_path(config_path).name} gesichert)",
                False,
            ):
                io.say(f"Abgebrochen – {config_path.name} wurde nicht verändert.")
                return EXIT_ABORTED
        else:
            io.say("Vorhandene Werte sind die Vorgabe; alle anderen Einstellungen bleiben erhalten.")
    start = original if original is not None else defaults
    wizard = Wizard(io, http, start, defaults, is_file=is_file, is_dir=is_dir, which=which, env=env)
    data = wizard.run()

    try:
        text = checked_render(data)
    except ConfigError as exc:
        _err(str(exc))
        io.say(f"Nichts gespeichert – {config_path.name} wurde nicht verändert.")
        return EXIT_ERROR
    final = parse_config(data).model_dump()
    for line in summary_lines(final):
        io.say(line)
    if original is not None and final == original:
        io.say("")
        io.say(f"Keine Änderungen – {config_path.name} bleibt unverändert.")
        return EXIT_OK
    if not wizard.confirm(f"Speichern nach {config_path}?", True):
        io.say(f"Nicht gespeichert – {config_path.name} wurde nicht verändert.")
        return EXIT_ABORTED
    backup = save_config(config_path, text)
    io.say(f"Gespeichert: {config_path}")
    if backup is not None:
        io.say(f"Sicherung der alten Datei: {backup}")
    io.say("Damit Änderungen wirken: JARVIS neu starten (Startmenü > JARVIS > JARVIS beenden, dann")
    io.say("JARVIS starten). Install.cmd erledigt das automatisch.")
    if final["voice"]["stt"]["enabled"]:
        io.say("Spracheingabe: Zusatzpaket und Whisper-Modell installiert Install.cmd (nach Rückfrage). Ohne")
        io.say("Installer im JARVIS-Ordner: uv sync --frozen --no-dev --extra voice, danach")
        io.say(".venv\\Scripts\\python.exe -m app.voice.stt --download --config config.yaml")
    warnings = config_warnings(parse_config(final))
    if warnings:
        io.say("Noch offen (JARVIS startet trotzdem):")
        for warning in warnings:
            io.say(f"  - {warning}")
    return EXIT_OK


def token(secrets_path: Path) -> int:
    if secrets_path.exists():
        try:
            data = yaml.safe_load(secrets_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            _err(f"{secrets_path} kann nicht gelesen werden ({type(exc).__name__}). Datei bleibt unverändert.")
            return EXIT_ERROR
        existing = data.get("api_token") if isinstance(data, dict) else None
        if isinstance(existing, str) and len(existing) >= 32:
            print(TOKEN_EXISTS_MESSAGE)
            return EXIT_OK
        _err(
            f"{secrets_path} enthält keinen gültigen API-Token und bleibt unverändert.\n"
            "Für einen neuen Token die Datei löschen und diesen Schritt (oder JARVIS) erneut starten."
        )
        return EXIT_ERROR
    created: list[str] = []
    value = load_or_create_token(secrets_path, announce=created.append)
    if not created:
        print(TOKEN_EXISTS_MESSAGE)
        return EXIT_OK
    print("")
    print("=== Neuer API-Token für JARVIS (wird nur jetzt angezeigt) ===")
    print(f"  {value}")
    print(f"Gespeichert in: {secrets_path}")
    print("Diesen Token beim ersten Öffnen der JARVIS-Oberfläche eingeben (PC und Handy).")
    print("Vergessen? Er steht in secrets.yaml. Niemals weitergeben.")
    print("")
    return EXIT_OK


def local_url(bind: str, port: int) -> str:
    """Adresse, unter der der Server auf diesem PC erreichbar ist."""
    host = bind.strip()
    if host in ("", "0.0.0.0", "localhost") or host.startswith("127."):
        host = "127.0.0.1"
    elif host == "::":
        host = "[::1]"
    elif ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/"


def info(config_path: Path) -> int:
    try:
        cfg = parse_config(load_existing(config_path), config_path.name)
    except ConfigError as exc:
        _err(str(exc))
        return EXIT_ERROR
    payload = {
        "bind": cfg.server.bind,
        "port": cfg.server.port,
        "local_url": local_url(cfg.server.bind, cfg.server.port),
        "warnings": config_warnings(cfg),
        # Für den Installer (Zusammenfassung, optionales Paket für die Spracheingabe):
        "desktop_enabled": cfg.desktop.enabled,
        "vision_model": None if is_todo(cfg.vision.model) else cfg.vision.model,
        "tts_enabled": cfg.voice.tts.enabled,
        "stt_enabled": cfg.voice.stt.enabled,
        "stt_model": cfg.voice.stt.model,
        "stt_model_size": APPROX_SIZES.get(cfg.voice.stt.model),  # z. B. "ca. 500 MB", null = unbekannt
    }
    print(json.dumps(payload))  # ensure_ascii: reine ASCII-Zeile, egal welche Konsolen-Codepage
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.setup_wizard", description="JARVIS-Einrichtung (config.yaml, Token, Infos)."
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{configure,token,info}")
    p = sub.add_parser("configure", help="config.yaml interaktiv ausfüllen oder prüfen")
    p.add_argument("--config", required=True, type=Path, help="Pfad zur config.yaml")
    p.add_argument("--example", type=Path, help="Pfad zur config.example.yaml (Standardwerte)")
    p.add_argument("--non-interactive", action="store_true",
                   help="nie fragen: fehlt die Datei, Standardwerte schreiben, sonst nur prüfen")
    p = sub.add_parser("token", help="API-Token in secrets.yaml sicherstellen")
    p.add_argument("--secrets", required=True, type=Path, help="Pfad zur secrets.yaml")
    p = sub.add_parser("info", help="bind/port/local_url/warnings/... als eine JSON-Zeile ausgeben")
    p.add_argument("--config", required=True, type=Path, help="Pfad zur config.yaml")
    return parser


def main(argv: list[str] | None = None, **configure_kwargs: Any) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "configure":
            return configure(
                args.config, args.example, non_interactive=args.non_interactive, **configure_kwargs
            )
        if args.command == "token":
            return token(args.secrets)
        return info(args.config)
    except (KeyboardInterrupt, EOFError):
        _err("Abgebrochen.")
        return EXIT_ABORTED
    except Exception as exc:  # nie einen Stacktrace zeigen
        detail = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
        filename = getattr(exc, "filename", None)
        _err(f"Fehler: {detail}" + (f" ({filename})" if filename else ""))
        return EXIT_ERROR


def _console_safe_streams() -> None:
    """Zeichen, die die Konsolen-Codepage nicht kennt, ersetzen statt abzustürzen."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


if __name__ == "__main__":
    _console_safe_streams()
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:  # Strg+C kam unter Windows erst nach dem EOFError an
        raise SystemExit(EXIT_ABORTED) from None
