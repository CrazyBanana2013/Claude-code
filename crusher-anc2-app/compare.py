"""Schritt 7: Messungen vergleichen und Dämpfung pro Oktavband ausgeben.

Die ERSTE angegebene Messung ist die Referenz (typisch: ANC aus). Für jede
weitere Messung wird die Dämpfung berechnet:

    Dämpfung [dB] = Pegel(Referenz) - Pegel(Messung)

Positiv = leiser als die Referenz (ANC hilft), negativ = lauter.

Aufruf:
    python compare.py anc_aus anc_an
    python compare.py anc_aus anc_an transparenz   # mehrere Einstellungen

Die Namen sind Labels aus measure.py (data/<label>.json). Das Diagramm
landet in docs/vergleich.png.

Näherung, kein Laborwert – siehe Hinweis in measure.py.
"""

import argparse
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # ohne Fenster rendern, funktioniert auch ohne Display
import matplotlib.pyplot as plt

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"


def load(label: str) -> dict:
    path = DATA_DIR / f"{label}.json"
    if not path.exists():
        sys.exit(f"Messung nicht gefunden: {path}  (erst: python measure.py --label {label})")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Dämpfung pro Oktavband vergleichen")
    parser.add_argument("labels", nargs="+", help="Referenz zuerst, dann eine oder mehr Messungen")
    parser.add_argument("--out", type=Path, default=BASE / "docs" / "vergleich.png", help="Pfad für das Diagramm")
    args = parser.parse_args()
    if len(args.labels) < 2:
        parser.error("Mindestens zwei Messungen angeben, z. B.: python compare.py anc_aus anc_an")

    ref_label, others = args.labels[0], args.labels[1:]
    ref = load(ref_label)
    bands = list(ref["oktavbaender_db"].keys())

    # Dämpfung je Messung und Band berechnen
    attenuation: dict[str, list[float]] = {}
    for label in others:
        m = load(label)
        attenuation[label] = [round(ref["oktavbaender_db"][b] - m["oktavbaender_db"][b], 1) for b in bands]
        for w in m.get("warnungen", []):
            print(f"WARNUNG in {label}: {w}")

    # Tabelle ausgeben
    print(f"Dämpfung in dB relativ zu '{ref_label}' (positiv = leiser):\n")
    header = f"{'Band (Hz)':>10} | " + " | ".join(f"{l:>12}" for l in others)
    print(header)
    print("-" * len(header))
    for i, b in enumerate(bands):
        print(f"{b:>10} | " + " | ".join(f"{attenuation[l][i]:>12.1f}" for l in others))

    # Wo bringt es am meisten / am wenigsten?
    print()
    for label in others:
        values = attenuation[label]
        best = max(range(len(bands)), key=lambda i: values[i])
        worst = min(range(len(bands)), key=lambda i: values[i])
        avg = sum(values) / len(values)
        print(f"{label}: am meisten im {bands[best]}-Hz-Band ({values[best]:+.1f} dB), "
              f"am wenigsten im {bands[worst]}-Hz-Band ({values[worst]:+.1f} dB), "
              f"Mittel {avg:+.1f} dB")

    print("\nHinweis: Näherung, kein Laborwert (Mikrofon vor der Ohrmuschel, unkalibriert).")
    print("Unterschiede unter ca. 2-3 dB liegen im Bereich normaler Messschwankung.")

    # Diagramm: gruppierte Balken je Band
    fig, ax = plt.subplots(figsize=(9, 5))
    width = 0.8 / len(others)
    x = range(len(bands))
    for n, label in enumerate(others):
        offset = (n - (len(others) - 1) / 2) * width
        ax.bar([i + offset for i in x], attenuation[label], width=width, label=label)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_xticks(list(x), bands)
    ax.set_xlabel("Oktavband-Mittenfrequenz (Hz)")
    ax.set_ylabel(f"Dämpfung ggü. '{ref_label}' (dB)")
    ax.set_title("ANC-Dämpfung pro Oktavband (Näherung, kein Laborwert)")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    args.out.parent.mkdir(exist_ok=True)
    fig.savefig(args.out, dpi=120)
    print(f"Diagramm gespeichert: {args.out}")


if __name__ == "__main__":
    main()
