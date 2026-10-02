"""Findet das kleinste lokal installierte Ollama-Modell mit Tool-Support.

Nutzt nur lesende Ollama-Endpunkte: GET /api/tags (installierte Modelle mit Größe) und
POST /api/show (Feld "capabilities", muss "tools" enthalten). Ändert nichts, zieht keine Modelle.
Die Abfrage selbst steckt in app/ollama_models.py (auch vom Einrichtungsassistenten genutzt).

Aufruf im Ordner jarvis:  .venv\\Scripts\\python.exe scripts\\pick_model.py [--url http://127.0.0.1:11434]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ollama_models import OllamaUnavailable, list_models, tool_models  # noqa: E402


def find_tool_models(client: httpx.Client, base: str) -> list[tuple[int, str]]:
    """Gibt alle Modelle aus und liefert (Größe, Name) der Tool-Modelle, kleinstes zuerst."""
    models = list_models(client, base.rstrip("/"))
    for m in models:
        mark = "tools" if m.supports_tools else "-"
        print(f"  {m.name:40s} {m.size_gb:6.1f} GB  capabilities={list(m.capabilities)}  [{mark}]")
    return [(m.size, m.name) for m in tool_models(models)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:11434")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    try:
        with httpx.Client(timeout=10, trust_env=False) as client:
            print(f"Installierte Modelle bei {base}:")
            found = find_tool_models(client, base)
    except OllamaUnavailable as exc:
        print(f"{exc}. Läuft Ollama?")
        return 1
    if not found:
        print("\nKein installiertes Modell unterstützt Tools. Ein kleines Tool-Modell ziehen,")
        print("Kandidaten siehe https://ollama.com/search?c=tools , dann dieses Skript erneut starten.")
        return 1
    size, name = found[0]
    print(f"\nEmpfehlung (kleinstes mit Tools): {name}  ({size / 1e9:.1f} GB)")
    print(f'In config.yaml eintragen:  llm:\n    model: "{name}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
