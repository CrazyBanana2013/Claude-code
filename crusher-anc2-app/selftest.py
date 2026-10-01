"""Selbsttest der Auswertung OHNE Mikrofon und OHNE Kopfhörer.

Erzeugt synthetische Signale mit bekannten Eigenschaften und prüft, ob
measure.py/compare.py die erwarteten Werte liefern. Die Ergebnisse sind
KEINE echten Messungen und landen als data/selftest_*.json (per .gitignore
ausgeschlossen).

Prüfungen:
  1. Sinus 1 kHz, Amplitude 0,5 -> 1000-Hz-Band muss -9,0 dBFS zeigen
     (Leistung eines Sinus = A²/2 = 0,125 -> 10*log10(0,125) = -9,03 dB).
  2. Rauschen vs. dasselbe Rauschen um genau 10 dB leiser -> compare.py
     muss in jedem Band +10,0 dB Dämpfung zeigen.

Aufruf:
    python selftest.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from measure import SAMPLE_RATE, octave_levels

BASE = Path(__file__).parent


def check(name: str, ok: bool, detail: str) -> bool:
    print(f"[{'OK ' if ok else 'FEHLER'}] {name}: {detail}")
    return ok


def main() -> None:
    results = []
    t = np.arange(int(10 * SAMPLE_RATE)) / SAMPLE_RATE

    # 1) Sinus mit bekannter Leistung
    sine = 0.5 * np.sin(2 * np.pi * 1000 * t)
    level = octave_levels(sine, SAMPLE_RATE)["1000"]
    results.append(check("Sinus 1 kHz", abs(level - (-9.03)) < 0.2, f"{level:.2f} dBFS (erwartet -9,03)"))

    # 2) Rauschen und dasselbe Rauschen 10 dB leiser, über WAV + echte Skripte
    rng = np.random.default_rng(0)
    noise = 0.1 * rng.standard_normal(len(t))
    quieter = noise * 10 ** (-10 / 20)
    with tempfile.TemporaryDirectory() as tmp:
        for label, sig in (("selftest_laut", noise), ("selftest_leise", quieter)):
            wav = Path(tmp) / f"{label}.wav"
            wavfile.write(wav, SAMPLE_RATE, sig.astype(np.float32))
            subprocess.run([sys.executable, str(BASE / "measure.py"), "--label", label, "--wav", str(wav)],
                           check=True, capture_output=True)
    out = subprocess.run(
        [sys.executable, str(BASE / "compare.py"), "selftest_laut", "selftest_leise",
         "--out", str(BASE / "docs" / "selftest-vergleich.png")],
        check=True, capture_output=True, text=True,
    ).stdout
    print(out)
    rows = [line for line in out.splitlines() if "|" in line and line.split("|")[0].strip().isdigit()]
    values = [float(r.split("|")[1]) for r in rows]
    results.append(check("Dämpfung 10 dB", len(values) == 8 and all(abs(v - 10.0) < 0.15 for v in values),
                         f"{values}"))

    print("\nAlle Tests bestanden." if all(results) else "\nMindestens ein Test FEHLGESCHLAGEN.")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
