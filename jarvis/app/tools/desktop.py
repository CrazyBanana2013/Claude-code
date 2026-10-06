"""Feste PC-Aktionen: Programme aus der Liste, Lautstärke, Medientasten, PC sperren, Webseite öffnen.

Bewusst gibt es KEINE freie Maus-/Tastatursteuerung, kein Tippen von Text, keine Klicks auf
Koordinaten, keine Befehlszeile und keine beliebigen Programme: Der Server ist aus dem Heimnetz und
über Tailscale erreichbar, und das LLM kann über Bildschirm- oder Webinhalte manipuliert werden
(Prompt-Injection). Jede Aktion ist deshalb ein eigenes Tool mit streng geprüften Parametern; das LLM
kann nur Programm-IDs aus ``desktop.apps`` wählen.

Alle Windows-Aufrufe stecken in kleinen Wrapper-Funktionen (``_send_media_key``, ``_lock_workstation``,
``_volume_get``/``_volume_set``/``_volume_mute``, ``_start_process``, ``_user_processes``,
``_close_processes``, ``_close_windows``, ``_focus_window``, ``_open_in_browser``), die Tests ersetzen. Jeder Wrapper
verweigert auf Nicht-Windows-Systemen selbst noch einmal (Schutz der Entwicklungsmaschine). Auch die
DNS-Abfrage für desktop_open_url (``_resolve_host``) ist ein ersetzbarer Wrapper.

Quellen: Virtual-Key-Codes und Scan-Codes der Medientasten, SendInput/INPUT/KEYBDINPUT,
LockWorkStation, SetForegroundWindow (Einschränkungen), EnumWindows, GetWindowThreadProcessId,
ShowWindowAsync (SW_RESTORE = 9), IsHungAppWindow, GetWindow (GW_OWNER = 4), EnumChildWindows,
PostMessage (WM_CLOSE = 0x0010) laut Microsoft-Doku (learn.microsoft.com,
Repos MicrosoftDocs/sdk-api und MicrosoftDocs/win32); Lautstärke über pycaw 20260927
(``AudioUtilities.GetSpeakers().EndpointVolume`` → IAudioEndpointVolume
``Get/SetMasterVolumeLevelScalar``, ``Get/SetMute``).
"""

from __future__ import annotations

import ctypes
import ipaddress
import os
import re
import socket
import subprocess
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import psutil
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import DesktopAppConfig, DesktopConfig, normalize_domain
from app.tools import pc
from app.tools.registry import Registry, ToolContext, ToolError

CLOSE_GRACE_SECONDS = 5.0
MAX_URL_LENGTH = 2048
NOT_WINDOWS = "nur auf dem Windows-PC verfügbar"

# Virtual-Key-Code und Scan-Code (ohne E0-Präfix) laut Microsoft-Doku – fest, nichts anderes wird gesendet.
MEDIA_KEYS: dict[str, tuple[int, int]] = {
    "play_pause": (0xB3, 0x22),  # VK_MEDIA_PLAY_PAUSE, Scan E0 22
    "next": (0xB0, 0x19),  # VK_MEDIA_NEXT_TRACK, Scan E0 19
    "previous": (0xB1, 0x10),  # VK_MEDIA_PREV_TRACK, Scan E0 10
    "stop": (0xB2, 0x24),  # VK_MEDIA_STOP, Scan E0 24
}
ALLOWED_VKS = frozenset(vk for vk, _scan in MEDIA_KEYS.values())
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9
GW_OWNER = 4
WM_CLOSE = 0x0010
ERROR_ACCESS_DENIED = 5
MEDIA_LOCKED = "Der PC ist gesperrt – Medientasten gehen erst nach dem Entsperren."
# Besitzer der Fensterrahmen von Store-Apps (Rechner & Co.); das Fenster der App ist ein Kindfenster darin.
FRAME_HOST = "applicationframehost.exe"

# Hostnamen, die nur im Heimnetz existieren (Router, Geräte). Webseiten öffnen soll keine Geräte im
# LAN per GET schalten können (z. B. WLED-HTTP-API) – außer die Domain steht in allowed_domains.
LOCAL_SUFFIXES = (
    "localhost", "local", "lan", "home", "internal", "intranet", "corp", "home.arpa", "fritz.box",
)
_URL_SAFE = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*$")
_ENDS_IN_NUMBER = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")  # WHATWG-URL: so ein Host ist eine IPv4-Adresse
# IPv6-Bereiche, die eine IPv4-Adresse in den letzten 32 Bit tragen (IPv4-kompatibel, NAT64 laut RFC 6052/8215)
_EMBEDDED_V4_NETS = tuple(ipaddress.ip_network(n) for n in ("::/96", "64:ff9b::/96", "64:ff9b:1::/48"))
DNS_TIMEOUT = 3.0


def is_windows() -> bool:
    return os.name == "nt"


def _refuse_unless_windows(what: str) -> None:
    if not is_windows():
        raise ToolError(f"{what} ist {NOT_WINDOWS}.")


# --------------------------------------------------------------------------------------------
# Parameter
# --------------------------------------------------------------------------------------------


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppParams(_Params):
    app_id: str = Field(min_length=1, max_length=32, description="ID des Programms aus desktop_apps_list")


class VolumeParams(_Params):
    action: Literal["set", "up", "down", "mute", "unmute", "get"] = Field(
        description="set = auf percent setzen, up/down = um step lauter/leiser, mute/unmute, get = abfragen"
    )
    percent: float | None = Field(None, description="Ziel-Lautstärke 0–100, nur für set")
    step: int = Field(10, ge=1, le=100, description="Schrittweite in Prozentpunkten für up/down")

    @model_validator(mode="after")
    def _percent_for_set(self) -> "VolumeParams":
        if self.action == "set" and self.percent is None:
            raise ValueError("für action 'set' bitte percent (0–100) angeben")
        return self


class MediaParams(_Params):
    action: Literal["play_pause", "next", "previous", "stop"] = Field(
        description="play_pause = Wiedergabe/Pause umschalten, next/previous = Titel wechseln, stop = anhalten"
    )


class UrlParams(_Params):
    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH, description="http- oder https-Adresse")


# --------------------------------------------------------------------------------------------
# Windows-Wrapper (in Tests ersetzt)
# --------------------------------------------------------------------------------------------

_USER32: Any = None
_USER32_LOCK = threading.Lock()


def _c_types():
    """ctypes-Strukturen für SendInput mit festen Breiten (wie Windows: LONG/DWORD = 32 bit)."""
    LONG, DWORD, WORD = ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint16
    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):  # nur für die richtige Größe der Union, wird nie befüllt
        _fields_ = [("dx", LONG), ("dy", LONG), ("mouseData", DWORD), ("dwFlags", DWORD),
                    ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", WORD), ("wScan", WORD), ("dwFlags", DWORD), ("time", DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", DWORD), ("u", _INPUTUNION)]

    return INPUT, KEYBDINPUT


INPUT, KEYBDINPUT = _c_types()
WNDENUMPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p
)


def _load_user32():  # pragma: no cover - nur unter Windows
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.LockWorkStation.argtypes = []
    user32.LockWorkStation.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_void_p]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [ctypes.c_void_p, wintypes.UINT]
    user32.GetWindow.restype = ctypes.c_void_p
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsIconic.argtypes = [ctypes.c_void_p]
    user32.IsIconic.restype = wintypes.BOOL
    # ShowWindowAsync statt ShowWindow: wartet nie auf ein hängendes Programm (sdk-api showwindowasync)
    user32.ShowWindowAsync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    user32.IsHungAppWindow.argtypes = [ctypes.c_void_p]
    user32.IsHungAppWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.EnumChildWindows.argtypes = [ctypes.c_void_p, WNDENUMPROC, ctypes.c_void_p]
    user32.EnumChildWindows.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    return user32


def _user32():
    global _USER32
    if not is_windows():
        raise ToolError(f"Diese Aktion ist {NOT_WINDOWS}.")
    with _USER32_LOCK:
        if _USER32 is None:
            _USER32 = _load_user32()
        return _USER32


def _send_media_key(vk: int, scan: int) -> None:
    """Eine Medientaste drücken und loslassen (SendInput). Nur die festen Codes aus MEDIA_KEYS."""
    if vk not in ALLOWED_VKS:
        raise ToolError("Interner Fehler: Diese Taste darf JARVIS nicht senden.")
    user32 = _user32()
    inputs = (INPUT * 2)()
    for item, flags in zip(inputs, (KEYEVENTF_EXTENDEDKEY, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP), strict=True):
        item.type = INPUT_KEYBOARD
        item.u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    sent = user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
    if sent != 2:
        # Gesperrter PC: Eingaben gehen an den Winlogon-Desktop, SendInput scheitert mit ERROR_ACCESS_DENIED.
        if _last_error() == ERROR_ACCESS_DENIED:
            raise ToolError(MEDIA_LOCKED)
        raise ToolError("Windows hat die Medientaste nicht angenommen.")


def _last_error() -> int:
    """GetLastError des letzten user32-Aufrufs (use_last_error=True); 0 außerhalb von Windows."""
    get = getattr(ctypes, "get_last_error", None)
    return int(get()) if get is not None else 0


def _lock_workstation() -> bool:
    return bool(_user32().LockWorkStation())


def _window_title(user32, hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _focus_window(pids: list[int], title: str) -> str:
    """Sichtbares Hauptfenster der Prozesse (sonst eines mit passendem Titel) nach vorne holen.

    Ergebnis: "focused", "refused" (Windows hat den Fokuswechsel verweigert) oder "no_window".
    """
    user32 = _user32()
    wanted = {int(p) for p in pids}
    title_l = title.strip().lower()
    by_pid: list[int] = []
    by_title: list[int] = []

    def callback(hwnd, _lparam):
        if not hwnd or not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
            return 1
        pid = ctypes.c_uint32(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.pointer(pid))
        if pid.value in wanted:
            by_pid.append(hwnd)
        elif title_l and title_l in _window_title(user32, hwnd).lower():
            by_title.append(hwnd)
        return 1

    user32.EnumWindows(WNDENUMPROC(callback), None)
    candidates = by_pid or by_title
    if not candidates:
        return "no_window"
    hwnd = candidates[0]
    if user32.IsHungAppWindow(hwnd):
        return "hung"
    if user32.IsIconic(hwnd):
        user32.ShowWindowAsync(hwnd, SW_RESTORE)  # nur einreihen – blockiert nie den Worker-Thread
    return "focused" if user32.SetForegroundWindow(hwnd) else "refused"


def _window_pid(user32, hwnd) -> int:
    pid = ctypes.c_uint32(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.pointer(pid))
    return int(pid.value)


def _close_windows(pids: list[int]) -> int:
    """WM_CLOSE an die sichtbaren Hauptfenster der Prozesse (wie ein Klick auf X). Ergebnis: Anzahl Fenster.

    Store-Apps wie der Rechner (CalculatorApp.exe) haben kein eigenes Hauptfenster: Den sichtbaren Rahmen
    besitzt ApplicationFrameHost.exe, das Fenster der App ist ein Kindfenster darin. So ein Rahmen bekommt
    WM_CLOSE nur, wenn eines seiner Kindfenster zu einem der Prozesse gehört – nie nach Fenstertitel.
    PostMessage wartet nicht auf das Programm (hängt es, bleibt JARVIS trotzdem bedienbar).
    """
    user32 = _user32()
    wanted = {int(p) for p in pids}
    hosts = {pid for pid, name in _user_processes() if name.lower() == FRAME_HOST}
    targets: list[int] = []

    def hosts_wanted(frame) -> bool:
        found = []

        def child(hwnd, _lparam):
            if _window_pid(user32, hwnd) in wanted:
                found.append(hwnd)
                return 0
            return 1

        user32.EnumChildWindows(frame, WNDENUMPROC(child), None)
        return bool(found)

    def top(hwnd, _lparam):
        if not hwnd or not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
            return 1
        pid = _window_pid(user32, hwnd)
        if pid in wanted or (pid in hosts and hosts_wanted(hwnd)):
            targets.append(hwnd)
        return 1

    user32.EnumWindows(WNDENUMPROC(top), None)
    for hwnd in targets:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    return len(targets)


_AUDIO_EXECUTOR: ThreadPoolExecutor | None = None
_AUDIO_LOCK = threading.Lock()


def _com_thread_init() -> None:
    """COM als MTA für den Audio-Thread (app/wincom.py): Der Thread wartet nur auf Aufträge und hat keine
    Nachrichtenschleife – als STA bekäme er ein verstecktes Fenster, das Broadcasts anderer Programme blockiert."""
    from app.wincom import init_mta

    init_mta()


def _audio_call(func):
    """COM-Aufrufe der Lautstärke laufen in EINEM festen Thread mit initialisiertem COM.

    Der Thread lebt bis zum Prozessende (kein CoUninitialize, solange noch COM-Zeiger leben).
    """
    global _AUDIO_EXECUTOR
    if not is_windows():
        raise ToolError(f"Lautstärke ist {NOT_WINDOWS}.")
    with _AUDIO_LOCK:
        if _AUDIO_EXECUTOR is None:
            _AUDIO_EXECUTOR = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="jarvis-audio", initializer=_com_thread_init
            )
        executor = _AUDIO_EXECUTOR
    try:
        return executor.submit(func).result(timeout=10)
    except ToolError:
        raise
    except TimeoutError:
        raise ToolError("Windows-Audio antwortet nicht (Zeitüberschreitung).") from None
    except Exception as exc:  # COMError/OSError aus pycaw, z. B. ohne Audiogerät
        raise ToolError(f"Lautstärke nicht verfügbar ({type(exc).__name__}).") from None


def _endpoint():  # pragma: no cover - nur unter Windows
    from pycaw.pycaw import AudioUtilities

    with warnings.catch_warnings():  # pycaw warnt bei einzelnen unlesbaren Geräte-Eigenschaften
        warnings.simplefilter("ignore")
        device = AudioUtilities.GetSpeakers()
    if device is None:
        raise ToolError("Kein Audio-Ausgabegerät gefunden.")
    return device.EndpointVolume


def _volume_get() -> tuple[float, bool]:
    """(Lautstärke 0.0–1.0, stumm) des Standard-Ausgabegeräts."""

    def work():  # pragma: no cover - nur unter Windows
        ep = _endpoint()
        return float(ep.GetMasterVolumeLevelScalar()), bool(ep.GetMute())

    return _audio_call(work)


def _volume_set(scalar: float) -> None:
    scalar = max(0.0, min(1.0, float(scalar)))

    def work():  # pragma: no cover - nur unter Windows
        _endpoint().SetMasterVolumeLevelScalar(scalar, None)

    _audio_call(work)


def _volume_mute(muted: bool) -> None:
    def work():  # pragma: no cover - nur unter Windows
        _endpoint().SetMute(bool(muted), None)

    _audio_call(work)


_STARTED: dict[int, subprocess.Popen] = {}


def _start_process(command: list[str]) -> int:
    """Programm aus der festen Liste starten (Argumentliste, nie shell=True)."""
    _refuse_unless_windows("Programme starten")
    for pid, proc in list(_STARTED.items()):  # beendete Prozesse vergessen
        if proc.poll() is not None:
            _STARTED.pop(pid, None)
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    proc = subprocess.Popen(
        list(command),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )
    _STARTED[proc.pid] = proc
    return proc.pid


def _protected_pids() -> set[int]:
    """JARVIS selbst und seine Elternprozesse (venv-Launcher) werden nie angefasst."""
    pids = {os.getpid()}
    try:
        pids.update(p.pid for p in psutil.Process().parents())
    except psutil.Error:
        pass
    return pids


def _current_username() -> str | None:
    try:
        return psutil.Process().username()
    except psutil.Error:
        return None


def _user_processes() -> list[tuple[int, str]]:
    """(PID, Prozessname) aller Prozesse des angemeldeten Benutzers (ohne JARVIS selbst)."""
    _refuse_unless_windows("Programmliste")
    me = _current_username()
    if me is None:
        return []
    skip = _protected_pids()
    result = []
    for proc in psutil.process_iter(["name", "username"], ad_value=None):
        info = proc.info
        if proc.pid in skip or not info.get("name") or info.get("username") != me:
            continue
        result.append((proc.pid, str(info["name"])))
    return result


def _soft_close(pid: int) -> None:  # pragma: no cover - nur unter Windows
    """taskkill ohne /F schickt WM_CLOSE an die Fenster des Prozesses (wie scripts_stop)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(["taskkill", "/PID", str(int(pid))], capture_output=True, timeout=5, creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        pass


def _close_processes(pids: list[int], process_name: str, grace: float = CLOSE_GRACE_SECONDS) -> str:
    """Prozesse erst sanft schließen, nach ``grace`` Sekunden hart beenden.

    Ergebnis: "closed" (von selbst beendet), "killed" (hart beendet), "killed_no_window" (kein Fenster zum
    Schließen gefunden, hart beendet) oder "not_running". Vor dem Schließen wird jeder Prozess erneut geprüft
    (gleicher Name, gleicher Benutzer, nicht JARVIS selbst) und unmittelbar vor jeder Schließ-Anfrage, ob es
    noch derselbe Prozess ist (psutil vergleicht die Startzeit) – Schutz gegen wiederverwendete PIDs.
    """
    _refuse_unless_windows("Programme schließen")
    me = _current_username()
    skip = _protected_pids()
    procs: list[psutil.Process] = []
    for pid in pids:
        try:
            proc = psutil.Process(int(pid))
            if proc.pid in skip or proc.name().lower() != process_name.lower() or proc.username() != me:
                continue
            procs.append(proc)
        except psutil.Error:
            continue
    if not procs:
        return "not_running"
    asked = _close_windows([p.pid for p in procs if p.is_running()])
    if not asked:
        # Kein sichtbares Fenster gefunden: taskkill ohne /F versucht es mit allen Fenstern des Prozesses.
        for proc in procs:
            if proc.is_running():
                _soft_close(proc.pid)
    _gone, alive = psutil.wait_procs(procs, timeout=grace)
    alive = [p for p in alive if p.is_running()]  # PID neu vergeben = der ursprüngliche Prozess ist weg
    for proc in alive:
        try:
            proc.kill()  # psutil prüft selbst noch einmal gegen wiederverwendete PIDs
        except psutil.Error:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=3)
        return "killed" if asked else "killed_no_window"
    return "closed"


def _resolve_host(host: str, timeout: float = DNS_TIMEOUT) -> list[str]:
    """IP-Adressen eines Hostnamens (getaddrinfo wie beim Browser). Leer = nicht auflösbar/zu langsam."""
    found: list[str] = []

    def work() -> None:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except (OSError, UnicodeError):
            return
        found.extend(str(info[4][0]) for info in infos)

    thread = threading.Thread(target=work, name="jarvis-dns", daemon=True)
    thread.start()
    thread.join(timeout)
    return [] if thread.is_alive() else list(found)


def _open_in_browser(url: str) -> None:
    """Geprüfte http(s)-Adresse im Standardbrowser öffnen (ShellExecute über os.startfile)."""
    _refuse_unless_windows("Webseiten öffnen")
    try:
        os.startfile(url)  # type: ignore[attr-defined]  # nur unter Windows vorhanden
    except OSError as exc:
        raise ToolError(f"Browser konnte nicht geöffnet werden: {exc.strerror or exc}") from None


# --------------------------------------------------------------------------------------------
# Prüfungen
# --------------------------------------------------------------------------------------------


def _require_enabled(ctx: ToolContext) -> DesktopConfig:
    cfg = ctx.config.desktop
    if not cfg.enabled:
        raise ToolError("PC-Steuerung ist ausgeschaltet (desktop.enabled in config.yaml).")
    return cfg


def _app(ctx: ToolContext, app_id: str) -> DesktopAppConfig:
    app = ctx.config.desktop.app(app_id.strip().lower())
    if app is None:
        known = ", ".join(a.id for a in ctx.config.desktop.apps) or "keine"
        raise ToolError(f"Unbekanntes Programm '{app_id}'. Verfügbar: {known}.")
    return app


def _app_pids(app: DesktopAppConfig) -> list[int]:
    name = app.process_name.lower()
    return [pid for pid, pname in _user_processes() if pname.lower() == name]


def _domain_allowed(host: str, allowed: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in allowed)


def ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True nur für öffentliche Adressen – auch in IPv6 verpackte IPv4-Adressen werden geprüft."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:
            return False
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is None and any(ip in net for net in _EMBEDDED_V4_NETS):
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if inner is not None and not inner.is_global:
            return False
    return ip.is_global


LOCAL_TARGET = "Lokale oder private Adressen öffne ich nicht (Schutz der Geräte im Heimnetz)."
# desktop.allowed_domains ist eine Positivliste – wer dort fritz.box einträgt, sperrt damit alle anderen Seiten.
ALLOWLIST_NOTE = (
    "Achtung: Sobald desktop.allowed_domains nicht leer ist, sind NUR noch die dort eingetragenen Domains erlaubt."
)


def _quote_non_ascii(text: str) -> str:
    return "".join(c if ord(c) < 128 else "".join(f"%{b:02X}" for b in c.encode("utf-8")) for c in text)


def validate_url(url: str, cfg: DesktopConfig) -> tuple[str, str]:
    """Prüft eine Adresse für desktop_open_url. Ergebnis: (normalisierte URL, Host). Wirft ToolError."""
    if not cfg.allow_open_url:
        raise ToolError("Webseiten öffnen ist ausgeschaltet (desktop.allow_open_url in config.yaml).")
    url = url.strip()
    if not url or len(url) > MAX_URL_LENGTH:
        raise ToolError(f"Adresse fehlt oder ist zu lang (höchstens {MAX_URL_LENGTH} Zeichen).")
    if any(ord(c) < 0x21 or ord(c) == 0x7F or c == "\\" or c.isspace() for c in url):
        raise ToolError("Die Adresse enthält ungültige Zeichen (Leerzeichen, Steuerzeichen oder \\).")
    try:
        parts = urlsplit(url)
    except ValueError:  # z. B. "http://[::1" (ungültige IPv6-Klammer)
        raise ToolError("Die Adresse ist ungültig.") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not url[len(parts.scheme):].startswith("://"):
        raise ToolError("Nur http- und https-Adressen sind erlaubt (z. B. https://example.org).")
    if not parts.netloc:
        raise ToolError("Die Adresse enthält keinen Host.")
    if "@" in parts.netloc:
        raise ToolError("Adressen mit Benutzername oder Passwort sind nicht erlaubt.")
    try:
        port = parts.port
        raw_host = parts.hostname or ""
    except ValueError:
        raise ToolError("Ungültiger Host oder Port in der Adresse.") from None
    try:
        ip = ipaddress.ip_address(raw_host)
    except ValueError:
        ip = None
    if ip is not None:
        if not ip_is_public(ip):
            raise ToolError(LOCAL_TARGET)
        if cfg.allowed_domains:
            raise ToolError("Diese Adresse ist nicht freigegeben (desktop.allowed_domains enthält nur Domains).")
        host = raw_host
        host_part = f"[{host}]" if ip.version == 6 else host
    else:
        try:
            host = normalize_domain(raw_host)
        except ValueError:
            raise ToolError(f"'{raw_host}' ist kein gültiger Domainname.") from None
        labels = host.split(".")
        if _ENDS_IN_NUMBER.match(labels[-1]):
            raise ToolError(LOCAL_TARGET)
        explicitly_allowed = _domain_allowed(host, cfg.allowed_domains)
        if cfg.allowed_domains and not explicitly_allowed:
            allowed = ", ".join(cfg.allowed_domains)
            raise ToolError(
                f"Die Domain {host} ist nicht freigegeben (erlaubt: {allowed}). desktop.allowed_domains ist eine "
                "Liste der einzig erlaubten Domains – leer ([]) = alle öffentlichen Domains."
            )
        is_local = len(labels) < 2 or any(host == s or host.endswith("." + s) for s in LOCAL_SUFFIXES)
        if is_local and not explicitly_allowed:
            raise ToolError(
                f"{host} ist ein Name im Heimnetz – solche Adressen öffne ich nur, wenn sie in "
                f"desktop.allowed_domains stehen. {ALLOWLIST_NOTE}"
            )
        host_part = host
    netloc = host_part if port is None else f"{host_part}:{port}"
    rest = urlunsplit(("", "", _quote_non_ascii(parts.path), _quote_non_ascii(parts.query),
                       _quote_non_ascii(parts.fragment)))
    if rest and not rest.startswith(("/", "?", "#")):
        rest = "/" + rest
    normalized = f"{scheme}://{netloc}{rest}"
    if not _URL_SAFE.match(normalized) or len(normalized) > MAX_URL_LENGTH:
        raise ToolError("Die Adresse enthält ungültige Zeichen.")
    return normalized, host


# --------------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------------


def desktop_apps_list(ctx: ToolContext, _p) -> dict:
    cfg = ctx.config.desktop
    available = cfg.enabled and is_windows()
    running: set[str] = set()
    if available:
        running = {name.lower() for _pid, name in _user_processes()}
    return {
        "enabled": cfg.enabled,
        "available": available,
        "apps": [
            {
                "id": a.id,
                "label": a.label,
                "configured": a.is_configured,
                "running": a.process_name.lower() in running,
            }
            for a in cfg.apps
        ],
    }


def desktop_app_start(ctx: ToolContext, p: AppParams) -> dict:
    _require_enabled(ctx)
    app = _app(ctx, p.app_id)
    if not app.is_configured:
        raise ToolError(f"Programm '{app.label}' ist noch nicht eingerichtet: command in config.yaml enthält TODO.")
    _refuse_unless_windows("Programme starten")
    if _app_pids(app):
        # Kein zweites Exemplar (kleine Modelle wiederholen Aufrufe gern) – nach vorne holen geht separat.
        return {"status": "already_running", "id": app.id, "label": app.label,
                "message": f"{app.label} läuft schon (nach vorne holen: „{app.label} nach vorne“)."}
    try:
        _start_process(list(app.command))
    except FileNotFoundError:
        raise ToolError(f"Programm für '{app.label}' nicht gefunden: erstes Element von command prüfen.") from None
    except OSError as exc:
        raise ToolError(f"Start von '{app.label}' fehlgeschlagen: {exc.strerror or exc}") from None
    return {"status": "started", "id": app.id, "label": app.label, "message": f"{app.label} gestartet."}


def desktop_app_close(ctx: ToolContext, p: AppParams) -> dict:
    """Schließen kann ungespeicherte Arbeit kosten – deshalb nur mit Bestätigung in der Oberfläche."""
    _require_enabled(ctx)
    app = _app(ctx, p.app_id)
    _refuse_unless_windows("Programme schließen")
    if not _app_pids(app):
        return {"status": "not_running", "id": app.id, "label": app.label, "message": f"{app.label} läuft nicht."}
    result = pc.request_confirmation(
        ctx,
        "desktop_app_close",
        {"app_id": app.id},
        prompt=f"{app.label} schließen? Nicht gespeicherte Änderungen gehen dabei eventuell verloren.",
        message=f"{app.label} schließen? Bitte in der Oberfläche bestätigen.",
    )
    return {**result, "id": app.id, "label": app.label}


def _do_close(ctx: ToolContext, params: dict[str, Any]) -> dict:
    """Ausführung nach Bestätigung – nur über pc.confirm() (Route /api/confirm/{id})."""
    _require_enabled(ctx)
    app = _app(ctx, str(params.get("app_id", "")))
    _refuse_unless_windows("Programme schließen")
    pids = _app_pids(app)
    if not pids:
        message = f"{app.label} läuft nicht mehr."
        return {"status": "not_running", "id": app.id, "label": app.label, "message": message}
    outcome = _close_processes(pids, app.process_name, CLOSE_GRACE_SECONDS)
    status, message = {
        "killed": ("killed", f"{app.label} hat nicht reagiert und wurde hart beendet."),
        "killed_no_window": ("killed", f"{app.label} hatte kein Fenster zum Schließen und wurde beendet."),
        "not_running": ("not_running", f"{app.label} läuft nicht mehr."),
    }.get(outcome, ("closed", f"{app.label} geschlossen."))
    return {"status": status, "id": app.id, "label": app.label, "message": message}


pc.register_confirm_action("desktop_app_close", _do_close)


def desktop_focus(ctx: ToolContext, p: AppParams) -> dict:
    _require_enabled(ctx)
    app = _app(ctx, p.app_id)
    _refuse_unless_windows("Fenster nach vorne holen")
    pids = _app_pids(app)
    base = {"id": app.id, "label": app.label}
    if not pids:
        return {**base, "status": "not_running", "message": f"{app.label} läuft nicht."}
    outcome = _focus_window(pids, app.window_title)
    if outcome == "focused":
        return {**base, "status": "focused", "message": f"{app.label} ist im Vordergrund."}
    if outcome == "refused":
        # Windows lässt nur das Programm im Vordergrund den Fokus wechseln – wer gerade am PC (z. B. im Browser)
        # arbeitet, bekommt das fast immer; vom Handy klappt es meist, wenn der PC ein paar Minuten ruht.
        return {
            **base,
            "status": "refused",
            "message": f"Windows hat den Fokuswechsel verweigert; {app.label} wartet in der Taskleiste "
            "(blinkt) – dort anklicken.",
        }
    if outcome == "hung":
        return {**base, "status": "not_responding", "message": f"{app.label} reagiert gerade nicht."}
    return {**base, "status": "no_window", "message": f"Kein sichtbares Fenster von {app.label} gefunden."}


def _clamp_percent(value: float) -> int:
    return int(round(max(0.0, min(100.0, float(value)))))


def desktop_volume(ctx: ToolContext, p: VolumeParams) -> dict:
    _require_enabled(ctx)
    _refuse_unless_windows("Lautstärke")
    level, muted = _volume_get()
    current = _clamp_percent(level * 100)
    if p.action == "set":
        _volume_set(_clamp_percent(p.percent or 0) / 100)
        if muted and _clamp_percent(p.percent or 0) > 0:
            _volume_mute(False)
    elif p.action == "up":
        _volume_set(_clamp_percent(current + p.step) / 100)
        if muted:
            _volume_mute(False)
    elif p.action == "down":
        _volume_set(_clamp_percent(current - p.step) / 100)
    elif p.action == "mute":
        _volume_mute(True)
    elif p.action == "unmute":
        _volume_mute(False)
    if p.action != "get":
        level, muted = _volume_get()
    return {"action": p.action, "percent": _clamp_percent(level * 100), "muted": bool(muted)}


def desktop_media(ctx: ToolContext, p: MediaParams) -> dict:
    _require_enabled(ctx)
    _refuse_unless_windows("Mediensteuerung")
    vk, scan = MEDIA_KEYS[p.action]
    _send_media_key(vk, scan)
    return {"status": "sent", "action": p.action}


def desktop_lock(ctx: ToolContext, _p) -> dict:
    _require_enabled(ctx)
    _refuse_unless_windows("PC sperren")
    if not _lock_workstation():
        raise ToolError("Windows hat das Sperren abgelehnt.")
    return {"status": "locked", "message": "PC gesperrt."}


def check_resolved_host(host: str, cfg: DesktopConfig) -> None:
    """Ein Domainname darf nicht auf eine Adresse im Heimnetz/auf diesen PC zeigen (z. B. 192.168.1.50.nip.io).

    Domains aus desktop.allowed_domains sind ausdrücklich freigegeben (z. B. fritz.box). Ist der Name nicht
    auflösbar, kann der Browser ihn auch nicht öffnen – dann bleibt es bei der Fehlerseite des Browsers.
    """
    try:
        ipaddress.ip_address(host)
        return  # IP-Adressen hat validate_url schon geprüft
    except ValueError:
        pass
    if _domain_allowed(host, cfg.allowed_domains):
        return
    for raw in _resolve_host(host):
        try:
            ip = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            continue
        if not ip_is_public(ip):
            raise ToolError(
                f"{host} zeigt auf eine Adresse im Heimnetz oder auf diesen PC – solche Adressen öffne ich nur, "
                f"wenn sie in desktop.allowed_domains stehen. {ALLOWLIST_NOTE}"
            )


def desktop_open_url(ctx: ToolContext, p: UrlParams) -> dict:
    cfg = _require_enabled(ctx)
    url, host = validate_url(p.url, cfg)
    _refuse_unless_windows("Webseiten öffnen")
    check_resolved_host(host, cfg)
    _open_in_browser(url)
    return {"status": "opened", "url": url, "host": host, "message": f"{host} im Browser geöffnet."}


def _apps_hint(cfg: DesktopConfig) -> str:
    apps = ", ".join(f"{a.id} ({a.label})" for a in cfg.apps)
    return f" Erlaubte app_id: {apps}." if apps else " Es sind keine Programme freigegeben."


def register(registry: Registry) -> None:
    hint = _apps_hint(registry.ctx.config.desktop)
    registry.tool(
        "desktop_apps_list",
        "Listet die freigegebenen Programme (id, label, running). Nur diese IDs sind erlaubt.",
    )(desktop_apps_list)
    registry.tool(
        "desktop_app_start", "Startet ein freigegebenes Programm (läuft es schon, passiert nichts)." + hint, AppParams
    )(desktop_app_start)
    registry.tool(
        "desktop_app_close",
        "Schließt ein freigegebenes Programm. Fordert nur eine Bestätigung an; der User muss in der "
        "Oberfläche auf 'Bestätigen' tippen." + hint,
        AppParams,
    )(desktop_app_close)
    registry.tool(
        "desktop_focus",
        "Holt das Fenster eines freigegebenen Programms in den Vordergrund (Windows verweigert das oft, während "
        "jemand am PC arbeitet – dann blinkt es in der Taskleiste)." + hint,
        AppParams,
    )(desktop_focus)
    registry.tool(
        "desktop_volume",
        "PC-Lautstärke: set (percent 0–100), up/down (step), mute, unmute oder get.",
        VolumeParams,
    )(desktop_volume)
    registry.tool(
        "desktop_media",
        "Medientaste drücken: play_pause, next, previous oder stop (wirkt auf den aktiven Player).",
        MediaParams,
    )(desktop_media)
    registry.tool("desktop_lock", "Sperrt den PC (Sperrbildschirm).")(desktop_lock)
    registry.tool(
        "desktop_open_url",
        "Öffnet eine http/https-Webseite im Standardbrowser des PCs. Nur Adressen, die der User "
        "selbst genannt hat.",
        UrlParams,
    )(desktop_open_url)
