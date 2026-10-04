"""Spracheingabe: Dekodieren echter Audiodateien (PyAV), Engine mit gemocktem Whisper-Modell, CLI.

Es werden nie echte Whisper-Gewichte geladen oder heruntergeladen. numpy und PyAV kommen mit dem
optionalen Extra "voice"; ohne es werden die Audio-Tests übersprungen.
"""

from __future__ import annotations

import io
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from app.config import PROJECT_DIR, STTConfig
from app.voice import stt
from app.voice.stt import AudioInvalid, AudioTooLarge, ModelMissing, STTEngine, STTUnavailable

try:
    import av
    import numpy as np
except ImportError:  # Extra "voice" nicht installiert
    av = np = None

needs_audio = pytest.mark.skipif(av is None, reason="PyAV/numpy fehlen (Extra 'voice' nicht installiert)")


# --- Testaudio erzeugen ------------------------------------------------------------------
def make_audio(container: str = "webm", codec: str = "libopus", seconds: float = 2.0, rate: int = 48000,
               freq: float = 440.0) -> bytes:
    """Kurzer Sinuston als echte Datei (webm/ogg mit Opus, wav, mp4 mit AAC)."""
    buf = io.BytesIO()
    with av.open(buf, mode="w", format=container) as out:
        stream = out.add_stream(codec, rate=rate, layout="mono")
        total = int(seconds * rate)
        t = np.arange(total) / rate
        signal = (0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
        for start in range(0, total, 960):
            frame = av.AudioFrame.from_ndarray(signal[start:start + 960].reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = rate
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return buf.getvalue()


class _NoSeek(io.RawIOBase):
    def __init__(self) -> None:
        self.data = bytearray()

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def write(self, b) -> int:
        self.data += b
        return len(b)


def make_live_webm(seconds: float = 1.5) -> bytes:
    """WebM wie von MediaRecorder: nicht seekbar geschrieben (keine Cues, keine Dauer im Header)."""
    sink = _NoSeek()
    with av.open(sink, mode="w", format="webm", options={"live": "1"}) as out:
        stream = out.add_stream("libopus", rate=48000, layout="mono")
        signal = (0.2 * np.sin(2 * np.pi * 300 * np.arange(int(48000 * seconds)) / 48000) * 32767).astype(np.int16)
        for start in range(0, signal.size, 960):
            frame = av.AudioFrame.from_ndarray(signal[start:start + 960].reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = 48000
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return bytes(sink.data)


WEBM_TYPE = "audio/webm;codecs=opus"


# --- Fake-Whisper ------------------------------------------------------------------------
@dataclass
class Segment:
    text: str


@dataclass
class Info:
    language: str
    duration: float


class FakeModel:
    def __init__(self, segments=(" Hallo", " Welt."), delay: float = 0.0) -> None:
        self.segments = list(segments)
        self.delay = delay
        self.calls: list[dict] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def transcribe(self, audio, **kwargs):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            self.calls.append({"samples": int(audio.size), "dtype": str(audio.dtype), **kwargs})
            return (Segment(t) for t in self.segments), Info(kwargs.get("language") or "de", audio.size / 16000)
        finally:
            with self._lock:
                self.active -= 1


def install_fake_whisper(monkeypatch, tmp_path: Path, model: FakeModel | None = None) -> dict:
    """Paket „installiert“, Modell „vorhanden“, load_model liefert das Fake-Modell."""
    model = model or FakeModel()
    state = {"model": model, "loads": []}
    monkeypatch.setattr(stt, "whisper_installed", lambda: True)
    monkeypatch.setattr(stt, "locate_model", lambda settings, root: tmp_path / "modell")

    def fake_load(settings, path, root):
        state["loads"].append((settings.model, path, root))
        return model

    monkeypatch.setattr(stt, "load_model", fake_load)
    return state


def engine(tmp_path: Path, **overrides) -> STTEngine:
    settings = STTConfig(**{"enabled": True, **overrides})
    return STTEngine(settings, tmp_path / "whisper")


def fake_hf_cache(root: Path, repo: str = "Systran/faster-whisper-small",
                  files=("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")) -> Path:
    """Hugging-Face-Cache-Layout, wie es snapshot_download(cache_dir=root) anlegt."""
    commit = "0123456789abcdef0123456789abcdef01234567"
    repo_dir = root / ("models--" + repo.replace("/", "--"))
    (repo_dir / "refs").mkdir(parents=True)
    (repo_dir / "refs" / "main").write_text(commit)
    snapshot = repo_dir / "snapshots" / commit
    snapshot.mkdir(parents=True)
    for name in files:
        (snapshot / name).write_bytes(b"x")
    return snapshot


# --- Dekodieren --------------------------------------------------------------------------
@needs_audio
@pytest.mark.parametrize(
    "container,codec,mime,rate",
    [
        ("webm", "libopus", "audio/webm;codecs=opus", 48000),
        ("ogg", "libopus", "audio/ogg; codecs=opus", 48000),
        ("wav", "pcm_s16le", "audio/wav", 44100),
        ("mp4", "aac", "audio/mp4", 44100),
    ],
)
def test_decode_real_formats(container, codec, mime, rate):
    data = make_audio(container, codec, seconds=2.0, rate=rate)
    fmt = stt.container_for(mime, data)
    audio = stt.decode_audio(data, fmt, max_seconds=30)
    assert audio.dtype == np.float32
    assert audio.ndim == 1
    assert abs(audio.size / 16000 - 2.0) < 0.1
    assert 0.1 < float(np.abs(audio).max()) <= 1.0


@needs_audio
def test_decode_live_webm_like_mediarecorder():
    data = make_live_webm(1.5)
    audio = stt.decode_audio(data, stt.container_for("audio/webm", data), max_seconds=30)
    assert abs(audio.size / 16000 - 1.5) < 0.1


@needs_audio
def test_decoder_matches_faster_whisper():
    """Eigener Dekoder (mit Längenlimit) liefert dasselbe wie faster_whisper.decode_audio."""
    faster_whisper = pytest.importorskip("faster_whisper")
    data = make_audio("wav", "pcm_s16le", seconds=1.0, rate=44100)
    ours = stt.decode_audio(data, "wav", max_seconds=30)
    theirs = faster_whisper.decode_audio(io.BytesIO(data))
    assert ours.shape == theirs.shape
    assert np.allclose(ours, theirs, atol=1e-3)


@needs_audio
def test_too_long_is_413():
    data = make_audio("ogg", "libopus", seconds=3.0)
    with pytest.raises(AudioTooLarge) as exc:
        stt.decode_audio(data, "ogg", max_seconds=1)
    assert exc.value.status_code == 413
    assert "zu lang (höchstens 1 s)" in str(exc.value)
    # Knapp über dem Limit (Toleranz 0,5 s) ist noch in Ordnung
    assert stt.decode_audio(data, "ogg", max_seconds=3).size > 0


@needs_audio
def test_truncated_recording_uses_what_is_there():
    data = make_live_webm(2.0)
    audio = stt.decode_audio(data[: len(data) // 2], "webm", max_seconds=30)
    assert 0.3 < audio.size / 16000 < 2.0


@pytest.mark.parametrize(
    "mime,data,message",
    [
        ("text/plain", b"hallo", "nicht unterstützt"),
        ("", b"RIFF....WAVE", "nicht unterstützt"),
        ("video/x-msvideo", b"RIFF....AVI ", "nicht unterstützt"),
        ("audio/wav", b"OggS" + b"\0" * 40, "passt nicht"),
        ("audio/webm", b"RIFF....WAVE", "passt nicht"),
        ("audio/mp4", b"\x1a\x45\xdf\xa3" + b"\0" * 40, "passt nicht"),
    ],
)
def test_container_for_rejects_wrong_types(mime, data, message):
    with pytest.raises(AudioInvalid) as exc:
        stt.container_for(mime, data)
    assert exc.value.status_code == 400
    assert message in str(exc.value)


@needs_audio
def test_container_for_accepts_parameters_and_aliases():
    wav = make_audio("wav", "pcm_s16le", seconds=0.2, rate=16000)
    assert stt.container_for("Audio/X-WAV", wav) == "wav"
    assert stt.container_for("audio/wave", wav) == "wav"


@needs_audio
@pytest.mark.parametrize(
    "data,container",
    [
        (b"\x1a\x45\xdf\xa3" + bytes(range(256)) * 4, "webm"),
        (b"OggS" + b"\xff" * 500, "ogg"),
        (b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 20, "wav"),
    ],
)
def test_corrupt_or_empty_audio_is_400(data, container):
    with pytest.raises(AudioInvalid) as exc:
        stt.decode_audio(data, container, max_seconds=30)
    assert exc.value.status_code == 400


# --- Engine ------------------------------------------------------------------------------
@needs_audio
def test_transcribe_with_fake_model(monkeypatch, tmp_path):
    state = install_fake_whisper(monkeypatch, tmp_path)
    eng = engine(tmp_path)
    data = make_audio(seconds=2.0)
    result = eng.transcribe(data, WEBM_TYPE)
    assert result == {"text": "Hallo Welt.", "language": "de", "duration": pytest.approx(2.0, abs=0.1)}
    call = state["model"].calls[0]
    assert call["language"] == "de"
    assert call["vad_filter"] is True
    assert call["dtype"] == "float32"
    assert abs(call["samples"] - 32000) < 1600


@needs_audio
def test_model_is_loaded_once_and_lazily(monkeypatch, tmp_path):
    state = install_fake_whisper(monkeypatch, tmp_path)
    eng = engine(tmp_path, model="base", device="cpu", compute_type="int8")
    assert state["loads"] == []
    assert eng.status()["loaded"] is False
    data = make_audio(seconds=0.5)
    eng.transcribe(data, WEBM_TYPE)
    eng.transcribe(data, WEBM_TYPE)
    assert len(state["loads"]) == 1
    assert state["loads"][0][0] == "base"
    assert eng.status() == {
        "available": True, "enabled": True, "model": "base", "loaded": True, "reason": None, "max_seconds": 30,
    }
    # Modell im Speicher: keine erneute Dateiprüfung nötig
    monkeypatch.setattr(stt, "locate_model", lambda s, r: pytest.fail("schon geladen"))
    eng._presence = None
    assert eng.transcribe(data, WEBM_TYPE)["text"] == "Hallo Welt."


@needs_audio
def test_transcriptions_are_serialized(monkeypatch, tmp_path):
    state = install_fake_whisper(monkeypatch, tmp_path, FakeModel(delay=0.2))
    eng = engine(tmp_path)
    data = make_audio(seconds=0.5)
    threads = [threading.Thread(target=eng.transcribe, args=(data, WEBM_TYPE)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(state["model"].calls) == 3
    assert state["model"].max_active == 1


@needs_audio
def test_empty_language_means_autodetect(monkeypatch, tmp_path):
    state = install_fake_whisper(monkeypatch, tmp_path)
    eng = engine(tmp_path, language="")
    eng.transcribe(make_audio(seconds=0.5), WEBM_TYPE)
    assert state["model"].calls[0]["language"] is None


@needs_audio
def test_disabled_is_503(monkeypatch, tmp_path):
    install_fake_whisper(monkeypatch, tmp_path)
    eng = STTEngine(STTConfig(enabled=False), tmp_path)
    with pytest.raises(STTUnavailable) as exc:
        eng.transcribe(make_audio(seconds=0.5), WEBM_TYPE)
    assert exc.value.status_code == 503
    assert "ausgeschaltet" in str(exc.value)
    assert eng.status()["available"] is False


def test_not_installed_is_503(monkeypatch, tmp_path):
    monkeypatch.setattr(stt, "whisper_installed", lambda: False)
    eng = engine(tmp_path)
    with pytest.raises(STTUnavailable) as exc:
        eng.transcribe(b"", WEBM_TYPE)
    assert "faster-whisper" in str(exc.value) and "--extra voice" in str(exc.value)
    status = eng.status()
    assert status["available"] is False and "faster-whisper" in status["reason"]


@needs_audio
def test_model_missing_is_503_with_download_hint(tmp_path):
    pytest.importorskip("faster_whisper")
    eng = engine(tmp_path)
    with pytest.raises(ModelMissing) as exc:
        eng.transcribe(make_audio(seconds=0.5), WEBM_TYPE)
    assert exc.value.status_code == 503
    assert "--download" in str(exc.value)
    status = eng.status()
    assert status["available"] is False and "noch nicht heruntergeladen" in status["reason"]


@needs_audio
def test_load_failure_is_503(monkeypatch, tmp_path):
    install_fake_whisper(monkeypatch, tmp_path)

    def broken(settings, path, root):
        raise RuntimeError("Unable to open file 'model.bin'")

    monkeypatch.setattr(stt, "load_model", broken)
    with pytest.raises(STTUnavailable) as exc:
        engine(tmp_path).transcribe(make_audio(seconds=0.5), WEBM_TYPE)
    assert "konnte nicht geladen werden" in str(exc.value)


@needs_audio
def test_invalid_audio_does_not_load_model(monkeypatch, tmp_path):
    state = install_fake_whisper(monkeypatch, tmp_path)
    eng = engine(tmp_path)
    with pytest.raises(AudioInvalid):
        eng.transcribe(b"OggS" + b"\xff" * 100, "audio/ogg")
    with pytest.raises(AudioTooLarge):
        eng.transcribe(b"\x1a\x45\xdf\xa3" + b"\0" * stt.MAX_UPLOAD_BYTES, WEBM_TYPE)
    assert state["loads"] == []


@needs_audio
def test_too_long_recording_uses_config_limit(monkeypatch, tmp_path):
    install_fake_whisper(monkeypatch, tmp_path)
    with pytest.raises(AudioTooLarge):
        engine(tmp_path, max_seconds=1).transcribe(make_audio(seconds=2.5), WEBM_TYPE)


# --- Modelldateien finden (echtes faster_whisper, ohne Netzwerk) ---------------------------
def test_locate_model_finds_hf_cache_layout(tmp_path):
    pytest.importorskip("faster_whisper")
    snapshot = fake_hf_cache(tmp_path)
    assert stt.locate_model(STTConfig(model="small"), tmp_path) == snapshot


def test_locate_model_incomplete_snapshot(tmp_path):
    pytest.importorskip("faster_whisper")
    fake_hf_cache(tmp_path, files=("model.bin", "config.json"))
    with pytest.raises(ModelMissing) as exc:
        stt.locate_model(STTConfig(model="small"), tmp_path)
    assert "tokenizer.json" in str(exc.value)


def test_locate_model_unknown_name(tmp_path):
    pytest.importorskip("faster_whisper")
    with pytest.raises(STTUnavailable) as exc:
        stt.locate_model(STTConfig(model="gibtsnicht"), tmp_path)
    assert not isinstance(exc.value, ModelMissing)
    assert "Unbekanntes Whisper-Modell" in str(exc.value)


def test_locate_model_accepts_local_directory(tmp_path):
    folder = tmp_path / "mein-modell"
    folder.mkdir()
    for name in stt.WHISPER_FILES:
        (folder / name).write_bytes(b"x")
    assert stt.locate_model(STTConfig(model=str(folder)), tmp_path / "egal") == folder


def test_load_model_uses_documented_arguments(monkeypatch, tmp_path):
    faster_whisper = pytest.importorskip("faster_whisper")
    seen = {}

    class Recorder:
        def __init__(self, model_size_or_path, **kwargs):
            seen["path"] = model_size_or_path
            seen.update(kwargs)

    monkeypatch.setattr(faster_whisper, "WhisperModel", Recorder)
    stt.load_model(STTConfig(device="cpu", compute_type="int8"), tmp_path / "snap", tmp_path)
    assert seen == {
        "path": str(tmp_path / "snap"),
        "device": "cpu",
        "compute_type": "int8",
        "download_root": str(tmp_path),
        "local_files_only": True,
    }


def test_resolve_download_root(tmp_path):
    assert stt.resolve_download_root(STTConfig(), tmp_path / "state") == tmp_path / "state" / "whisper"
    absolute = tmp_path / "modelle"
    assert stt.resolve_download_root(STTConfig(download_root=str(absolute)), tmp_path) == absolute
    assert stt.resolve_download_root(STTConfig(download_root="modelle"), tmp_path) == PROJECT_DIR / "modelle"


# --- Kommandozeile ----------------------------------------------------------------------
def write_config(tmp_path: Path, **stt_values) -> Path:
    data = yaml.safe_load((PROJECT_DIR / "config.example.yaml").read_text(encoding="utf-8"))
    data["voice"]["stt"].update(stt_values)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_cli_check_missing_and_present(tmp_path, capsys):
    pytest.importorskip("faster_whisper")
    cfg = write_config(tmp_path, enabled=True)
    state = tmp_path / "state"
    assert stt.main(["--check", "--config", str(cfg), "--state", str(state)]) == 1
    assert "fehlt" in capsys.readouterr().out
    fake_hf_cache(state / "whisper")
    assert stt.main(["--check", "--config", str(cfg), "--state", str(state)]) == 0
    assert "vorhanden" in capsys.readouterr().out


def test_cli_download_prints_plan_then_downloads(monkeypatch, tmp_path, capsys):
    pytest.importorskip("faster_whisper")
    cfg = write_config(tmp_path, enabled=True)
    state = tmp_path / "state"
    calls = []

    def fake_download(name, root):
        calls.append((name, root))
        return fake_hf_cache(root)

    monkeypatch.setattr(stt, "download", fake_download)
    monkeypatch.setattr(stt, "_download_size", lambda repo, root: 484 * 1024 * 1024)
    assert stt.main(["--download", "--config", str(cfg), "--state", str(state)]) == 0
    out = capsys.readouterr().out
    assert calls == [("small", state / "whisper")]
    assert "https://huggingface.co/Systran/faster-whisper-small" in out
    assert str(state / "whisper") in out
    assert "484 MB" in out
    assert out.index("Größe") < out.index("Fertig")
    # Zweiter Aufruf: schon vorhanden, kein Download
    assert stt.main(["--download", "--config", str(cfg), "--state", str(state)]) == 0
    assert len(calls) == 1


def test_cli_download_failure(monkeypatch, tmp_path, capsys):
    pytest.importorskip("faster_whisper")
    cfg = write_config(tmp_path)

    def offline(name, root):
        raise OSError("Verbindung abgelehnt")

    monkeypatch.setattr(stt, "download", offline)
    monkeypatch.setattr(stt, "_download_size", lambda repo, root: None)
    assert stt.main(["--download", "--config", str(cfg), "--state", str(tmp_path / "s")]) == 1
    captured = capsys.readouterr()
    assert "ca. 500 MB" in captured.out
    assert "voice.stt.enabled ist false" in captured.out
    assert "Download fehlgeschlagen: Verbindung abgelehnt" in captured.err


def test_cli_not_installed_and_bad_config(monkeypatch, tmp_path, capsys):
    cfg = write_config(tmp_path)
    monkeypatch.setattr(stt, "whisper_installed", lambda: False)
    assert stt.main(["--download", "--config", str(cfg)]) == 3
    assert "requirements-voice.txt" in capsys.readouterr().err
    bad = tmp_path / "kaputt.yaml"
    bad.write_text("voice: [", encoding="utf-8")
    assert stt.main(["--check", "--config", str(bad)]) == 2


def test_cli_runs_as_module_without_warnings(tmp_path):
    cfg = write_config(tmp_path, enabled=True)
    proc = subprocess.run(
        [sys.executable, "-m", "app.voice.stt", "--check", "--config", str(cfg), "--state", str(tmp_path / "st")],
        cwd=PROJECT_DIR, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 1, proc.stderr
    assert "fehlt" in proc.stdout
    assert "Warning" not in proc.stderr and "Traceback" not in proc.stderr
