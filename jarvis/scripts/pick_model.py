"""Findet das kleinste lokal installierte Ollama-Modell mit Tool-Support.

Nutzt nur lesende Ollama-Endpunkte: GET /api/tags (installierte Modelle mit Größe) und
POST /api/show (Feld "capabilities", muss "tools" enthalten). Ändert nichts.

Aufruf im Ordner jarvis:  .venv\\Scripts\\python.exe scripts\\pick_model.py [--url http://127.0.0.1:11434]
"""

from __future__ import annotations

import argparse

import httpx


def find_tool_models(client: httpx.Client, base: str) -> list[tuple[int, str]]:
    tags = client.get(f"{base}/api/tags").json().get("models", [])
    found = []
    for model in tags:
        name = model.get("name")
        if not name:
            continue
        show = client.post(f"{base}/api/show", json={"model": name})
        caps = show.json().get("capabilities", []) if show.status_code == 200 else []
        mark = "tools" if "tools" in caps else "-"
        size = int(model.get("size", 0))
        print(f"  {name:40s} {size / 1e9:6.1f} GB  capabilities={caps}  [{mark}]")
        if "tools" in caps:
            found.append((size, name))
    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:11434")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    try:
        with httpx.Client(timeout=10, trust_env=False) as client:
            print(f"Installierte Modelle bei {base}:")
            found = find_tool_models(client, base)
    except httpx.HTTPError as exc:
        print(f"Ollama nicht erreichbar ({type(exc).__name__}). Läuft Ollama?")
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
