"""Sprachausgabe: Worker-Thread, Unterbrechen/Verwerfen, SAPI-Backend mit gefälschtem comtypes."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
import types

import pytest

from app.config import TTSConfig
from app.voice import tts
from app.voice.tts import SapiBackend, TTSEngine, choose_voice, clean_text


class FakeBackend:
    """Simuliert SAPI: speak() dauert `duration` Sekunden, ein neuer speak()/stop() unterbricht."""

    def __init__(self, duration: float = 30.0) -> None:
        self.duration = duration
        self.voice_name = "Microsoft Hedda Desktop - German"
        self.note = None
        self.events: queue.Queue = queue.Queue()
        self.calls: list[tuple] = []
        self.threads: set[str] = set()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self._done_at = 0.0

    def _record(self, *call) -> None:
        self.threads.add(threading.current_thread().name)
        self.calls.append(call)
        self.events.put(call)

    def open(self) -> None:
        self._record("open")

    def speak(self, text: str) -> None:
        self.entered.set()
        assert self.release.wait(10)
        self._done_at = time.monotonic() + self.duration
        self._record("speak", text)

    def wait(self, timeout_ms: int) -> bool:
        time.sleep(min(timeout_ms, 10) / 1000)
        return time.monotonic() >= self._done_at

    def stop(self) -> None:
        self._done_at = 0.0
        self._record("stop")

    def close(self) -> None:
        self._record("close")


def next_event(backend: FakeBackend, kind: str, timeout: float = 5.0) -> tuple:
    deadline = time.monotonic() + timeout
    while True:
        event = backend.events.get(timeout=max(0.01, deadline - time.monotonic()))
        if event[0] == kind:
            return event


def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Bedingung nicht erfüllt")
        time.sleep(0.01)


@pytest.fixture
def windows(monkeypatch):
    """Windows vortäuschen; create_backend liefert ein FakeBackend."""
    backend = FakeBackend()
    monkeypatch.setattr(tts, "is_windows", lambda: True)
    monkeypatch.setattr(tts, "create_backend", lambda settings: backend)
    return backend


@pytest.fixture
def make_engine():
    engines: list[TTSEngine] = []

    def make(**settings) -> TTSEngine:
        engine = TTSEngine(TTSConfig(**settings))
        engines.append(engine)
        return engine

    yield make
    for engine in engines:
        engine.close()


# --- Text --------------------------------------------------------------------------------
def test_clean_text():
    assert clean_text("  Hallo\n\tWelt\x00!  ") == "Hallo Welt !"
    assert clean_text("**Licht** ist `an`") == "Licht ist an"
    long = "wort " * 400
    cut = clean_text(long)
    assert len(cut) <= 1000 and cut.endswith("wort")
    assert clean_text("x" * 1500) == "x" * 1000
    assert clean_text("\n\x07  ") == ""


# --- Nicht Windows / ausgeschaltet --------------------------------------------------------
def test_refuses_cleanly_on_non_windows(monkeypatch, make_engine):
    monkeypatch.setattr(tts, "is_windows", lambda: False)
    monkeypatch.setattr(tts, "create_backend", lambda s: pytest.fail("kein Backend außerhalb von Windows"))
    engine = make_engine()
    engine.warm_up()
    assert engine.speak("Hallo") is False
    engine.stop()
    status = engine.status()
    assert status["available"] is False
    assert status["reason"] == "Sprachausgabe ist nur auf dem Windows-PC verfügbar."
    assert status["enabled"] is True and status["speak_replies"] is True
    assert engine._thread is None


def test_disabled(windows, make_engine):
    engine = make_engine(enabled=False)
    assert engine.speak("Hallo") is False
    assert "ausgeschaltet" in engine.status()["reason"]
    assert windows.calls == []


def test_missing_settings_is_unavailable():
    engine = TTSEngine(None)
    assert engine.speak("Hallo") is False
    assert engine.status()["available"] is False
    assert engine.speak_replies is False


# --- Worker-Thread ------------------------------------------------------------------------
def test_speak_returns_immediately_and_runs_in_worker(windows, make_engine):
    engine = make_engine()
    windows.release.clear()  # Backend „hängt“ beim Sprechen
    started = time.monotonic()
    assert engine.speak("Hallo Welt") is True
    assert time.monotonic() - started < 0.5
    windows.release.set()
    assert next_event(windows, "speak") == ("speak", "Hallo Welt")
    assert windows.threads == {"jarvis-tts"}
    status = engine.status()
    assert status["available"] is True
    assert status["voice"] == "Microsoft Hedda Desktop - German"
    wait_until(lambda: engine.status()["speaking"])


def test_new_text_interrupts_running_speech(windows, make_engine):
    engine = make_engine()
    engine.speak("Erster langer Satz")
    next_event(windows, "speak")
    engine.speak("Zweiter Satz")
    assert next_event(windows, "speak") == ("speak", "Zweiter Satz")
    # SAPI-Purge-Flag im speak() unterbricht – kein extra stop() nötig
    assert [c for c in windows.calls if c[0] != "open"] == [("speak", "Erster langer Satz"), ("speak", "Zweiter Satz")]


def test_pending_texts_are_purged_newest_wins(windows, make_engine):
    engine = make_engine()
    windows.release.clear()
    engine.speak("A")
    assert windows.entered.wait(5)  # Worker steckt in speak("A")
    for text in ("B", "C", "D"):
        assert engine.speak(text) is True
    windows.release.set()
    assert next_event(windows, "speak") == ("speak", "A")
    assert next_event(windows, "speak") == ("speak", "D")
    time.sleep(0.3)
    spoken = [c[1] for c in windows.calls if c[0] == "speak"]
    assert spoken == ["A", "D"]


def test_stop_purges(windows, make_engine):
    engine = make_engine()
    engine.speak("Bla bla")
    next_event(windows, "speak")
    engine.stop()
    next_event(windows, "stop")
    wait_until(lambda: not engine.status()["speaking"])


def test_speech_finishes_by_itself(make_engine, monkeypatch):
    backend = FakeBackend(duration=0.05)
    monkeypatch.setattr(tts, "is_windows", lambda: True)
    monkeypatch.setattr(tts, "create_backend", lambda s: backend)
    engine = make_engine()
    engine.speak("kurz")
    next_event(backend, "speak")
    wait_until(lambda: not engine.status()["speaking"])
    assert "stop" not in [c[0] for c in backend.calls]


def test_close_joins_thread_and_releases_backend(windows, make_engine):
    engine = make_engine()
    engine.speak("Hallo")
    next_event(windows, "speak")
    thread = engine._thread
    engine.close()
    assert not thread.is_alive()
    assert windows.calls[-1] == ("close",)
    assert engine.speak("noch was") is False


def test_open_failure_is_reported(monkeypatch, make_engine):
    monkeypatch.setattr(tts, "is_windows", lambda: True)

    class Broken(FakeBackend):
        def open(self):
            raise OSError("Klasse nicht registriert")

    broken = Broken()
    monkeypatch.setattr(tts, "create_backend", lambda s: broken)
    engine = make_engine()
    engine.warm_up()
    status = engine.status()
    assert status["available"] is False
    assert "konnte nicht gestartet werden: Klasse nicht registriert" in status["reason"]
    assert engine.speak("Hallo") is False
    assert broken.calls == [("close",)]  # halb geöffnetes Backend wird trotzdem freigegeben


def test_sapi_open_failure_balances_com_init(fake_comtypes, monkeypatch, make_engine):
    def no_sapi(progid, dynamic=False, **kwargs):
        raise OSError("Klasse nicht registriert")

    monkeypatch.setattr(sys.modules["comtypes.client"], "CreateObject", no_sapi)
    monkeypatch.setattr(tts, "is_windows", lambda: True)
    engine = make_engine()
    engine.warm_up()
    assert "Klasse nicht registriert" in engine.status()["reason"]
    assert fake_comtypes["init"] == fake_comtypes["uninit"] == ["jarvis-tts"]


def test_backend_errors_do_not_kill_worker(windows, make_engine, monkeypatch):
    engine = make_engine()
    original = windows.speak

    def flaky(text):
        if text == "kaputt":
            raise OSError("SPERR_DEVICE_BUSY")
        original(text)

    monkeypatch.setattr(windows, "speak", flaky)
    engine.speak("kaputt")
    time.sleep(0.2)
    engine.speak("geht wieder")
    assert next_event(windows, "speak") == ("speak", "geht wieder")


# --- Stimmenwahl --------------------------------------------------------------------------
class FakeToken:
    def __init__(self, description: str, language: str | None) -> None:
        self.description = description
        self.language = language

    def GetDescription(self, locale=0):
        return self.description

    def GetAttribute(self, name):
        assert name == "Language"
        if self.language is None:
            raise OSError("Attribut fehlt")
        return self.language


ZIRA = FakeToken("Microsoft Zira Desktop - English (United States)", "409")
HEDDA = FakeToken("Microsoft Hedda Desktop - German", "407")
SWISS = FakeToken("Anderer Anbieter - Deutsch (Schweiz)", "807;409")
BROKEN = FakeToken("Ohne Sprache", None)


def test_choose_first_german_voice():
    token, name, note = choose_voice([BROKEN, ZIRA, HEDDA, SWISS], "")
    assert token is HEDDA and name == HEDDA.description and note is None
    token, _, _ = choose_voice([ZIRA, SWISS], "")
    assert token is SWISS


def test_choose_preferred_voice_by_substring():
    token, name, note = choose_voice([HEDDA, ZIRA], "zira")
    assert token is ZIRA and note is None


def test_preferred_voice_missing_falls_back_to_german():
    token, _, note = choose_voice([ZIRA, HEDDA], "Katja")
    assert token is HEDDA
    assert "Katja" in note and "Hedda" in note


def test_no_german_voice_uses_default():
    token, name, note = choose_voice([ZIRA], "")
    assert token is None and name is None
    assert "Keine deutsche" in note


# --- SAPI-Backend mit gefälschtem comtypes ------------------------------------------------
class FakeTokens:
    def __init__(self, tokens):
        self._tokens = tokens
        self.Count = len(tokens)

    def Item(self, index):
        return self._tokens[index]


class FakeSpVoice:
    def __init__(self, tokens):
        self.tokens = tokens
        self.Voice = tokens[0] if tokens else FakeToken("Standard", "409")
        self.Rate = 0
        self.Volume = 100
        self.speak_calls: list[tuple[str, int, str]] = []
        self.get_voices_args = None

    def GetVoices(self, required, optional):
        self.get_voices_args = (required, optional)
        return FakeTokens(self.tokens)

    def Speak(self, text, flags):
        self.speak_calls.append((text, flags, threading.current_thread().name))
        return 1

    def WaitUntilDone(self, ms):
        return True


@pytest.fixture
def fake_comtypes(monkeypatch):
    record = {"init": [], "uninit": [], "created": [], "flags": []}
    monkeypatch.setattr(sys, "coinit_flags", 2, raising=False)  # STA – init_mta muss das überschreiben
    voice = FakeSpVoice([ZIRA, HEDDA])
    module = types.ModuleType("comtypes")
    client = types.ModuleType("comtypes.client")

    def co_init(flags=None):
        record["init"].append(threading.current_thread().name)
        record["flags"].append(flags)

    def co_uninit():
        record["uninit"].append(threading.current_thread().name)

    def create_object(progid, dynamic=False, **kwargs):
        record["created"].append((progid, dynamic, threading.current_thread().name))
        return voice

    module.CoInitializeEx = co_init
    module.CoUninitialize = co_uninit
    client.CreateObject = create_object
    module.client = client
    monkeypatch.setitem(sys.modules, "comtypes", module)
    monkeypatch.setitem(sys.modules, "comtypes.client", client)
    record["voice"] = voice
    return record


def test_sapi_backend_open_speak_close(fake_comtypes):
    backend = SapiBackend("", rate=25, volume=-3)
    backend.open()
    voice = fake_comtypes["voice"]
    assert fake_comtypes["created"][0][:2] == ("SAPI.SpVoice", True)
    assert voice.get_voices_args == ("", "")
    assert voice.Voice is HEDDA
    assert backend.voice_name == HEDDA.description
    assert (voice.Rate, voice.Volume) == (10, 0)  # geklemmt
    backend.speak("Hallo")
    text, flags, _ = voice.speak_calls[-1]
    assert text == "Hallo"
    assert flags == tts.SVSF_ASYNC | tts.SVSF_PURGE_BEFORE_SPEAK | tts.SVSF_IS_NOT_XML
    assert backend.wait(100) is True
    backend.close()
    assert voice.speak_calls[-1][0] == "" and voice.speak_calls[-1][1] & tts.SVSF_PURGE_BEFORE_SPEAK
    assert len(fake_comtypes["init"]) == len(fake_comtypes["uninit"]) == 1
    assert backend._voice is None
    # COM als MTA (COINIT_MULTITHREADED = 0): kein verstecktes STA-Fenster ohne Nachrichtenschleife
    assert fake_comtypes["flags"] == [0] and sys.coinit_flags == 0


@pytest.mark.parametrize(
    "text",
    [
        '<voice required="Gender=Male"/><volume level="100">laut</volume>',
        "C:\\Windows\\win.ini",
        '"; Remove-Item -Recurse C:\\ ; $(calc.exe) `whoami` & del *.* | shutdown /s',
    ],
)
def test_text_is_never_xml_filename_or_shell(fake_comtypes, monkeypatch, make_engine, text):
    """Text geht als Daten an Speak – nie als XML, Dateiname oder über eine Shell."""

    def no_process(*args, **kwargs):
        raise AssertionError("Sprachausgabe darf keinen Prozess starten")

    monkeypatch.setattr(subprocess, "Popen", no_process)
    monkeypatch.setattr(subprocess, "run", no_process)
    monkeypatch.setattr(tts, "is_windows", lambda: True)  # echtes create_backend → SapiBackend
    engine = make_engine()
    assert engine.speak(text) is True
    voice = fake_comtypes["voice"]
    expected = clean_text(text)  # nur Steuerzeichen, * und ` fallen weg
    assert "<voice" in expected or "win.ini" in expected or "$(calc.exe)" in expected
    wait_until(lambda: any(c[0] == expected for c in voice.speak_calls))
    spoken, flags, thread_name = next(c for c in voice.speak_calls if c[0] == expected)
    assert flags & tts.SVSF_IS_NOT_XML
    assert not flags & (tts.SVSF_IS_FILENAME | tts.SVSF_IS_XML)
    # COM wird im selben Thread initialisiert, benutzt und wieder freigegeben
    assert thread_name == "jarvis-tts"
    assert fake_comtypes["init"] == ["jarvis-tts"]
    assert fake_comtypes["created"][0][2] == "jarvis-tts"
    engine.close()
    assert fake_comtypes["uninit"] == ["jarvis-tts"]


def test_create_backend_passes_settings():
    backend = tts.create_backend(TTSConfig(voice="Hedda", rate=-3, volume=70))
    assert isinstance(backend, SapiBackend)
    assert (backend.preferred_voice, backend.rate, backend.volume) == ("Hedda", -3, 70)
