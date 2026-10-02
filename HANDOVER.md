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

- [x] M2 Tools: `app/tools/pc.py` (pc_shutdown → nur confirm_required, Ausführung nur über
  `POST /api/confirm/{id}`, 30 s gültig, einmalig; pc_shutdown_cancel = `shutdown /a`),
  `app/tools/led.py` (WLED-JSON-API laut Doku kno.wled.ge, gelesen über
  github.com/wled/WLED-Docs `docs/interfaces/json-api.md`), `app/tools/sensors.py`
  (Adapter `esphome_rest`, 3 s Timeout, Fehler pro Sensor), `app/tools/scripts.py`
  (Popen mit Argumentliste, eigene Prozessgruppe, PID+Startzeit in `state/scripts.json`).
  `scripts/probe.py` (nur GET). Verifiziert: `pytest` 81 passed (subprocess gemockt,
  HTTP per httpx.MockTransport, Dummy-Skript `tests/dummy_script.py`).
- [ ] ESPHome-Probe gegen das echte Gerät: aus der Cloud-Umgebung NICHT möglich
  ("Destination IP is in a private/reserved range"). → User-Aufgabe.

## In Arbeit
- M3 LLM-Agent (`app/llm.py`) und Regel-Parser (`app/fallback.py`) – noch Platzhalter.

## Nächste Schritte

## Offene Fragen / Blocker

## Konventionen
