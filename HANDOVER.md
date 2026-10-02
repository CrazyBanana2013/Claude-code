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

- [x] M3 LLM: `app/llm.py` (Ollama `/api/chat`, stream=false, tools aus Registry,
  temperature 0.2, keep_alive aus Config, max. 4 Tool-Runden, Gesamt-Timeout per
  `asyncio.timeout`; Fehler: nicht erreichbar / 404 Modell fehlt / "does not support tools"),
  `app/fallback.py` (Regel-Parser), `POST /api/chat` → `{reply, tool_calls, source, llm_error?}`.
  `scripts/pick_model.py` sucht das kleinste Modell mit capability "tools".
  Verifiziert: `pytest` 124 passed (Ollama gemockt: Tool-Call, unbekanntes Tool abgelehnt,
  Schleifenlimit, Fallback bei Verbindungsfehler/404/400/500/Timeout).

- [x] M4 Oberfläche `web/index.html`: Single-File, keine externen Ressourcen, Token-Dialog
  (localStorage `jarvis.token`), Statusleiste (Server/LLM), Kacheln Licht/Klima/Skripte/PC,
  Chat mit Tool-Chips, Bestätigen-Dialog mit Countdown. Nutzt `/api/tools/*` direkt.
  Verifiziert: `tests/test_ui.py` (Server startet per uvicorn mit Kopie von
  config.example.yaml, `GET /` 200, `/api/tools` ohne Token 401, keine externen Ressourcen,
  alle in der UI verwendeten Tool-Namen existieren in der Registry) + manueller
  Playwright-Durchlauf (390×844) gegen Fake-WLED/ESPHome: keine JS-Fehler.

- [x] M5 `launcher/wake.html`: Einstellungen (MAC, MyFRITZ!-Host, UDP-Port, JARVIS-URL) nur in
  localStorage (`jarvis.wake.settings`), "PC starten" öffnet
  `https://www.depicus.com/wake-on-lan/woli?m=<MAC ohne Trenner>&i=<Host>&s=255.255.255.255&p=<Port>`,
  danach alle 3 s `fetch(<URL>/api/health, {mode:"no-cors"})` mit 2,5 s Abbruch, bei Erfolg
  "JARVIS öffnen" + Weiterleitung nach 3 s ("Hier bleiben" stoppt sie). Pop-up-blockiert →
  Link. Verifiziert: `tests/web/wake.test.mjs` (Playwright/Chromium, 5 Fälle) läuft über
  `tests/test_wake_launcher.py` in pytest (wird übersprungen, wenn Node/Playwright fehlen).
  Auf einem echten Handy NICHT getestet → User-Aufgabe.

## In Arbeit
- M6 Betrieb: `scripts/start.ps1`, `scripts/install_autostart.ps1`, README, Gesamtlauf.

## Nächste Schritte

## Offene Fragen / Blocker

## Konventionen
