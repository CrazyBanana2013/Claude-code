"""Stimme: Sprachausgabe am PC (tts) und lokale Spracherkennung (stt).

Einstellungen kommen aus config.yaml, Abschnitt voice (tts/stt, siehe config.example.yaml).
Keine Cloud: TTS über die eingebauten Windows-Stimmen (SAPI), STT mit faster-whisper auf dem PC.

Die Untermodule werden hier bewusst nicht beim Import geladen: `python -m app.voice.stt` führt
stt.py als __main__ aus, und ein vorheriger Import über das Paket ergäbe eine RuntimeWarning.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from app.voice.stt import STTEngine
    from app.voice.tts import TTSEngine


class VoiceService:
    def __init__(self, tts: "TTSEngine", stt: "STTEngine") -> None:
        self.tts = tts
        self.stt = stt

    @classmethod
    def from_config(cls, config: Any, state_dir: Path) -> "VoiceService":
        from app.voice.stt import STTEngine, resolve_download_root
        from app.voice.tts import TTSEngine

        voice = getattr(config, "voice", None)
        tts_cfg = getattr(voice, "tts", None)
        stt_cfg = getattr(voice, "stt", None)
        return cls(TTSEngine(tts_cfg), STTEngine(stt_cfg, resolve_download_root(stt_cfg, state_dir)))

    def status(self) -> dict:
        """Blockiert evtl. kurz (Sprach-Thread starten, Modelldateien prüfen) – im Thread aufrufen."""
        self.tts.warm_up()
        return {"tts": self.tts.status(), "stt": self.stt.status()}

    def close(self) -> None:
        self.tts.close()
