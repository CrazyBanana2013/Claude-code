"""Konfiguration laden und validieren (config.yaml)."""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PROJECT_DIR = Path(__file__).resolve().parent.parent

DEFAULT_NETWORKS = [
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
]


class ConfigError(Exception):
    """Konfiguration fehlt oder ist ungültig. Die Nachricht ist für Menschen gedacht."""


def is_todo(value: Any) -> bool:
    """True, wenn ein Wert noch ein TODO-Platzhalter ist (oder fehlt)."""
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return not stripped or "TODO" in stripped
    if isinstance(value, (list, tuple)):
        return any(is_todo(v) for v in value)
    return False


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Strict):
    bind: str = "0.0.0.0"
    port: int = Field(8765, ge=1, le=65535)
    allowed_networks: list[str] = Field(default_factory=lambda: list(DEFAULT_NETWORKS))

    @field_validator("allowed_networks")
    @classmethod
    def _check_networks(cls, value: list[str]) -> list[str]:
        for net in value:
            try:
                ipaddress.ip_network(net, strict=False)
            except ValueError as exc:
                raise ValueError(f"'{net}' ist kein gültiges Netz (z. B. 192.168.0.0/16)") from exc
        return value


class LLMConfig(_Strict):
    enabled: bool = True
    force_fallback: bool = False
    base_url: str = "http://127.0.0.1:11434"
    model: str = "TODO_MODELLNAME"
    keep_alive: str = "2m"
    timeout: float = Field(60, gt=0, le=600)
    temperature: float = Field(0.2, ge=0, le=2)
    max_tool_rounds: int = Field(4, ge=1, le=10)
    # Nur für "Thinking"-Modelle: false spart Zeit. null = Parameter nicht senden.
    think: bool | None = None

    @property
    def usable(self) -> bool:
        return self.enabled and not self.force_fallback and not is_todo(self.model)


class WLEDConfig(_Strict):
    base_url: str = "TODO"
    timeout: float = Field(3, gt=0, le=30)


class SensorConfig(_Strict):
    name: str = Field(min_length=1)
    type: Literal["esphome_rest"] = "esphome_rest"
    base_url: str
    entity_id: str
    unit: str = ""


ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"


class ScriptConfig(_Strict):
    id: str = Field(pattern=ID_PATTERN)
    label: str = Field(min_length=1)
    cwd: str
    command: list[str] = Field(min_length=1)
    single_instance: bool = True
    hide_window: bool = False

    @property
    def is_configured(self) -> bool:
        return not is_todo(self.cwd) and not is_todo(self.command)


# Prozesse, die JARVIS nie starten/schließen/fokussieren darf – auch nicht, wenn sie in
# desktop.apps stehen: Windows-Kernprozesse, die Shell und JARVIS bzw. Ollama selbst.
PROTECTED_PROCESS_NAMES = frozenset(
    {
        "explorer.exe", "dwm.exe", "winlogon.exe", "csrss.exe", "lsass.exe", "services.exe",
        "smss.exe", "svchost.exe", "wininit.exe", "sihost.exe", "ctfmon.exe", "taskhostw.exe",
        "runtimebroker.exe", "fontdrvhost.exe", "conhost.exe", "taskmgr.exe", "logonui.exe",
        "lockapp.exe", "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
        "python.exe", "pythonw.exe", "py.exe", "pyw.exe", "uv.exe", "ollama.exe", "ollama app.exe",
        "tailscale.exe", "tailscaled.exe", "tailscale-ipn.exe",
    }
)
_PROCESS_NAME_FORBIDDEN = set('\\/:*?"<>|')
# Ein Label eines Domainnamens (nach IDNA, also nur ASCII)
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_domain(value: str) -> str:
    """Domainnamen vereinheitlichen (klein, ohne Punkt am Ende, Umlaute als IDNA/Punycode).

    Wirft ValueError mit deutscher Meldung, wenn es kein gültiger Domainname ist.
    """
    host = value.strip().lower().rstrip(".")
    if host.startswith("*."):
        host = host[2:]
    if not host or "://" in host or any(c in host for c in "/:@\\ \t?#"):
        raise ValueError(f"'{value}' ist kein Domainname (nur z. B. youtube.com, ohne https:// und Pfad)")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError(f"'{value}' ist kein gültiger Domainname") from None
    labels = host.split(".")
    if len(host) > 253 or not all(_DOMAIN_LABEL.match(label) for label in labels):
        raise ValueError(f"'{value}' ist kein gültiger Domainname")
    return host


class DesktopAppConfig(_Strict):
    """Ein Programm, das JARVIS starten, schließen und in den Vordergrund holen darf."""

    id: str = Field(pattern=ID_PATTERN)
    label: str = Field(min_length=1, max_length=60)
    # Argumentliste wie bei scripts (kein Shell-String); z. B. ["notepad.exe"]
    command: list[str] = Field(min_length=1)
    # Dateiname des laufenden Prozesses (ohne Pfad), z. B. "notepad.exe" – zum Erkennen und Schließen
    process_name: str = Field(min_length=1, max_length=100)
    # Teil des Fenstertitels, falls das Fenster einem anderen Prozess gehört (z. B. Store-Apps)
    window_title: str = Field("", max_length=200)

    @field_validator("process_name")
    @classmethod
    def _check_process_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(c in _PROCESS_NAME_FORBIDDEN for c in value):
            raise ValueError("nur der Dateiname des Prozesses ohne Pfad, z. B. notepad.exe")
        if value.lower() in PROTECTED_PROCESS_NAMES:
            raise ValueError(f"'{value}' ist ein System-, Shell- oder JARVIS-Prozess und wird nicht gesteuert")
        return value

    @property
    def is_configured(self) -> bool:
        return not is_todo(self.command) and not is_todo(self.process_name)


def _default_apps() -> list[DesktopAppConfig]:
    return [
        DesktopAppConfig(
            id="editor", label="Editor", command=["notepad.exe"], process_name="notepad.exe", window_title="Editor"
        ),
        DesktopAppConfig(
            id="rechner", label="Rechner", command=["calc.exe"], process_name="CalculatorApp.exe",
            window_title="Rechner",
        ),
    ]


class DesktopConfig(_Strict):
    enabled: bool = True
    allow_open_url: bool = True
    # Leer = alle Domains erlaubt; sonst nur diese Domains und ihre Subdomains.
    allowed_domains: list[str] = Field(default_factory=list)
    apps: list[DesktopAppConfig] = Field(default_factory=_default_apps)

    @field_validator("allowed_domains")
    @classmethod
    def _check_domains(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(normalize_domain(v) for v in value))

    @field_validator("apps")
    @classmethod
    def _unique_ids(cls, value: list[DesktopAppConfig]) -> list[DesktopAppConfig]:
        seen: set[str] = set()
        for app in value:
            if app.id in seen:
                raise ValueError(f"Programm-ID '{app.id}' ist doppelt vergeben")
            seen.add(app.id)
        return value

    def app(self, app_id: str) -> DesktopAppConfig | None:
        return next((a for a in self.apps if a.id == app_id), None)


class VisionConfig(_Strict):
    # Ollama-Modell mit capability "vision" (darf gleich llm.model sein)
    model: str = "TODO_VISIONMODELL"
    max_side: int = Field(1568, ge=256, le=4096)
    jpeg_quality: int = Field(80, ge=30, le=95)
    monitor: int = Field(1, ge=0, le=16)  # 1 = Hauptbildschirm, 0 = alle Bildschirme zusammen
    timeout: float = Field(90, gt=0, le=600)

    @property
    def configured(self) -> bool:
        return not is_todo(self.model)


class TTSConfig(_Strict):
    enabled: bool = True
    speak_replies: bool = True
    voice: str = Field("", max_length=200)  # "" = erste installierte deutsche Stimme
    rate: int = Field(0, ge=-10, le=10)
    volume: int = Field(100, ge=0, le=100)


class STTConfig(_Strict):
    enabled: bool = False
    model: str = Field("small", min_length=1, max_length=200)
    device: Literal["cpu", "cuda", "auto"] = "cpu"
    # Werte laut CTranslate2-Doku (docs/quantization.md)
    compute_type: Literal[
        "default", "auto", "int8", "int8_float32", "int8_float16", "int8_bfloat16", "int16", "float16",
        "bfloat16", "float32",
    ] = "int8"
    language: str = Field("de", pattern=r"^([a-z]{2,3})?$")  # "" = automatisch erkennen
    max_seconds: int = Field(30, ge=1, le=120)
    download_root: str = ""  # "" = <state>/whisper


class VoiceConfig(_Strict):
    tts: TTSConfig = Field(default_factory=TTSConfig)
    stt: STTConfig = Field(default_factory=STTConfig)


class AppConfig(_Strict):
    server: ServerConfig = Field(default_factory=ServerConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    wled: WLEDConfig = Field(default_factory=WLEDConfig)
    sensors: list[SensorConfig] = Field(default_factory=list)
    scripts: list[ScriptConfig] = Field(default_factory=list)
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)

    @field_validator("scripts")
    @classmethod
    def _unique_ids(cls, value: list[ScriptConfig]) -> list[ScriptConfig]:
        seen: set[str] = set()
        for script in value:
            if script.id in seen:
                raise ValueError(f"Skript-ID '{script.id}' ist doppelt vergeben")
            seen.add(script.id)
        return value

    def script(self, script_id: str) -> ScriptConfig | None:
        return next((s for s in self.scripts if s.id == script_id), None)


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) if not isinstance(p, int) else f"[{p}]" for p in err["loc"])
        loc = loc.replace(".[", "[")
        msg = err["msg"]
        if err["type"] == "missing":
            msg = "Pflichtfeld fehlt"
        elif err["type"] == "extra_forbidden":
            msg = "unbekannter Schlüssel (Tippfehler?)"
        lines.append(f"  - {loc or '(Wurzel)'}: {msg}")
    return "\n".join(lines)


def parse_config(data: Any, source: str = "config") -> AppConfig:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: Die Datei muss aus Abschnitten (server, llm, ...) bestehen.")
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{source} ist ungültig:\n{_format_errors(exc)}") from None


def default_config_path() -> Path:
    return Path(os.environ.get("JARVIS_CONFIG", PROJECT_DIR / "config.yaml"))


def load_config(path: Path | str | None = None) -> AppConfig:
    path = Path(path) if path else default_config_path()
    if not path.exists():
        raise ConfigError(
            f"Konfigurationsdatei nicht gefunden: {path}\n"
            f"  Lege sie an mit:  copy config.example.yaml config.yaml  (Windows)\n"
            f"                    cp config.example.yaml config.yaml    (Linux/macOS)"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ConfigError(
            f"{path.name} ist nicht UTF-8-kodiert (z. B. im Editor als 'ANSI' gespeichert). "
            "Bitte im Editor mit Codierung UTF-8 speichern."
        ) from None
    except IsADirectoryError:
        raise ConfigError(f"{path} ist ein Ordner, keine Datei.") from None
    except OSError as exc:
        raise ConfigError(f"{path} kann nicht gelesen werden: {exc.strerror or exc}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (Zeile {mark.line + 1}, Spalte {mark.column + 1})" if mark else ""
        raise ConfigError(f"{path.name} ist kein gültiges YAML{where}: {exc}") from None
    return parse_config(data, source=path.name)


def config_warnings(cfg: AppConfig) -> list[str]:
    """Hinweise auf noch nicht ausgefüllte Werte – für die Konsole beim Start."""
    warnings = []
    if cfg.llm.enabled and not cfg.llm.force_fallback and is_todo(cfg.llm.model):
        warnings.append("llm.model ist noch TODO → nur Regel-Parser aktiv.")
    if is_todo(cfg.wled.base_url):
        warnings.append("wled.base_url ist noch TODO → LED-Tools melden 'nicht eingerichtet'.")
    for s in cfg.sensors:
        if is_todo(s.base_url) or is_todo(s.entity_id):
            warnings.append(f"Sensor '{s.name}': base_url/entity_id noch TODO.")
    for sc in cfg.scripts:
        if not sc.is_configured:
            warnings.append(f"Skript '{sc.id}': cwd/command noch TODO.")
    if cfg.desktop.enabled:
        for app in cfg.desktop.apps:
            if not app.is_configured:
                warnings.append(f"Programm '{app.id}': command/process_name noch TODO.")
    return warnings
