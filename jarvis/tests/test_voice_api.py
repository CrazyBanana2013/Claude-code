"""HTTP-Routen /api/voice/* und die Option "speak" von /api/chat."""

from __future__ import annotations

import asyncio
import tempfile
import time

import pytest
from starlette import formparsers

from app.voice import stt, tts
from tests.conftest import client_for
from tests.test_voice_stt import install_fake_whisper, make_audio, needs_audio
from tests.test_voice_tts import FakeBackend, next_event

pytestmark = pytest.mark.anyio

VOICE_ROUTES = [
    ("GET", "/api/voice/status"),
    ("POST", "/api/voice/speak"),
    ("POST", "/api/voice/stop"),
    ("POST", "/api/voice/stt"),
]
STT_ON = {"voice": {"stt": {"enabled": True, "max_seconds": 5}}}
WEBM_STUB = b"\x1a\x45\xdf\xa3" + b"\0" * 64  # nur Magic-Bytes – reicht, wo nicht dekodiert wird


@pytest.fixture
def make_app(factory):
    """App bauen und am Ende den Sprach-Thread beenden (ASGITransport startet keinen Lifespan)."""
    apps = []

    def make(overrides=None):
        app, token = factory(overrides)
        apps.append(app)
        return app, token

    yield make
    for app in apps:
        app.state.voice.close()


@pytest.fixture
def windows(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(tts, "is_windows", lambda: True)
    monkeypatch.setattr(tts, "create_backend", lambda settings: backend)
    return backend


# --- Zugriffsschutz -----------------------------------------------------------------------
@pytest.mark.parametrize("method,path", VOICE_ROUTES)
async def test_voice_routes_need_token(make_app, method, path):
    app, _ = make_app()
    async with client_for(app) as c:
        r = await c.request(method, path, json={"text": "hallo"})
    assert r.status_code == 401


@pytest.mark.parametrize("method,path", VOICE_ROUTES)
async def test_voice_routes_blocked_from_public_ip(make_app, method, path):
    app, token = make_app()
    async with client_for(app, ip="203.0.113.9", token=token) as c:
        r = await c.request(method, path, json={"text": "hallo"})
    assert r.status_code == 403


async def test_stt_upload_not_read_before_auth(make_app):
    app, _ = make_app(STT_ON)
    pulled = []

    async def body():  # gültiges multipart – ein Parser vor der Token-Prüfung würde alles lesen
        yield b'--abc\r\nContent-Disposition: form-data; name="audio"; filename="a.webm"\r\n\r\n'
        for i in range(50):
            pulled.append(i)
            yield b"x" * 65536
        yield b"\r\n--abc--\r\n"

    async with client_for(app) as c:
        r = await c.post(
            "/api/voice/stt", content=body(), headers={"content-type": "multipart/form-data; boundary=abc"}
        )
    assert r.status_code == 401
    assert pulled == []


# --- Status ------------------------------------------------------------------------------
async def test_status_on_non_windows(make_app):
    app, token = make_app()
    async with client_for(app, token=token) as c:
        r = await c.get("/api/voice/status")
    assert r.status_code == 200
    body = r.json()
    assert body["tts"]["available"] is False
    assert body["tts"]["reason"] == "Sprachausgabe ist nur auf dem Windows-PC verfügbar."
    assert body["tts"]["enabled"] is True and body["tts"]["speak_replies"] is True
    assert body["tts"]["voice"] is None
    assert body["stt"] == {
        "available": False,
        "enabled": False,
        "model": "small",
        "loaded": False,
        "reason": "Spracheingabe ist ausgeschaltet (voice.stt.enabled in config.yaml).",
        "max_seconds": 30,
    }


async def test_status_with_voice_and_stt(make_app, windows, monkeypatch, tmp_path):
    install_fake_whisper(monkeypatch, tmp_path)
    app, token = make_app(STT_ON)
    async with client_for(app, token=token) as c:
        body = (await c.get("/api/voice/status")).json()
    assert body["tts"]["available"] is True
    assert body["tts"]["voice"] == "Microsoft Hedda Desktop - German"
    assert body["tts"]["reason"] is None
    assert body["stt"]["available"] is True and body["stt"]["loaded"] is False


async def test_status_reports_missing_model(make_app, monkeypatch, tmp_path):
    pytest.importorskip("faster_whisper")
    app, token = make_app(STT_ON)
    async with client_for(app, token=token) as c:
        body = (await c.get("/api/voice/status")).json()
    assert body["stt"]["available"] is False
    assert "--download" in body["stt"]["reason"]


# --- Sprechen -----------------------------------------------------------------------------
async def test_speak_on_non_windows_is_503(make_app):
    app, token = make_app()
    async with client_for(app, token=token) as c:
        r = await c.post("/api/voice/speak", json={"text": "Hallo"})
    assert r.status_code == 503
    assert r.json() == {"ok": False, "error": "Sprachausgabe ist nur auf dem Windows-PC verfügbar."}


async def test_speak_and_stop(make_app, windows):
    app, token = make_app()
    async with client_for(app, token=token) as c:
        r = await c.post("/api/voice/speak", json={"text": "Guten Abend"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "queued": True}
        assert next_event(windows, "speak") == ("speak", "Guten Abend")
        r = await c.post("/api/voice/stop")
        assert r.json() == {"ok": True}
        next_event(windows, "stop")


async def test_stop_without_tts_is_ok(make_app):
    app, token = make_app()
    async with client_for(app, token=token) as c:
        r = await c.post("/api/voice/stop")
    assert r.status_code == 200 and r.json() == {"ok": True}


@pytest.mark.parametrize("payload", [{"text": ""}, {"text": "x" * 1001}, {}, {"text": "hi", "flags": 4}])
async def test_speak_validation(make_app, windows, payload):
    app, token = make_app()
    async with client_for(app, token=token) as c:
        r = await c.post("/api/voice/speak", json=payload)
    assert r.status_code == 422


async def test_speak_whitespace_only_is_400(make_app, windows):
    app, token = make_app()
    async with client_for(app, token=token) as c:
        r = await c.post("/api/voice/speak", json={"text": " \n\t "})
    assert r.status_code == 400
    assert r.json() == {"ok": False, "error": "Kein Text zum Sprechen."}


async def test_speak_disabled_is_503(make_app, windows):
    app, token = make_app({"voice": {"tts": {"enabled": False}}})
    async with client_for(app, token=token) as c:
        r = await c.post("/api/voice/speak", json={"text": "Hallo"})
    assert r.status_code == 503
    assert "ausgeschaltet" in r.json()["error"]


# --- Spracheingabe ------------------------------------------------------------------------
def audio_files(data: bytes, mime: str = "audio/webm;codecs=opus", name: str = "aufnahme.webm"):
    return {"audio": (name, data, mime)}


async def test_stt_disabled_is_503(make_app):
    app, token = make_app()
    async with client_for(app, token=token) as c:
        r = await c.post("/api/voice/stt", files=audio_files(WEBM_STUB))
    assert r.status_code == 503
    assert r.json() == {"ok": False, "error": "Spracheingabe ist ausgeschaltet (voice.stt.enabled in config.yaml)."}


async def test_stt_not_installed_is_503(make_app, monkeypatch):
    monkeypatch.setattr(stt, "whisper_installed", lambda: False)
    app, token = make_app(STT_ON)
    async with client_for(app, token=token) as c:
        r = await c.post("/api/voice/stt", files=audio_files(WEBM_STUB))
    assert r.status_code == 503
    assert "faster-whisper" in r.json()["error"]


async def test_stt_model_missing_is_503(make_app):
    pytest.importorskip("faster_whisper")
    app, token = make_app(STT_ON)
    async with client_for(app, token=token) as c:
        r = await c.post("/api/voice/stt", files=audio_files(WEBM_STUB))
    assert r.status_code == 503
    assert "noch nicht heruntergeladen" in r.json()["error"]


@needs_audio
@pytest.mark.parametrize(
    "container,codec,mime",
    [("webm", "libopus", "audio/webm;codecs=opus"), ("ogg", "libopus", "audio/ogg"),
     ("wav", "pcm_s16le", "audio/wav"), ("mp4", "aac", "audio/mp4")],
)
async def test_stt_transcribes_real_upload(make_app, monkeypatch, tmp_path, container, codec, mime):
    state = install_fake_whisper(monkeypatch, tmp_path)
    app, token = make_app(STT_ON)
    data = make_audio(container, codec, seconds=1.5, rate=48000 if "opus" in codec else 44100)
    async with client_for(app, token=token) as c:
        r = await c.post("/api/voice/stt", files=audio_files(data, mime, f"a.{container}"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "Hallo Welt."
    assert body["language"] == "de"
    assert body["duration"] == pytest.approx(1.5, abs=0.1)
    assert set(body) == {"text", "language", "duration"}
    assert state["model"].calls[0]["language"] == "de"


@pytest.mark.parametrize(
    "files,message",
    [
        ({"audio": ("a.txt", b"hallo", "text/plain")}, "nicht unterstützt"),
        ({"audio": ("a.wav", b"OggS" + b"\0" * 64, "audio/wav")}, "passt nicht"),
        ({"datei": ("a.webm", b"\x1a\x45\xdf\xa3", "audio/webm")}, "Feld 'audio'"),
        pytest.param({"audio": ("a.ogg", b"OggS" + b"\xff" * 300, "audio/ogg")}, "nicht gelesen", marks=needs_audio),
    ],
)
async def test_stt_bad_uploads_are_400(make_app, monkeypatch, tmp_path, files, message):
    install_fake_whisper(monkeypatch, tmp_path)
    app, token = make_app(STT_ON)
    async with client_for(app, token=token) as c:
        r = await c.post("/api/voice/stt", files=files)
    assert r.status_code == 400
    assert r.json()["ok"] is False and message in r.json()["error"]


async def test_stt_not_multipart_is_400(make_app, monkeypatch, tmp_path):
    install_fake_whisper(monkeypatch, tmp_path)
    app, token = make_app(STT_ON)
    async with client_for(app, token=token) as c:
        r = await c.post("/api/voice/stt", content=WEBM_STUB, headers={"content-type": "audio/webm"})
        assert r.status_code == 400
        assert "multipart/form-data" in r.json()["error"]
        r = await c.post("/api/voice/stt", files={"audio": ("a", b"1", "audio/wav"), "b": ("b", b"2", "audio/wav")})
        assert r.status_code == 400


async def test_stt_over_10mb_is_413(make_app, monkeypatch, tmp_path):
    state = install_fake_whisper(monkeypatch, tmp_path)
    app, token = make_app(STT_ON)
    big = b"\x1a\x45\xdf\xa3" + b"\0" * (10 * 1024 * 1024)
    async with client_for(app, token=token) as c:
        r = await c.post("/api/voice/stt", files=audio_files(big))
    assert r.status_code == 413
    assert r.json() == {"ok": False, "error": "Die Aufnahme ist größer als 10 MB."}
    assert state["loads"] == []


async def test_stt_streamed_body_without_length_is_cut_off(make_app, monkeypatch, tmp_path):
    install_fake_whisper(monkeypatch, tmp_path)
    app, token = make_app(STT_ON)
    pulled = []

    async def body():
        yield b'--abc\r\nContent-Disposition: form-data; name="audio"; filename="a.webm"\r\n'
        yield b"Content-Type: audio/webm\r\n\r\n"
        for i in range(400):  # bis zu 25 MB, aber nach gut 10 MB muss Schluss sein
            pulled.append(i)
            yield b"\0" * 65536

    async with client_for(app, token=token) as c:
        r = await c.post(
            "/api/voice/stt", content=body(), headers={"content-type": "multipart/form-data; boundary=abc"}
        )
    assert r.status_code == 413
    assert len(pulled) < 170


@needs_audio
async def test_stt_too_long_recording_is_413(make_app, monkeypatch, tmp_path):
    install_fake_whisper(monkeypatch, tmp_path)
    app, token = make_app(STT_ON)  # max_seconds 5
    async with client_for(app, token=token) as c:
        r = await c.post("/api/voice/stt", files=audio_files(make_audio(seconds=7)))
    assert r.status_code == 413
    assert "höchstens 5 s" in r.json()["error"]


@needs_audio
async def test_stt_closes_spooled_upload_files(make_app, monkeypatch, tmp_path):
    """Uploads > 1 MB landen bei Starlette in einer Temp-Datei – die muss danach zu (= gelöscht) sein."""
    install_fake_whisper(monkeypatch, tmp_path)
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool_dir))
    created = []

    class Tracking(tempfile.SpooledTemporaryFile):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", Tracking)
    app, token = make_app(STT_ON)
    data = make_audio("wav", "pcm_s16le", seconds=4.9, rate=192000)  # > 1 MB → Datei auf Platte
    assert len(data) > formparsers.MultiPartParser.spool_max_size
    async with client_for(app, token=token) as c:
        r = await c.post("/api/voice/stt", files=audio_files(data, "audio/wav", "a.wav"))
        assert r.status_code == 200, r.text
        r = await c.post("/api/voice/stt", files=audio_files(b"OggS" + b"\0" * 2_000_000, "audio/wav"))
        assert r.status_code == 400
    assert len(created) == 2
    assert all(f.closed for f in created)
    assert list(spool_dir.iterdir()) == []


# --- /api/chat mit "speak" ----------------------------------------------------------------
async def chat(app, token, **payload):
    async with client_for(app, token=token) as c:
        r = await c.post("/api/chat", json={"message": "hallo jarvis", **payload})
    assert r.status_code == 200, r.text
    return r.json()


async def test_chat_speaks_reply_by_default(make_app, windows):
    app, token = make_app()
    body = await chat(app, token)
    assert body["spoken"] is True
    assert next_event(windows, "speak") == ("speak", body["reply"])


async def test_chat_speak_false_stays_silent(make_app, windows):
    app, token = make_app()
    body = await chat(app, token, speak=False)
    assert body["spoken"] is False
    time.sleep(0.1)
    assert [c for c in windows.calls if c[0] == "speak"] == []


async def test_chat_default_follows_config(make_app, windows):
    app, token = make_app({"voice": {"tts": {"speak_replies": False}}})
    assert (await chat(app, token))["spoken"] is False
    assert (await chat(app, token, speak=True))["spoken"] is True
    next_event(windows, "speak")


async def test_chat_on_non_windows_reports_not_spoken(make_app):
    app, token = make_app()
    body = await chat(app, token, speak=True)
    assert body["spoken"] is False
    assert body["source"] == "fallback" and body["reply"]


async def test_chat_never_waits_for_speech(make_app, windows):
    windows.release.clear()  # Backend hängt in speak()
    app, token = make_app()
    started = time.monotonic()
    body = await asyncio.wait_for(chat(app, token, speak=True), timeout=5)
    assert time.monotonic() - started < 2
    assert body["spoken"] is True
    assert windows.entered.wait(5)
    windows.release.set()
    next_event(windows, "speak")


async def test_chat_rejects_non_bool_speak(make_app):
    app, token = make_app()
    async with client_for(app, token=token) as c:
        r = await c.post("/api/chat", json={"message": "hallo", "speak": "vielleicht"})
    assert r.status_code == 422


async def test_lifespan_closes_tts_worker(make_app, windows):
    app, token = make_app()
    await chat(app, token, speak=True)
    next_event(windows, "speak")
    thread = app.state.voice.tts._thread
    async with app.router.lifespan_context(app):
        pass
    assert not thread.is_alive()
    assert windows.calls[-1] == ("close",)
