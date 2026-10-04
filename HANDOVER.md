# Handover – JARVIS (lokaler KI-Assistent)
Letztes Update: 2026-10-04 06:45 UTC

## Ziel
Kleiner Webserver (FastAPI) auf einem Windows-PC mit JARVIS-Oberfläche (Chat + Schnellaktionen),
erreichbar im LAN und über Tailscale vom Handy. Ein kleines lokales Ollama-Modell übersetzt
Sprache in Tool-Aufrufe (PC herunterfahren, WLED-LED-Strip, Zimmertemperatur via ESPHome,
Skripte starten/stoppen). Alles funktioniert auch ohne LLM (Buttons + Regel-Parser).
Dazu `jarvis/launcher/wake.html` fürs Handy: weckt den PC über die Depicus-WoL-Seite und leitet
danach zu JARVIS weiter.

## Umgebung
- Code liegt im Repo `Claude-code`, Projektordner `jarvis/`; diese Datei liegt im Repo-Root.
- Entwicklung bisher: Linux-Cloud-Container (NICHT der Ziel-PC). Python 3.11.15, uv 0.8.17,
  Node 22 mit global installiertem Playwright 1.56.1 (Chromium unter `/opt/pw-browsers`).
- Kein Ollama, kein Windows, kein Zugriff aufs Heimnetz (private IP-Bereiche werden vom
  Egress-Proxy geblockt: "Destination IP is in a private/reserved range").
  Deshalb konnten `winver`, `ollama --version`, `ollama list` und die ESPHome-Probe NICHT
  ausgeführt werden → User-Aufgaben unter "Nächste Schritte".
- Ziel-PC: Windows, Ryzen 9800X3D, RX 9070 XT, Ollama mit ROCm auf Port 11434,
  Standardbenutzer ohne Adminrechte.
- Abhängigkeiten (in `jarvis/pyproject.toml`, gelockt in `jarvis/uv.lock`, Index pypi.org):
  fastapi 0.142.2, uvicorn 0.54.0, httpx 0.28.1, pydantic 2.13.5, pyyaml 6.0.3, psutil 7.2.2;
  dev: pytest 9.1.1, anyio 4.15.1.
- Befehle (im Ordner `jarvis`):
  - Installieren: `uv sync`
  - Tests: `.venv/bin/python -m pytest` (Windows: `.venv\Scripts\python.exe -m pytest`)
  - Starten: `python -m app` bzw. `powershell -ExecutionPolicy Bypass -File scripts\start.ps1`
    (alternativ `uvicorn app.main:app --host 0.0.0.0 --port 8765`)
  - Umgebungsvariablen (optional, v. a. für Tests): `JARVIS_CONFIG`, `JARVIS_SECRETS`, `JARVIS_STATE`.
  - Installer-Tests brauchen PowerShell: `JARVIS_PWSH=<pfad>/pwsh .venv/bin/python -m pytest`
    (ohne pwsh werden sie übersprungen). In der Cloud-Umgebung wurde PowerShell 7.4.6 nach
    `<scratchpad>/pwsh/` entpackt (github.com/PowerShell/PowerShell Releases, linux-x64 tar.gz);
    PSScriptAnalyzer (PS-5.1-Kompatibilitätsprofil) lag unter `<scratchpad>/psmodules/` – beides
    nicht im Repo, bei Bedarf neu holen.
  - Release-ZIP bauen: `.venv/bin/python scripts/build_installer.py` → `dist/JARVIS-Setup-<version>.zip` + `.sha256`.
  - Langsame Tests (Browser-HUD-Test ~3 min, Installer-E2E): Marker `slow`; `pytest -m "not slow"` lässt sie aus.

## Projektstruktur
- `jarvis/app/main.py` – `create_app()` (für Tests) + lazy `app` für uvicorn; Routen `/`,
  `/api/health`, `/api/status`, `/api/tools`, `/api/tools/{name}`, `/api/confirm/{id}`, `/api/chat`.
- `jarvis/app/__main__.py` – `python -m app`: Host/Port aus config.yaml; unter pythonw.exe
  Ausgabe nach `state/logs/server.log`.
- `jarvis/app/config.py` – pydantic-Modelle, deutsche Fehlermeldungen, `is_todo()`, `config_warnings()`.
- `jarvis/app/auth.py` – Token in secrets.yaml, Bearer-Prüfung mit `hmac.compare_digest`, Lockout.
- `jarvis/app/netguard.py` – ASGI-Middleware, 403 außerhalb erlaubter Netze.
- `jarvis/app/tools/registry.py` – `Tool`, `Registry`, `ToolContext`, Fehlerklassen, `run_recorded()`.
- `jarvis/app/tools/pc.py` – `pc_shutdown` (nur confirm_required), `pc_shutdown_cancel`, `confirm()`.
- `jarvis/app/tools/led.py` – `led_power`, `led_brightness`, `led_color`, `led_effect`, `led_preset`, `led_status`.
- `jarvis/app/tools/sensors.py` – `sensors_read`, Adapter-Tabelle `ADAPTERS` (`esphome_rest`).
- `jarvis/app/tools/scripts.py` – `scripts_list/start/stop/status`, `ScriptManager`.
- `jarvis/app/llm.py` – `OllamaAgent` (chat mit Tool-Schleife, status).
- `jarvis/app/fallback.py` – Regel-Parser `parse()` + `handle()`.
- `jarvis/web/index.html` – Oberfläche (eine Datei).
- `jarvis/launcher/wake.html` – Handy-Launcher (eine Datei).
- `jarvis/scripts/start.ps1`, `install_autostart.ps1`, `stop.ps1` – Windows-Betrieb.
- `jarvis/scripts/probe.py` – nur-GET-Probe gegen WLED/ESPHome.
- `jarvis/scripts/pick_model.py` – kleinstes Ollama-Modell mit capability "tools" finden.
- `jarvis/tests/` – pytest (Mocks für Geräte/Ollama), `tests/dummy_script.py`,
  `tests/web/wake.test.mjs` (Playwright, via `tests/test_wake_launcher.py`).
- `jarvis/config.example.yaml` – einzige versionierte Config; `config.yaml`, `secrets.yaml`,
  `state/` sind in `jarvis/.gitignore`.
- `jarvis/README.md` – Einrichtung für den User (Installer zuerst, manuelle Einrichtung als Alternative).
- `jarvis/Install.cmd`, `jarvis/Uninstall.cmd` – Doppelklick-Wrapper für `scripts/install.ps1` / `scripts/uninstall.ps1`.
- `jarvis/scripts/installer-lib.ps1` – alle Installer-Funktionen (Kopieren, Python-Umgebung, Marker,
  Verknüpfungen, Server starten/stoppen, Löschlogik, Sicherheitsprüfungen); von install/uninstall/stop/
  install_autostart dot-gesourct.
- `jarvis/scripts/start-hidden.ps1` – Startmenü „JARVIS starten“: ohne Fenster starten, auf /api/health warten, Browser öffnen.
- `jarvis/app/setup_wizard.py` – Einrichtungsassistent `python -m app.setup_wizard configure|token|info`.
- `jarvis/app/ollama_models.py` – nur lesende Ollama-Abfrage (`/api/tags`, `/api/show`), genutzt von Assistent und pick_model.py.
- `jarvis/scripts/build_installer.py` – deterministisches Release-ZIP mit Secret-Sperre.
- `jarvis/requirements.txt` – `uv export --frozen --no-dev` mit Hashes (pip-Fallback des Installers).
- `jarvis/.gitattributes` – Text LF, `.ps1/.cmd/.bat` CRLF.
- `jarvis/tests/ps/*.tests.ps1` – pwsh-Tests des Installers (lib, install-flow, uninstall, stop, syntax, windows-sim);
  `tests/test_installer_ps.py`, `test_installer_e2e.py`, `test_installer_ctrlc.py`, `test_build_installer.py`,
  `test_setup_wizard.py`, `test_main_pidfile.py`.
- `jarvis/tests/web/hud.test.mjs` + `tests/test_ui_browser.py` – Playwright-Test der HUD-Oberfläche (25+ Fälle,
  eigener Fake-WLED/ESPHome und echter JARVIS-Server; `/api/confirm` wird im Browser immer blockiert).

## Architektur & Entscheidungen
- **Eine Registry** (`app/tools/registry.py`): Parameter jedes Tools sind ein pydantic-Modell;
  daraus entstehen JSON-Schema (für Ollama, bereinigt: keine titles, kein anyOf-null) und die
  Validierung. LLM, Fallback und UI rufen alle `Registry.execute()` auf.
- **Shutdown nur mit Bestätigung:** `pc_shutdown` erzeugt eine einmalige ID (30 s). Ausgeführt
  wird nur in `pc.confirm()`, das ausschließlich die Route `POST /api/confirm/{id}` aufruft.
  Es gibt kein Tool, über das das LLM bestätigen könnte. Auf Nicht-Windows verweigert
  `run_command()` (Schutz der Entwicklungsmaschine).
- **Fehlerformat:** Tool-Fehler → `{"ok": false, "error": "..."}` mit 404 (unbekanntes Tool),
  422 (Parameter), 400 (Gerät/TODO). Erfolg → `{"ok": true, "tool", "result"}`.
- **Lockout:** fehlender oder falscher Token zählt als Fehlversuch; ab 5 → 429 mit Retry-After
  für 60 s, auch mit richtigem Token. Erfolg setzt den Zähler zurück.
- **Netguard prüft nur die TCP-Quelladresse** (keine X-Forwarded-For), weil kein Reverse-Proxy
  vorgesehen ist. `::1` wird zusätzlich erlaubt, IPv4-mapped IPv6 wird entpackt.
- **WLED:** POST `/json/state` mit `"v": true`; `"seg"` als Objekt (wirkt auf alle ausgewählten
  Segmente); Helligkeit 0 % → `{"on": false}` (Doku empfiehlt das statt bri 0); Effekte über
  `/json/eff` (Namen "RSVD"/"-" sind reserviert), Preset-Namen über `GET /presets.json`
  (Datei des WLED-Dateisystems; in der JSON-API-Doku nur indirekt erwähnt → am echten Gerät
  prüfen; Preset per Nummer funktioniert unabhängig davon). Quelle: WLED-Docs-Repo
  `docs/interfaces/json-api.md` (kno.wled.ge war vom Proxy gesperrt).
- **ESPHome:** `GET /sensor/<Name>` → Feld `value`. Laut aktueller Doku (esphome-docs-Repo,
  `src/content/docs/web-api/index.mdx`) ist `<Name>` der Entitätsname aus der YAML; ältere
  Firmware nutzt die object_id. Daher bei 404 automatischer zweiter Versuch mit object_id-Form.
  NaN/null → "kein Messwert". Fehler pro Sensor, nie Gesamtabbruch.
- **Skripte:** `Popen(list, shell=False)`. Windows: `CREATE_NEW_PROCESS_GROUP` +
  (`CREATE_NO_WINDOW` und Log-Datei, wenn `hide_window`, sonst `CREATE_NEW_CONSOLE`). POSIX:
  `start_new_session=True`. Stop: Windows `taskkill /PID x /T` ohne /F (WM_CLOSE), POSIX
  SIGTERM an die Gruppe; nach 5 s `psutil` kill des ganzen Baums. PID + create_time in
  `state/scripts.json` → Status stimmt nach Neustart, PID-Wiederverwendung wird erkannt.
  Verworfen: `CTRL_BREAK_EVENT` als sanfter Stop, weil das nur im selben Konsolenfenster geht
  und der Server per pythonw keine Konsole hat.
- **LLM:** Ollama `/api/chat`, `stream:false`, `options.temperature`, `keep_alive` aus Config,
  optional `think`. Tool-Ergebnisse gehen als `{"role":"tool","tool_name":...}` zurück (laut
  Ollama-Doku `docs/api.md`). `arguments` als Objekt oder JSON-String akzeptiert.
  Fallback auf Regel-Parser nur, wenn das LLM fehlschlägt, BEVOR ein Tool ausgeführt wurde
  (sonst würde doppelt ausgeführt) – dann Antwort "Teilweise erledigt, dann Fehler: …".
  Fehlererkennung: Verbindungsfehler; HTTP 404 oder "not found" → Modell fehlt;
  "does not support tools" → kein Tool-Support (genauer Fehlertext von Ollama am echten Gerät
  nicht geprüft, Erkennung per Teilstring).
- **App lazy laden:** `app.main.__getattr__("app")` lädt Config erst beim Zugriff durch uvicorn,
  damit Tests `create_app()` ohne config.yaml importieren können. Config-Fehler →
  `SystemExit(2)` mit Klartext, kein Stacktrace.
- **wake.html:** `window.open(url, "_blank")` ohne "noopener"-Feature (damit liefert
  window.open immer null und die Pop-up-Erkennung wäre falsch), danach `opener = null`.
  Abfrage-Schleife mit Generationszähler gegen parallele Schleifen; `visibilitychange` prüft
  beim Zurückkehren sofort.
- **Autostart:** Verknüpfung im Startup-Ordner auf `.venv\Scripts\pythonw.exe -m app`
  (Fallback python.exe minimiert). Keine Aufgabenplanung (Scope).
- **Installer pro Benutzer** (`%LOCALAPPDATA%\JARVIS`), weil der Alltag ein Standardkonto ist.
  Läuft er erhöht (Admin), bricht er ab (außer `-AllowAdmin`). Logik in PowerShell, weil auf
  jedem Windows vorhanden; alles Testbare (Config schreiben, Probes, Modellwahl) in Python
  (`app/setup_wizard.py`). Verworfen: klassische setup.exe (Inno Setup/NSIS) – hier nicht
  bau-/testbar, bringt für ein Pro-Benutzer-Kopieren keinen Mehrwert.
- **Python-Umgebung:** uv (`uv sync --frozen --no-dev`) bevorzugt; uv wird nur nach Zustimmung
  mit dem offiziellen Befehl aus der uv-Doku installiert (`UV_NO_MODIFY_PATH=1`), sonst
  `python -m venv` + `pip install --require-hashes -r requirements.txt`. Python wird vor der
  uv-Frage gesucht; Microsoft-Store-Python wird für den venv-Fallback übersprungen (MSIX-Pfade).
- **Löschsicherheit:** Deinstallation nur mit Marker `.jarvis-install.json` (Dateiliste); gelöscht
  werden nur gelistete Dateien, nie Wildcards; config.yaml, config.yaml.bak, secrets.yaml, state/
  nur mit `-Purge`. Ohne Purge bleibt ein „Rest-Marker“ (leere Liste, `uninstalled_at`), damit ein
  späteres `-Purge` möglich ist. Ziele wie Laufwerkswurzel, Profil, LOCALAPPDATA, UNC werden
  abgelehnt; Links/Junctions werden nie verfolgt. OneDrive-Platzhalter zählen NICHT als Link
  (Reparse-Tag mit Name-Surrogate-Bit prüfen) – sonst fehlten Programmdateien.
- **Firewall:** Der Installer ändert nichts, er zeigt drei Admin-Befehle: zwei Allow-Regeln
  (LAN, Tailscale) und das Entfernen der von Windows beim ersten Start automatisch angelegten
  Block-Regeln für das Python von JARVIS (Block hat Vorrang vor Allow; Standardbenutzer können
  im Windows-Dialog nicht „Zulassen“).
- **PID-Datei** `state/server.pid` (pid, executable, started, create_time) aus `python -m app`;
  stop/uninstall prüfen damit den richtigen Prozess (venv-Launcher → Kind-Interpreter).
- **HUD-Oberfläche** (`web/index.html`, „Arc-Reaktor-Strudel“): ausgewählt per Jury aus 3
  Entwürfen (arc-vortex 24,5 / tactical-hud 24 / holo-galaxy 19,5 Punkte; Kriterien Film-Optik,
  Handy-Bedienung, Technik). Ein Canvas mit einer rAF-Schleife (DPR ≤ 2, Partikelzahl nach
  Fläche, Pause bei verdecktem Tab, reduzierte Bewegung = statisch), Knoten sind echte
  `<button>`s mit Live-Werten, Panels als Bottom-Sheet (Handy) bzw. Seitenpanel (≥ 900 px).
  Funktionsgleich mit der alten Kachel-Oberfläche plus Effekt-/Preset-Schnellwahl. Die alte
  Oberfläche ist nur noch in der Git-Historie (Commit c9dedba).

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
  ("Destination IP is in a private/reserved range"). → User-Aufgabe (Nächste Schritte, Punkt 4).
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
- [x] M6 Betrieb: `app/__main__.py`, `scripts/start.ps1`, `scripts/install_autostart.ps1`
  (Verknüpfung in `[Environment]::GetFolderPath("Startup")`, WindowStyle 7, `-Remove`),
  `scripts/stop.ps1`, `README.md` (Einrichtung, Firewall, Tailscale, Autostart, wake.html-
  Einschränkungen, Test-Checkliste, Warnung "Niemals Port 8765 freigeben"), `uv.lock`.
  Gesamtlauf: `pytest` 131 passed; `python -m app` mit Kopie von config.example.yaml startet,
  `GET /api/health` 200, `GET /` 200, `/api/status`, `/api/tools`, `/api/tools/*`,
  `/api/confirm/*` ohne Token 401 (6. Fehlversuch korrekt 429); Secret-Scan per `git grep`
  (MAC-, IPv4-, Token-, Pfadmuster): nur Platzhalter/Testwerte.
  **Die PowerShell-Skripte konnten nicht ausgeführt werden** (kein Windows/PowerShell in der
  Entwicklungsumgebung) → nur per Code-Review geprüft.

- [x] Installer (Version 0.2.0, Stand vor dem unabhängigen Review): `jarvis/Install.cmd` →
  `scripts/install.ps1` (pro Benutzer nach `%LOCALAPPDATA%\JARVIS`, ohne Admin, uv nur nach
  Zustimmung sonst venv+pip mit `requirements.txt` inkl. Hashes, Einrichtungsassistent
  `python -m app.setup_wizard configure|token|info`, Startmenü, Autostart, Serverstart,
  Zusammenfassung mit Firewall-Befehlen zum Selbst-Ausführen), `Uninstall.cmd` →
  `scripts/uninstall.ps1` (nur mit Marker `.jarvis-install.json`, löscht nur bekannte Dateien,
  config/secrets/state nur mit `-Purge`), gemeinsame Funktionen `scripts/installer-lib.ps1`,
  PID-Datei `state/server.pid` aus `python -m app`, Release-ZIP `scripts/build_installer.py`
  → `dist/JARVIS-Setup-<version>.zip`. Verifiziert: `JARVIS_PWSH=<pfad zu pwsh> pytest` 324 passed
  (inkl. pwsh-Tests unter `tests/ps/` und End-to-End `tests/test_installer_e2e.py`: ZIP bauen →
  installieren → Update → deinstallieren → Purge). Getestet mit PowerShell 7.4.6 unter Linux
  (Testschalter `-AllowNonWindows`); Windows PowerShell 5.1 nur per PSScriptAnalyzer-Profil.

- [x] Unabhängiges Review des Installers: 4 Prüfbereiche (Windows/PS 5.1, Sicherheit/Scope,
  Python, Bedienung/Doku), 41 Funde, 37 nach Gegenprüfung bestätigt (1 hoch: Firewall-Block-
  Regeln; 4 mittel: OneDrive-Platzhalter, Block-Regeln beim ersten Start, Installation in
  fremden Ordner, Absturz bei falsch getippter IPv4), alle umgesetzt inkl. Tests; nicht umgesetzt
  (bewusst/optional): GitHub-Release veröffentlichen (Entscheidung User), eigener Exit-Code bei
  nicht gestartetem Server, 5. Startmenü-Eintrag „einrichten“, eigene Zustimmung für uv-Python-
  Download. Verifiziert: `JARVIS_PWSH=… pytest` 382 passed; PSScriptAnalyzer PS-5.1-Profil 0 Funde.
- [x] HUD-Oberfläche im Film-JARVIS-Stil (Strudel, Arme zu Knoten, bewegte Partikel) in
  `web/index.html`; Token-Dialog mit Installer-Hinweis und „Token anzeigen“. Verifiziert:
  `tests/test_ui_browser.py` (Playwright, 3 Läufe stabil), Knack-Test (320×568 bis 2560 px,
  flaches Querformat, Server offline, 429, langsame API, Doppeltipp, 8 Skripte/6 Sensoren mit
  langen Namen, verdeckter Tab, reduzierte Bewegung, 60 s ohne Speicherwachstum) – 6 Layout-/
  Bedienfehler gefunden und behoben. Nur headless Chromium unter Linux, kein echtes Handy.

- [x] GitHub-Release v0.2.0 (Vorabversion) mit `JARVIS-Setup-0.2.0.zip` + `.sha256`:
  https://github.com/CrazyBanana2013/Claude-code/releases/tag/v0.2.0 – erzeugt von
  `.github/workflows/release.yml` (Push auf main/Entwicklungs-Branch: legt Release + Tag
  `v<version>` an, wenn es die Version aus `jarvis/pyproject.toml` noch nicht gibt; sonst nichts).
  Grund: Tag-Pushes sind aus der Cloud-Umgebung blockiert („unexpected disconnect“), Branch-Pushes
  nicht. Verifiziert: Lauf 37183308693 erfolgreich; das ZIP auf GitHub ist byte-identisch mit
  einem lokalen Bau desselben Commits (SHA-256 60ad1b80…), 35 Dateien, keine config/secrets.
  **Neues Release = Version in `jarvis/pyproject.toml` erhöhen und pushen.**

## In Arbeit
- Nichts. Offen sind nur Aufgaben am echten PC/Handy (siehe Nächste Schritte).

## Nächste Schritte
Empfohlener Weg mit dem Installer (ersetzt die manuellen Punkte 1, 2, 5 und 12 der Liste darunter):
A. **Paket holen:** `JARVIS-Setup-0.2.0.zip` aus dem GitHub-Release v0.2.0 herunterladen
   (oder Repo als ZIP / klonen; im Ordner `jarvis` liegt `Install.cmd`). Alternativ Release-ZIP bauen
   (`python scripts\build_installer.py`). ZIP vor dem Entpacken: Rechtsklick > Eigenschaften >
   „Zulassen“. NICHT in einen OneDrive-Ordner entpacken (z. B. nach `C:\JARVIS-Setup`).
B. **`Install.cmd` doppelklicken** (nicht als Administrator). Der Assistent fragt WLED-IP,
   ESPHome-IP + Sensornamen, Pfad zum CS2-Skript und das Ollama-Modell ab. Token aus Schritt 6
   notieren. Danach die drei Firewall-Befehle aus der Zusammenfassung in einer Admin-PowerShell
   ausführen (inkl. Entfernen der Block-Regeln).
C. Weiter mit den Punkten 3, 4 und 6–11 unten (Modell, ESPHome, Firewall-Prüfung, Tailscale,
   LED-Test, CS2-Skript, Herunterfahren, wake.html).
D. **Neue Oberfläche am Handy prüfen** (390 px und quer): Knoten antippen, Panels öffnen/schließen,
   Animation flüssig? Akku/Wärme nach 10 min? Ergebnis hier notieren.
E. **Rückmeldung bei Windows-Fehlern** des Installers: Meldung + `%LOCALAPPDATA%\JARVIS\state\logs\server.log`
   hier eintragen.

Manuelle Einrichtung ohne Installer (Alternative), in dieser Reihenfolge:
1. **Umgebung prüfen** (PowerShell): `winver`, `python --version` (≥ 3.11), `uv --version`,
   `ollama --version`, `ollama list`. Danach im Ordner `jarvis`: `uv sync` und
   `.venv\Scripts\python.exe -m pytest` (erwartet: alles grün; der wake.html-Browsertest wird
   ohne Node/Playwright übersprungen). Bei Fehlern unter Windows die Meldung hier eintragen.
2. **Config ausfüllen:** `copy config.example.yaml config.yaml`, dann alle `TODO`-Werte:
   `wled.base_url`, beide `sensors[*].base_url` + `entity_id` (exakter Entitätsname aus der
   ESPHome-YAML), `scripts[0].cwd` + `command` (CS2-Skript als Argumentliste),
   `llm.model` (Punkt 3).
3. **Modell wählen:** `.venv\Scripts\python.exe scripts\pick_model.py`. Gibt es kein
   installiertes Modell mit capability `tools`, ein kleines Tool-Modell von
   https://ollama.com/search?c=tools ziehen (`ollama pull <name>`), Skript erneut ausführen und
   den Namen in `llm.model` eintragen. Bis dahin läuft JARVIS mit dem Regel-Parser.
4. **ESPHome prüfen (nur lesen):** `.venv\Scripts\python.exe scripts\probe.py --esphome
   http://<IP des ESPHome-Geräts> --entity "<Sensorname>"`. Liefert `/` keinen ESPHome-
   Webserver (Fehler/404), in der ESPHome-YAML ergänzen und selbst flashen:
   ```yaml
   web_server:
     port: 80
   ```
   Falls ein BME280 noch fehlt, ist das eine eigene Firmware-Aufgabe (nicht Teil von JARVIS).
   Danach `scripts\probe.py` ohne Argumente: alle Sensoren müssen HTTP 200 liefern.
5. **Erster Start:** `powershell -ExecutionPolicy Bypass -File scripts\start.ps1`; Token aus
   der Konsole notieren (steht auch in `secrets.yaml`).
6. **Firewall (einmalig mit Adminrechten)** – Befehle in README Abschnitt 5. Vorher
   `Get-NetConnectionProfile` (Heimnetz = Private) und `Get-NetAdapter` (Name des
   Tailscale-Adapters) prüfen.
7. **Tailscale** auf PC und Handy einrichten (README Abschnitt 6), Tailscale-IP des PCs
   notieren (`tailscale ip -4`).
8. **Erster echter LED-Schalttest** über die Oberfläche: An, Aus, Helligkeit, eine Farbe,
   ein Effekt; Preset per Name testen (bestätigt, dass `GET /presets.json` so funktioniert).
9. **CS2-Skript** über die Kachel starten/stoppen; JARVIS neu starten und prüfen, dass der
   Status "läuft" erhalten bleibt. Prüfen, ob der sanfte Stop reicht oder nach 5 s hart
   beendet wird (Ergebnis `stopped` vs. `killed` in der Antwort).
10. **Herunterfahren testen:** Dialog → Abbrechen (nichts passiert); dann Bestätigen und in
    der PC-Kachel "Abbrechen" drücken (`shutdown /a`); zuletzt einmal echt herunterfahren.
11. **Praxistest wake.html:** Datei aufs Handy kopieren, im Browser öffnen, Einstellungen
    ausfüllen. PC aus, Handy auf mobilen Daten mit Tailscale an, "PC starten" → nach dem
    Hochfahren muss JARVIS automatisch öffnen. Klappt das Öffnen der lokalen Datei nicht,
    Einschränkungen in README Abschnitt 8 beachten und Ergebnis hier notieren.
12. **Autostart:** `powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1`,
    ab-/anmelden, prüfen dass JARVIS ohne Fenster läuft (`state\logs\server.log`).

Mögliche Weiterentwicklung (nicht beauftragt): eigener Sensor-Hub als weiterer Adapter in
`app/tools/sensors.py` (`ADAPTERS`-Tabelle + neuer `type` in `SensorConfig`).

## Offene Fragen / Blocker
- Kein Zugriff auf Ziel-PC, Ollama und Heimnetz aus der Entwicklungsumgebung: Modellwahl,
  ESPHome-Entitäten und WLED-Verhalten sind ungeprüft (siehe Nächste Schritte 3, 4, 8).
- Genaue Fehlertexte von Ollama für "Modell fehlt" / "keine Tools" nicht am echten System
  geprüft; Erkennung über HTTP 404 bzw. Teilstring "does not support tools".
- `GET /presets.json` für Preset-Namen ist in der JSON-API-Doku nicht als Route gelistet
  (nur als Datei erwähnt) → am Gerät bestätigen.
- Ob der Tailscale-Adapter unter Windows "Tailscale" heißt und welches Netzwerkprofil er hat,
  ist ungeprüft (README nennt die Prüfbefehle).
- Ob `uv venv` unter Windows `pythonw.exe` anlegt, ist ungeprüft; `install_autostart.ps1`
  fällt sonst auf `python.exe` (minimiert) zurück.
- Installer auf echtem Windows nie ausgeführt: Windows PowerShell 5.1 nur statisch geprüft
  (PSScriptAnalyzer), Windows-Zweige (WScript.Shell-Verknüpfungen, pythonw, Get-CimInstance,
  Unblock-File, Set-Clipboard, ACLs, Reparse-Tags/OneDrive, Admin-Erkennung, cmd.exe-Verhalten)
  nur simuliert (`tests/ps/windows-sim.tests.ps1`). → Nächste Schritte B/E.
- ~~Entscheidung User: GitHub-Release veröffentlichen?~~ → Ja, erledigt (v0.2.0, Vorabversion).
- Token aufs Handy bringen ist umständlich (43 Zeichen abtippen oder per Messenger an sich selbst).
  Mögliche Verbesserung (nicht beauftragt): Kopplungs-QR-Code am PC, der die Tailscale-URL mit Token
  enthält (Token im URL-Fragment `#token=…`, UI übernimmt ihn in localStorage).
- HUD kosmetisch offen: Helligkeitsregler steht vor dem ersten LED-Status auf 50 %, Anzeige „–“;
  auf dem Desktop überlappt der Telemetrie-Text leicht einige Strudel-Linien.
- Entscheidung User: Soll das CS2-Skript mit eigenem Fenster laufen (`hide_window: false`,
  Standard) oder unsichtbar mit Log-Datei (`hide_window: true`)?

## Konventionen
- Sprache: UI-Texte, Fehlermeldungen, Kommentare und Commits auf Deutsch; Code-Bezeichner Englisch.
- Python ≥ 3.11, `from __future__ import annotations`, Zeilenlänge ~110, keine neuen
  Abhängigkeiten ohne Versionsprüfung.
- Tools: Name `bereich_aktion` (`led_power`), Parameter als pydantic-Modell mit
  `extra="forbid"`, Fehler für den User als `ToolError` mit verständlichem deutschen Text.
- Geräte nur über `ctx.http` (in Tests per `httpx.MockTransport` ersetzt); Prozesse in Tests
  immer mocken (`pc.subprocess.run`, `pc.is_windows`).
- Keine echten MACs, IPs, Hostnamen, Token oder Pfade im Repo; Beispiele mit `TODO`,
  `AA:BB:CC:DD:EE:FF`, `beispiel.myfritz.net`.
- Commits: ein Commit pro Meilenstein, Format `JARVIS M<n>: <kurze Beschreibung>` (ab 0.2.0: `JARVIS <version>: …`).
- `.ps1`/`.cmd`: nur ASCII, PS-5.1-kompatibel (kein `??`, `?.`, Ternary, `&&`/`||`), CRLF; Tests prüfen das.
- Branch: `claude/affectionate-pasteur-utoby7`.
