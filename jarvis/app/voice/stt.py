"""Spracheingabe: lokale Spracherkennung mit faster-whisper (optionales Extra "voice").

- Das Modell wird beim ersten Gebrauch einmal geladen (Lock), nur aus lokalen Dateien
  (`local_files_only=True`): Der Server lädt nie selbst etwas aus dem Internet. Den einmaligen
  Download macht der Installer nach Zustimmung bzw. von Hand:
      python -m app.voice.stt --download --config config.yaml
  Ablage wie bei `WhisperModel(..., download_root=...)` (Hugging-Face-Cache-Layout) unter
  voice.stt.download_root, Vorgabe <state>/whisper.
- Audio wird im Speicher dekodiert (PyAV, FFmpeg-Demuxer fest nach MIME-Typ, Magic-Bytes geprüft)
  und nach voice.stt.max_seconds abgebrochen – keine eigenen temporären Dateien. Die Upload-Puffer
  von Starlette (SpooledTemporaryFile) werden nach jeder Anfrage geschlossen und damit gelöscht.
"""

from __future__ import annotations

import argparse
import gc
import importlib.util
import io
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator

from app.config import PROJECT_DIR

log = logging.getLogger("jarvis.voice")

SAMPLE_RATE = 16000
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MULTIPART_OVERHEAD = 64 * 1024
DURATION_GRACE = 0.5  # Sekunden Toleranz über max_seconds (Encoder-Polster, Stopp-Verzögerung)
LOCK_TIMEOUT = 120
PRESENCE_TTL = 10.0
WHISPER_FILES = ("model.bin", "config.json", "tokenizer.json")
# Wie faster_whisper.utils.download_model – nur für die Größenangabe vor dem Download.
DOWNLOAD_PATTERNS = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]
APPROX_SIZES = {
    "tiny": "ca. 75 MB",
    "base": "ca. 145 MB",
    "small": "ca. 500 MB",
    "medium": "ca. 1,5 GB",
    "large-v3": "ca. 3 GB",
    "turbo": "ca. 1,6 GB",
}

# MIME-Typ (ohne Parameter wie ;codecs=opus) → FFmpeg-Demuxer
CONTAINERS = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/vnd.wave": "wav",
    "audio/mp4": "mp4",
}
_MAGIC = {
    "webm": lambda d: d[:4] == b"\x1a\x45\xdf\xa3",
    "ogg": lambda d: d[:4] == b"OggS",
    "wav": lambda d: d[:4] == b"RIFF" and d[8:12] == b"WAVE",
    "mp4": lambda d: d[4:8] == b"ftyp",
}


# --- Fehler (Text ist für den User, status_code für die HTTP-Antwort) -----------------------
class STTError(Exception):
    status_code = 400


class AudioInvalid(STTError):
    status_code = 400


class AudioTooLarge(STTError):
    status_code = 413


class STTUnavailable(STTError):
    status_code = 503


class ModelMissing(STTUnavailable):
    pass


def download_hint() -> str:
    return (
        "Einmalig im JARVIS-Ordner ausführen: "
        ".venv\\Scripts\\python.exe -m app.voice.stt --download --config config.yaml "
        "(oder Install.cmd erneut starten und die Spracheingabe aktivieren)."
    )


# --- Wrapper um faster-whisper (in Tests ersetzt) ----------------------------------------
def whisper_installed() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def resolve_download_root(settings: Any, state_dir: Path) -> Path:
    raw = str(getattr(settings, "download_root", "") or "").strip()
    if not raw:
        return Path(state_dir) / "whisper"
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path


def _local_model_dir(name: str) -> Path | None:
    path = Path(name).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path if path.is_dir() else None


def locate_model(settings: Any, root: Path) -> Path:
    """Pfad zu den Modelldateien – ohne Netzwerk. Wirft ModelMissing/STTUnavailable."""
    name = str(getattr(settings, "model", "") or "").strip()
    if not name:
        raise STTUnavailable("voice.stt.model ist leer – z. B. small eintragen.")
    path = _local_model_dir(name)
    if path is None:
        from faster_whisper.utils import download_model

        try:
            path = Path(download_model(name, local_files_only=True, cache_dir=str(root)))
        except ValueError:
            raise STTUnavailable(
                f"Unbekanntes Whisper-Modell '{name}' (voice.stt.model). Möglich sind z. B. tiny, base, "
                "small, medium, large-v3, turbo oder ein Ordner mit einem konvertierten Modell."
            ) from None
        except Exception:
            raise ModelMissing(
                f"Whisper-Modell '{name}' ist noch nicht heruntergeladen (Ordner {root}). {download_hint()}"
            ) from None
    missing = [f for f in WHISPER_FILES if not (path / f).is_file()]
    if missing:
        raise ModelMissing(
            f"Whisper-Modell '{name}' ist unvollständig (fehlt: {', '.join(missing)}). {download_hint()}"
        )
    return path


def load_model(settings: Any, path: Path, root: Path) -> Any:
    from faster_whisper import WhisperModel

    return WhisperModel(
        str(path),
        device=str(getattr(settings, "device", "cpu") or "cpu"),
        compute_type=str(getattr(settings, "compute_type", "int8") or "int8"),
        download_root=str(root),
        local_files_only=True,
    )


# --- Audio ----------------------------------------------------------------------------
def container_for(content_type: str, data: bytes) -> str:
    base = (content_type or "").split(";", 1)[0].strip().lower()
    container = CONTAINERS.get(base)
    if container is None:
        raise AudioInvalid(
            f"Audioformat '{base or 'unbekannt'}' wird nicht unterstützt (erlaubt: webm, ogg, wav, mp4)."
        )
    if not _MAGIC[container](data):
        raise AudioInvalid("Der Dateiinhalt passt nicht zum angegebenen Audioformat.")
    return container


def decode_audio(data: bytes, container: str, max_seconds: float) -> Any:
    """Mono-float32 mit 16 kHz (wie faster_whisper.decode_audio), Abbruch nach max_seconds."""
    import av
    import numpy as np

    limit = int((float(max_seconds) + DURATION_GRACE) * SAMPLE_RATE)
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    chunks: list[Any] = []
    total = 0

    def take(frames) -> None:
        nonlocal total
        for out in frames:
            array = out.to_ndarray().reshape(-1)
            total += array.size
            if total > limit:
                raise AudioTooLarge(f"Die Aufnahme ist zu lang (höchstens {float(max_seconds):g} s).")
            chunks.append(array)

    try:
        with av.open(io.BytesIO(data), mode="r", format=container, metadata_errors="ignore") as source:
            if not source.streams.audio:
                raise AudioInvalid("Die Datei enthält keine Tonspur.")
            frames = source.decode(audio=0)
            while True:
                try:
                    frame = next(frames)
                except StopIteration:
                    break
                except av.error.InvalidDataError:
                    break  # abgeschnittene Aufnahme: Bisheriges verwenden
                frame.pts = None
                take(resampler.resample(frame))
            take(resampler.resample(None))
    except STTError:
        raise
    except Exception as exc:
        log.info("Audio nicht lesbar (%s): %s", container, exc)
        raise AudioInvalid("Die Aufnahme konnte nicht gelesen werden (beschädigt oder falsches Format).") from None
    finally:
        del resampler
        gc.collect()  # siehe faster_whisper/audio.py: Resampler-Objekte sonst nicht freigegeben
    if total == 0:
        raise AudioInvalid("Die Aufnahme enthält keinen Ton.")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


# --- Engine ---------------------------------------------------------------------------
class STTEngine:
    def __init__(self, settings: Any, download_root: Path) -> None:
        self.settings = settings
        self.root = Path(download_root)
        self._model: Any = None
        self._lock = threading.Lock()
        # (Zeitpunkt, Pfad oder None, Fehlerklasse, Fehlertext) der letzten Modellprüfung
        self._presence: tuple[float, Path | None, type[STTUnavailable], str] | None = None
        self._presence_lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return str(getattr(self.settings, "model", "") or "")

    def basic_reason(self) -> str | None:
        """Gründe, die ohne Modellprüfung feststehen (billig, auch im Event-Loop)."""
        if self.settings is None:
            return "Spracheingabe ist nicht konfiguriert (Abschnitt voice fehlt in config.yaml)."
        if not self.settings.enabled:
            return "Spracheingabe ist ausgeschaltet (voice.stt.enabled in config.yaml)."
        if not whisper_installed():
            return (
                "Spracheingabe ist nicht installiert (Paket faster-whisper fehlt). Install.cmd erneut starten "
                "und die Spracheingabe aktivieren – oder im JARVIS-Ordner: uv sync --frozen --no-dev --extra voice"
            )
        return None

    def check_basic(self) -> None:
        reason = self.basic_reason()
        if reason:
            raise STTUnavailable(reason)

    def _model_path(self) -> Path:
        with self._presence_lock:
            now = time.monotonic()
            if self._presence is None or now - self._presence[0] > PRESENCE_TTL:
                try:
                    self._presence = (now, locate_model(self.settings, self.root), STTUnavailable, "")
                except STTUnavailable as exc:
                    self._presence = (now, None, type(exc), str(exc))
            _, path, error_class, message = self._presence
        if path is None:
            raise error_class(message)
        return path

    def status(self) -> dict:
        """Blockiert evtl. kurz (Import von faster-whisper, Dateiprüfung) – im Thread aufrufen."""
        reason = self.basic_reason()
        if reason is None and self._model is None:
            try:
                self._model_path()
            except STTUnavailable as exc:
                reason = str(exc)
        return {
            "available": reason is None,
            "enabled": bool(getattr(self.settings, "enabled", False)),
            "model": self.model_name,
            "loaded": self._model is not None,
            "reason": reason,
            # zusätzlich: längste Aufnahme, damit die Oberfläche rechtzeitig stoppt
            "max_seconds": int(getattr(self.settings, "max_seconds", 0) or 0),
        }

    def _get_model(self) -> Any:
        if self._model is None:
            path = self._model_path()
            try:
                self._model = load_model(self.settings, path, self.root)
            except Exception as exc:
                self._presence = None
                log.warning("Whisper-Modell laden fehlgeschlagen: %s", exc)
                raise STTUnavailable(f"Whisper-Modell konnte nicht geladen werden: {exc}") from None
        return self._model

    def transcribe(self, data: bytes, content_type: str) -> dict:
        """Blockierend (Dekodieren + Erkennen) – im Thread aufrufen."""
        self.check_basic()
        if len(data) > MAX_UPLOAD_BYTES:
            raise AudioTooLarge("Die Aufnahme ist größer als 10 MB.")
        container = container_for(content_type, data)
        if self._model is None:
            self._model_path()  # fehlendes Modell sofort melden, ohne erst zu dekodieren
        audio = decode_audio(data, container, self.settings.max_seconds)
        duration = audio.size / SAMPLE_RATE
        language = str(getattr(self.settings, "language", "") or "").strip()
        language = None if language.lower() in ("", "auto") else language
        if not self._lock.acquire(timeout=LOCK_TIMEOUT):
            raise STTUnavailable("Die Spracherkennung ist gerade beschäftigt – bitte gleich noch einmal.")
        try:
            model = self._get_model()
            try:
                segments, info = model.transcribe(
                    audio,
                    language=language,
                    beam_size=5,
                    vad_filter=True,  # Stille entfernen – sonst „hört“ Whisper Untertitel-Floskeln
                    condition_on_previous_text=False,
                )
                text = " ".join("".join(segment.text for segment in segments).split())
            except Exception as exc:
                log.warning("Spracherkennung fehlgeschlagen: %s", exc)
                raise STTUnavailable(f"Spracherkennung fehlgeschlagen: {exc}") from None
        finally:
            self._lock.release()
        return {
            "text": text,
            "language": str(getattr(info, "language", "") or language or ""),
            "duration": round(duration, 2),
        }


# --- Upload lesen (Starlette) ---------------------------------------------------------
async def read_upload(request: Any, field: str = "audio", limit: int = MAX_UPLOAD_BYTES) -> tuple[bytes, str]:
    """Liest genau eine Datei aus multipart/form-data, bricht bei mehr als `limit` Bytes ab."""
    from starlette.datastructures import UploadFile
    from starlette.formparsers import MultiPartException, MultiPartParser

    def too_large() -> AudioTooLarge:
        return AudioTooLarge(f"Die Aufnahme ist größer als {limit // (1024 * 1024)} MB.")

    body_limit = limit + MULTIPART_OVERHEAD
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise AudioInvalid(f"Erwartet multipart/form-data mit dem Feld '{field}'.")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError:
            raise AudioInvalid("Ungültige Content-Length.") from None
        if declared > body_limit:
            raise too_large()

    async def limited() -> AsyncIterator[bytes]:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > body_limit:
                raise too_large()
            yield chunk

    parser = MultiPartParser(request.headers, limited(), max_files=1, max_fields=5)
    try:
        form = await parser.parse()
    except MultiPartException as exc:
        raise AudioInvalid(f"Ungültige Formulardaten: {exc.message}") from None
    try:
        upload = form.get(field)
        if not isinstance(upload, UploadFile):
            raise AudioInvalid(f"Das Feld '{field}' mit der Aufnahme fehlt.")
        data = await upload.read(limit + 1)
        if len(data) > limit:
            raise too_large()
        return data, upload.content_type or ""
    finally:
        await form.close()


# --- Kommandozeile: Modell herunterladen / prüfen --------------------------------------
def default_state_dir() -> Path:
    return Path(os.environ.get("JARVIS_STATE", PROJECT_DIR / "state"))


def _repo_id(name: str) -> str | None:
    if "/" in name:
        return name
    try:
        from faster_whisper import utils

        return getattr(utils, "_MODELS", {}).get(name)
    except Exception:
        return None


def _download_size(repo_id: str, root: Path) -> int | None:
    """Noch zu ladende Bytes laut Hugging Face (dry_run, lädt nichts) – None, wenn unbekannt."""
    try:
        from huggingface_hub import snapshot_download

        files = snapshot_download(repo_id, cache_dir=str(root), allow_patterns=DOWNLOAD_PATTERNS, dry_run=True)
        return sum(f.file_size or 0 for f in files if f.will_download)
    except Exception:
        return None


def _format_size(size: int) -> str:
    if size >= 1024**3:
        return f"{size / 1024**3:.1f} GB".replace(".", ",")
    return f"{max(1, round(size / 1024**2))} MB"


def download(name: str, root: Path) -> Path:
    from faster_whisper.utils import download_model

    root.mkdir(parents=True, exist_ok=True)
    return Path(download_model(name, cache_dir=str(root)))


def main(argv: list[str] | None = None) -> int:
    from app.config import ConfigError, load_config

    parser = argparse.ArgumentParser(
        prog="python -m app.voice.stt",
        description="Whisper-Modell für die Spracheingabe von JARVIS herunterladen oder prüfen.",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--download", action="store_true", help="Modell aus voice.stt.model herunterladen")
    action.add_argument("--check", action="store_true", help="nur prüfen, ob das Modell vorhanden ist (Exit 0/1)")
    parser.add_argument("--config", help="Pfad zu config.yaml (Vorgabe: JARVIS_CONFIG bzw. config.yaml)")
    parser.add_argument("--state", help="state-Ordner (Vorgabe: JARVIS_STATE bzw. state im JARVIS-Ordner)")
    args = parser.parse_args(argv)

    # Vor dem ersten Import von huggingface_hub: keine Telemetrie, kein gespeicherter HF-Token.
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Konfiguration nicht lesbar:\n{exc}", file=sys.stderr)
        return 2
    settings = getattr(getattr(config, "voice", None), "stt", None)
    if settings is None:
        print("config.yaml enthält keinen Abschnitt voice.stt.", file=sys.stderr)
        return 2
    root = resolve_download_root(settings, Path(args.state) if args.state else default_state_dir())
    name = str(settings.model).strip()
    if not whisper_installed():
        print(
            "Das Paket faster-whisper ist nicht installiert (optionales Extra 'voice').\n"
            "Im JARVIS-Ordner: uv sync --frozen --no-dev --extra voice\n"
            "oder: .venv\\Scripts\\python.exe -m pip install --require-hashes -r requirements-voice.txt",
            file=sys.stderr,
        )
        return 3
    try:
        path = locate_model(settings, root)
    except ModelMissing:
        if args.check:
            print(f"Whisper-Modell '{name}' fehlt (Ordner {root}).")
            return 1
    except STTUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    else:
        print(f"Whisper-Modell '{name}' ist vorhanden: {path}")
        return 0

    repo = _repo_id(name)
    print(f"Spracheingabe: Whisper-Modell '{name}' herunterladen")
    print(f"  Quelle: https://huggingface.co/{repo}" if repo else "  Quelle: Hugging Face")
    print(f"  Ziel:   {root}")
    size = _download_size(repo, root) if repo else None
    if size:
        print(f"  Größe:  {_format_size(size)}")
    else:
        print(f"  Größe:  {APPROX_SIZES.get(name, 'unbekannt')}")
    print("  Einmaliger Download, danach läuft die Spracherkennung offline. Das kann einige Minuten dauern …")
    if not settings.enabled:
        print("  Hinweis: voice.stt.enabled ist false – zum Benutzen in config.yaml auf true setzen.")
    sys.stdout.flush()
    try:
        path = download(name, root)
        locate_model(settings, root)
    except Exception as exc:
        print(f"Download fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    print(f"Fertig: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
