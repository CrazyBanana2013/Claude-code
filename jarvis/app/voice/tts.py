"""Sprachausgabe am PC: Windows-SAPI (SAPI.SpVoice über comtypes) in einem eigenen Thread.

Warum SAPI über comtypes und nicht System.Speech per PowerShell?
- Kein Unterprozess, keine Kommandozeile: Der Text wird als BSTR an ISpeechVoice::Speak
  übergeben, nie von einer Shell gelesen – Command-Injection ist damit ausgeschlossen.
- Unterbrechen ist eingebaut: SVSFPurgeBeforeSpeak verwirft laufende und wartende Ausgaben.
- comtypes ist ohnehin eine Abhängigkeit (Windows), offline, keine PowerShell-Startzeit pro Satz.
Die Flags setzen immer SVSFIsNotXML (Text wird nie als SAPI-XML gelesen, kann also weder Stimme,
Lautstärke noch Dateien steuern) und nie SVSFIsFilename (Text wäre sonst ein Dateipfad).
Werte laut sapi.idl (SpeechVoiceSpeakFlags): SVSFlagsAsync = 1, SVSFPurgeBeforeSpeak = 2,
SVSFIsFilename = 4, SVSFIsXML = 8, SVSFIsNotXML = 16.

COM-Objekte gehören zu dem Thread (Apartment), in dem sie erzeugt wurden. Deshalb erzeugt,
benutzt und gibt nur der Worker-Thread das SpVoice-Objekt frei; alle anderen Threads legen nur
Befehle in einen Ein-Platz-Puffer („der neueste Text gewinnt“).
"""

from __future__ import annotations

import gc
import logging
import os
import re
import threading
from typing import Any, Protocol

log = logging.getLogger("jarvis.voice")

MAX_TEXT = 1000

SVSF_ASYNC = 1
SVSF_PURGE_BEFORE_SPEAK = 2
SVSF_IS_FILENAME = 4
SVSF_IS_XML = 8
SVSF_IS_NOT_XML = 16
SPEAK_FLAGS = SVSF_ASYNC | SVSF_PURGE_BEFORE_SPEAK | SVSF_IS_NOT_XML

GERMAN_PRIMARY_LANGID = 0x07  # de-DE 0x407, de-AT 0xC07, de-CH 0x807, …

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MARKUP = re.compile(r"[*`]+")


def is_windows() -> bool:
    return os.name == "nt"


def clean_text(text: str, limit: int = MAX_TEXT) -> str:
    """Steuerzeichen und Markdown-Sternchen entfernen, Leerraum glätten, auf `limit` kürzen."""
    text = _MARKUP.sub("", _CONTROL_CHARS.sub(" ", str(text)))
    text = " ".join(text.split())
    if len(text) > limit:
        cut = text[:limit]
        space = cut.rfind(" ")
        text = cut[:space] if space > limit // 2 else cut
    return text


class Backend(Protocol):
    voice_name: str | None
    note: str | None

    def open(self) -> None: ...
    def speak(self, text: str) -> None: ...
    def wait(self, timeout_ms: int) -> bool: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


# --- Stimme auswählen -----------------------------------------------------------------
def _token_description(token: Any) -> str:
    try:
        return str(token.GetDescription(0))
    except Exception:
        return ""


def _is_german(token: Any) -> bool:
    try:
        raw = str(token.GetAttribute("Language") or "")
    except Exception:
        return False
    for part in raw.split(";"):
        try:
            if int(part.strip(), 16) & 0x3FF == GERMAN_PRIMARY_LANGID:
                return True
        except ValueError:
            continue
    return False


def choose_voice(tokens: list[Any], preferred: str = "") -> tuple[Any | None, str | None, str | None]:
    """(Token, Name, Hinweis): gewünschte Stimme (Teilstring) > erste deutsche > Standardstimme."""
    described = [(t, _token_description(t)) for t in tokens]
    note = None
    wanted = (preferred or "").strip().lower()
    if wanted:
        for token, name in described:
            if wanted in name.lower():
                return token, name, None
        note = f"Stimme '{preferred}' nicht gefunden."
    for token, name in described:
        if _is_german(token):
            return token, name, (f"{note} Es spricht '{name}'." if note else None)
    hint = "Keine deutsche SAPI-Stimme installiert – es spricht die Windows-Standardstimme."
    return None, None, f"{note} {hint}" if note else hint


# --- Windows-Backend --------------------------------------------------------------------
class SapiBackend:
    """SAPI.SpVoice – alle Methoden laufen im Worker-Thread der TTSEngine."""

    def __init__(self, preferred_voice: str = "", rate: int = 0, volume: int = 100) -> None:
        self.preferred_voice = preferred_voice or ""
        self.rate = max(-10, min(10, int(rate)))
        self.volume = max(0, min(100, int(volume)))
        self.voice_name: str | None = None
        self.note: str | None = None
        self._voice: Any = None
        self._comtypes: Any = None
        self._co_initialized = False

    def open(self) -> None:
        import comtypes  # erster Import initialisiert COM für diesen Thread
        import comtypes.client

        self._comtypes = comtypes
        try:
            comtypes.CoInitializeEx()
            self._co_initialized = True
        except OSError:  # schon in anderem Modus initialisiert – dann ohne eigenes Gegenstück
            self._co_initialized = False
        voice = comtypes.client.CreateObject("SAPI.SpVoice", dynamic=True)
        tokens_obj = voice.GetVoices("", "")
        tokens = [tokens_obj.Item(i) for i in range(int(tokens_obj.Count))]
        token, name, self.note = choose_voice(tokens, self.preferred_voice)
        if token is not None:
            voice.Voice = token
        voice.Rate = self.rate
        voice.Volume = self.volume
        self.voice_name = name or _token_description(voice.Voice) or None
        del tokens, tokens_obj, token
        self._voice = voice

    def speak(self, text: str) -> None:
        self._voice.Speak(text, SPEAK_FLAGS)

    def wait(self, timeout_ms: int) -> bool:
        return bool(self._voice.WaitUntilDone(int(timeout_ms)))

    def stop(self) -> None:
        self._voice.Speak("", SVSF_ASYNC | SVSF_PURGE_BEFORE_SPEAK | SVSF_IS_NOT_XML)

    def close(self) -> None:
        if self._voice is not None:
            try:
                self.stop()
            except Exception:
                pass
        # COM-Referenzen freigeben, BEVOR das Apartment dieses Threads endet.
        self._voice = None
        gc.collect()
        if self._co_initialized and self._comtypes is not None:
            self._comtypes.CoUninitialize()
            self._co_initialized = False


def create_backend(settings: Any) -> Backend:
    """Windows-API-Wrapper (in Tests ersetzt)."""
    return SapiBackend(
        preferred_voice=getattr(settings, "voice", "") or "",
        rate=getattr(settings, "rate", 0),
        volume=getattr(settings, "volume", 100),
    )


# --- Engine mit Worker-Thread -----------------------------------------------------------
class TTSEngine:
    """Nicht blockierende Sprachausgabe. Neuer Text unterbricht und verwirft ältere Ausgaben."""

    POLL_MS = 100

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._cond = threading.Condition()
        self._pending: tuple[str, str] | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closed = False
        self._error: str | None = None
        self._voice_name: str | None = None
        self._note: str | None = None
        self._speaking = False

    # --- Zustand ---------------------------------------------------------------------
    @property
    def speak_replies(self) -> bool:
        """Vorgabe für /api/chat, wenn die Anfrage kein "speak" mitschickt."""
        return bool(getattr(self.settings, "speak_replies", False))

    def unavailable_reason(self) -> str | None:
        if self.settings is None:
            return "Sprachausgabe ist nicht konfiguriert (Abschnitt voice fehlt in config.yaml)."
        if not self.settings.enabled:
            return "Sprachausgabe ist ausgeschaltet (voice.tts.enabled in config.yaml)."
        if not is_windows():
            return "Sprachausgabe ist nur auf dem Windows-PC verfügbar."
        return self._error

    @property
    def available(self) -> bool:
        return self.unavailable_reason() is None

    def warm_up(self, timeout: float = 3.0) -> None:
        """Startet den Sprach-Thread und wartet kurz auf die Stimme (blockiert – nicht im Event-Loop)."""
        if self.available:
            self._ensure_thread()
            self._ready.wait(timeout)

    def status(self) -> dict:
        reason = self.unavailable_reason()
        return {
            "available": reason is None,
            "enabled": bool(getattr(self.settings, "enabled", False)),
            "speak_replies": bool(getattr(self.settings, "speak_replies", False)),
            "voice": self._voice_name,
            "reason": reason,
            "note": self._note,
            "speaking": self._speaking,
        }

    # --- Befehle (aus beliebigen Threads, kehren sofort zurück) --------------------------
    def speak(self, text: str) -> bool:
        """Text zum Sprechen einreihen. False, wenn nichts zu sagen ist oder TTS nicht geht."""
        text = clean_text(text)
        if not text or not self.available:
            return False
        if not self._ensure_thread():
            return False
        with self._cond:
            self._pending = ("speak", text)  # noch nicht Gesprochenes verfällt
            self._cond.notify()
        return True

    def stop(self) -> None:
        with self._cond:
            if self._thread is None or self._closed:
                return
            self._pending = ("stop", "")
            self._cond.notify()

    def close(self, timeout: float = 3.0) -> None:
        with self._cond:
            self._closed = True
            self._pending = ("close", "")
            self._cond.notify()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    # --- Worker ----------------------------------------------------------------------
    def _ensure_thread(self) -> bool:
        with self._cond:
            if self._closed:
                return False
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="jarvis-tts", daemon=True)
                self._thread.start()
            return True

    def _run(self) -> None:
        backend: Backend | None = None
        try:
            backend = create_backend(self.settings)
            backend.open()
        except Exception as exc:
            log.warning("Sprachausgabe nicht verfügbar: %s", exc)
            self._error = f"Sprachausgabe konnte nicht gestartet werden: {exc}"
            if backend is not None:
                try:
                    backend.close()  # z. B. CoUninitialize nach CoInitializeEx
                except Exception:
                    pass
            self._ready.set()
            return
        self._voice_name = backend.voice_name
        self._note = backend.note
        self._ready.set()
        active = False
        try:
            while True:
                with self._cond:
                    while self._pending is None and not active:
                        self._cond.wait()
                    command, self._pending = self._pending, None
                if command is None:  # spricht gerade, kein neuer Befehl
                    try:
                        done = backend.wait(self.POLL_MS)
                    except Exception:
                        done = True
                    if done:
                        active = self._speaking = False
                    continue
                kind, text = command
                if kind == "close":
                    break
                try:
                    if kind == "speak":
                        backend.speak(text)  # Purge-Flag unterbricht die laufende Ausgabe
                        active = True
                    else:
                        backend.stop()
                        active = False
                except Exception as exc:
                    log.warning("Sprachausgabe fehlgeschlagen: %s", exc)
                    active = False
                self._speaking = active
        finally:
            self._speaking = False
            try:
                backend.close()
            except Exception as exc:  # pragma: no cover - nur Protokoll
                log.warning("Sprachausgabe schließen: %s", exc)
