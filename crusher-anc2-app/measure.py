"""Schritt 6: Umgebungsgeräusch aufnehmen und in Oktavbändern auswerten.

Ablauf:
  1. 10 Sekunden Mikrofonsignal aufnehmen (48 kHz, mono)
  2. Leistungsdichtespektrum mit der Welch-Methode berechnen
  3. Leistung in Oktavbändern 63 Hz ... 8 kHz aufsummieren
  4. Ergebnis als data/<label>.json speichern

WICHTIG – NÄHERUNG, KEIN LABORWERT:
  Das Mikrofon liegt direkt vor der Ohrmuschel bzw. im Ohrpolster, nicht in
  einem Kunstkopf/Ohrsimulator. Die Werte sind unkalibriert (dB relativ zur
  Vollaussteuerung des Mikrofons, "dBFS"). Aussagekräftig ist nur der
  VERGLEICH zweier Messungen mit gleichem Mikrofon, gleicher Position,
  gleicher Pegeleinstellung und gleichem Umgebungsgeräusch.

Aufruf:
    python measure.py --label anc_aus
    python measure.py --label anc_an --device 3     # bestimmtes Mikrofon
    python measure.py --list-devices                # Mikrofone anzeigen
    python measure.py --label test --wav datei.wav  # WAV statt Mikrofon auswerten
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import welch

SAMPLE_RATE = 48_000  # Hz
DURATION = 10.0  # Sekunden
# Oktav-Mittenfrequenzen nach IEC 61260 (Nennwerte), 63 Hz bis 8 kHz
OCTAVE_CENTERS = [63, 125, 250, 500, 1000, 2000, 4000, 8000]
DATA_DIR = Path(__file__).parent / "data"

DISCLAIMER = (
    "Naeherung, kein Laborwert: Mikrofon vor/in der Ohrmuschel, unkalibriert (dBFS). "
    "Nur Vergleiche mit gleichem Mikrofon, gleicher Position und gleichem Geraeusch sind sinnvoll."
)


def record(seconds: float, device: int | None) -> np.ndarray:
    """Nimmt mono vom Mikrofon auf und gibt float32-Samples (-1..1) zurück."""
    import sounddevice as sd  # erst hier importieren: --wav funktioniert auch ohne PortAudio

    print(f"Aufnahme läuft {seconds:.0f} s ... bitte ruhig bleiben.")
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1,
                   dtype="float32", device=device)
    sd.wait()  # blockiert, bis die Aufnahme fertig ist
    return audio[:, 0]


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    """Liest eine WAV-Datei und normiert sie auf float (-1..1), mono."""
    rate, data = wavfile.read(path)
    if np.issubdtype(data.dtype, np.integer):
        data = data.astype(np.float64) / np.iinfo(data.dtype).max
    if data.ndim > 1:
        data = data[:, 0]  # nur ersten Kanal verwenden
    return data.astype(np.float64), int(rate)


def octave_levels(signal: np.ndarray, rate: int) -> dict[str, float]:
    """Berechnet den Pegel (dBFS) je Oktavband aus dem Welch-Spektrum."""
    # nperseg=16384 -> Frequenzauflösung ~2,9 Hz bei 48 kHz, genug für das 63-Hz-Band
    freqs, psd = welch(signal, fs=rate, nperseg=min(16384, len(signal)), window="hann")
    df = freqs[1] - freqs[0]

    levels = {}
    for fc in OCTAVE_CENTERS:
        lo, hi = fc / np.sqrt(2), fc * np.sqrt(2)  # Bandgrenzen einer Oktave
        mask = (freqs >= lo) & (freqs < hi)
        power = np.sum(psd[mask]) * df  # Leistung im Band = Integral der Dichte
        # 1e-20 verhindert log(0) bei absoluter Stille
        levels[str(fc)] = round(float(10 * np.log10(power + 1e-20)), 2)
    return levels


def main() -> None:
    parser = argparse.ArgumentParser(description="Mikrofon-Messung in Oktavbändern (Näherung!)")
    parser.add_argument("--label", help="Name der Messung, z. B. anc_aus -> data/anc_aus.json")
    parser.add_argument("--seconds", type=float, default=DURATION, help="Aufnahmedauer (Standard: 10 s)")
    parser.add_argument("--device", type=int, default=None, help="Mikrofon-Index (siehe --list-devices)")
    parser.add_argument("--wav", type=Path, help="WAV-Datei auswerten statt aufzunehmen")
    parser.add_argument("--list-devices", action="store_true", help="Audiogeräte auflisten und beenden")
    args = parser.parse_args()

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return
    if not args.label:
        parser.error("--label fehlt (z. B. --label anc_aus)")

    print("HINWEIS:", DISCLAIMER)
    if args.wav:
        signal, rate = load_wav(args.wav)
        source = f"wav:{args.wav.name}"
    else:
        signal, rate = record(args.seconds, args.device), SAMPLE_RATE
        source = f"mikrofon:{args.device if args.device is not None else 'standard'}"

    # Plausibilitätsprüfungen am Rohsignal
    peak = float(np.max(np.abs(signal))) if len(signal) else 0.0
    rms_db = float(20 * np.log10(np.sqrt(np.mean(signal ** 2)) + 1e-20)) if len(signal) else -400.0
    warnings = []
    if peak >= 0.99:
        warnings.append("Uebersteuerung (Clipping) - Mikrofonpegel in Windows senken.")
    if rms_db < -80:
        warnings.append("Signal sehr leise - Mikrofon stumm/falsches Geraet? (--list-devices)")
    for w in warnings:
        print("WARNUNG:", w)

    levels = octave_levels(signal, rate)
    result = {
        "label": args.label,
        "zeitpunkt": datetime.datetime.now().isoformat(timespec="seconds"),
        "quelle": source,
        "abtastrate_hz": rate,
        "dauer_s": round(len(signal) / rate, 2),
        "einheit": "dBFS (unkalibriert)",
        "spitzenwert": round(peak, 4),
        "gesamtpegel_dbfs": round(rms_db, 2),
        "oktavbaender_db": levels,
        "warnungen": warnings,
        "hinweis": DISCLAIMER,
    }

    DATA_DIR.mkdir(exist_ok=True)
    out = DATA_DIR / f"{args.label}.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nGesamtpegel: {rms_db:6.1f} dBFS   Spitze: {peak:.3f}")
    print(f"{'Band (Hz)':>10} | Pegel (dBFS)")
    print("-" * 26)
    for fc, db in levels.items():
        print(f"{fc:>10} | {db:8.1f}")
    print(f"\nGespeichert: {out}")


if __name__ == "__main__":
    try:
        main()
    except OSError as err:  # z. B. "PortAudio library not found" oder kein Mikrofon
        sys.exit(f"Audiofehler: {err}")
