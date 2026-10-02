# Handover – JARVIS (lokaler KI-Assistent)
Letztes Update: 2026-10-02 15:15 UTC

## Ziel
Kleiner Webserver (FastAPI) auf einem Windows-PC mit JARVIS-Oberfläche (Chat + Schnellaktionen),
erreichbar im LAN und über Tailscale vom Handy. Ein kleines lokales Ollama-Modell übersetzt
Sprache in Tool-Aufrufe (PC herunterfahren, WLED-LED-Strip, Zimmertemperatur via ESPHome,
Skripte starten/stoppen). Alles funktioniert auch ohne LLM (Buttons + Regel-Parser).
Dazu `jarvis/launcher/wake.html` fürs Handy: weckt den PC über die Depicus-WoL-Seite und leitet
danach zu JARVIS weiter.

## Umgebung
- Entwicklung bisher: Linux-Cloud-Container (NICHT der Ziel-PC). Python 3.11.15, uv 0.8.17,
  Node 22 mit global installiertem Playwright 1.56.1 (Chromium unter `/opt/pw-browsers`).
- Kein Ollama, kein Windows, kein Zugriff aufs Heimnetz (192.168.178.x wird vom Proxy geblockt).
  Deshalb konnten `winver`, `ollama --version`, `ollama list` und die ESPHome-Probe NICHT
  ausgeführt werden → User-Aufgaben unter "Nächste Schritte".
- Ziel-PC: Windows, Ryzen 9800X3D, RX 9070 XT, Ollama mit ROCm auf Port 11434.

## Projektstruktur
(wird fortlaufend ergänzt)

## Architektur & Entscheidungen
(wird fortlaufend ergänzt)

## Erledigt
- [x] M0 Setup: `jarvis/` angelegt (Repo `Claude-code` war schon ein Git-Repo, daher kein
  separates `git init`), `.gitignore` (config.yaml, secrets.yaml, state/, .venv), `pyproject.toml`
  mit geprüften Versionen (per `uv pip install` ermittelt: fastapi 0.142.2, uvicorn 0.54.0,
  httpx 0.28.1, pydantic 2.13.5, pyyaml 6.0.3, psutil 7.2.2, pytest 9.1.1).
- [x] M1 Kern: `app/config.py` (pydantic, deutsche Fehlermeldungen, TODO-Erkennung),
  `app/auth.py` (Token in secrets.yaml, Bearer, hmac.compare_digest, 5 Fehlversuche → 60 s
  Sperre mit 429), `app/netguard.py` (ASGI-Middleware, 403 außerhalb erlaubter Netze),
  `app/tools/registry.py`, `app/main.py`. Verifiziert: `pytest` 43 passed;
  `uvicorn app.main:app` ohne config.yaml → klare Meldung, kein Stacktrace.

## In Arbeit
- M2 Tools (pc, led, sensors, scripts) – bisher nur Platzhalter in `jarvis/app/tools/`.

## Nächste Schritte

## Offene Fragen / Blocker

## Konventionen
