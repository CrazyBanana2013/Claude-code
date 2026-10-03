# JARVIS – lokaler Assistent für PC, LED-Strip, Zimmersensoren und Skripte

Ein kleiner Webserver auf dem Windows-PC mit einer JARVIS-Oberfläche fürs Handy im Stil
der Filme: In der Mitte dreht sich ein Arc-Reaktor im Partikel-Strudel, Arme führen zu den
Knoten Licht, Klima, Skripte, PC und System (antippen öffnet das jeweilige Bedienfeld:
Helligkeit, Farbe, Effekte, Temperatur, Skripte starten/stoppen, PC herunterfahren), dazu ein
Befehls-Chat. Ein kleines **lokales** Ollama-Modell übersetzt Sätze wie „Licht auf 40 % und
blau“ in Tool-Aufrufe. Ohne Ollama übernimmt ein Regel-Parser die Kernbefehle; die Buttons
brauchen das Modell ohnehin nicht.

> [!CAUTION]
> **Niemals Port 8765 in der FRITZ!Box (oder einem anderen Router) freigeben.**
> JARVIS ist nur fürs Heimnetz und für Tailscale gedacht. Von unterwegs kommst du über
> Tailscale (VPN) an den Server, nicht über eine Portfreigabe. Zusätzlich lehnt der Server
> jede Anfrage ab, die nicht aus 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 oder
> 100.64.0.0/10 (Tailscale) kommt.

## Inhalt

- [Was wo liegt](#was-wo-liegt)
- [1. Installation](#1-installation)
- [2. Konfiguration](#2-konfiguration-configyaml)
- [3. Modell auswählen](#3-modell-auswählen)
- [4. Erster Start und Token](#4-erster-start-und-token)
- [5. Windows-Firewall](#5-windows-firewall)
- [6. Tailscale](#6-tailscale-auf-pc-und-handy)
- [7. Autostart](#7-autostart)
- [8. Handy-Launcher wake.html](#8-handy-launcher-wakehtml)
- [9. Test-Checkliste](#9-test-checkliste)
- [Bedienung](#bedienung)
- [Sicherheit](#sicherheit)
- [Entwicklung und Tests](#entwicklung-und-tests)
- [Fehlerbehebung](#fehlerbehebung)

## Was wo liegt

| Pfad | Zweck |
|---|---|
| `app/main.py` | FastAPI-App: Routen `/`, `/api/health`, `/api/status`, `/api/tools`, `/api/tools/{name}`, `/api/confirm/{id}`, `/api/chat` |
| `Install.cmd`, `Uninstall.cmd` | Installer und Deinstallation per Doppelklick (rufen `scripts\install.ps1` bzw. `scripts\uninstall.ps1` auf) |
| `app/__main__.py` | `python -m app` – startet mit Host/Port aus `config.yaml`; solange der Server läuft, steht seine PID in `state/server.pid` |
| `app/setup_wizard.py` | Einrichtungsassistent (`configure`, `token`, `info`), vom Installer aufgerufen |
| `app/config.py` | Laden und Prüfen von `config.yaml` |
| `app/auth.py` | API-Token, Sperre nach Fehlversuchen |
| `app/netguard.py` | IP-Filter (nur LAN/Tailscale) |
| `app/tools/` | Tools: `pc.py`, `led.py`, `sensors.py`, `scripts.py`, `registry.py` |
| `app/llm.py` | Ollama-Agent (`/api/chat` mit Tools) |
| `app/fallback.py` | Regel-Parser ohne LLM |
| `web/index.html` | JARVIS-Oberfläche (eine Datei, keine externen Ressourcen) |
| `launcher/wake.html` | Handy-Seite: PC per Wake-on-LAN starten, dann zu JARVIS |
| `scripts/` | `install.ps1`, `uninstall.ps1`, `installer-lib.ps1` (gemeinsame Funktionen), `start.ps1`, `start-hidden.ps1` (Startmenü „JARVIS starten“), `install_autostart.ps1`, `stop.ps1`, `probe.py`, `pick_model.py` |
| `scripts/build_installer.py` | Baut das Release-Paket `dist/JARVIS-Setup-<version>.zip` (siehe [Entwicklung](#entwicklung-und-tests)) |
| `requirements.txt` | Paketliste mit Hashes für die Installation ohne uv (`pip --require-hashes`), erzeugt aus `uv.lock` |
| `config.example.yaml` | Vorlage – die echte `config.yaml` und `secrets.yaml` werden nie committet |

## 1. Installation

### Mit dem Installer (empfohlen)

Der Installer richtet JARVIS **pro Benutzer** ein, ganz ohne Adminrechte.

1. **Holen** – eine der drei Möglichkeiten:
   - **Ohne Werkzeuge:** auf GitHub die Repository-Seite öffnen, den Branch mit dem Installer wählen
     (Ordner `jarvis` enthält `Install.cmd`), dann *Code* → *Download ZIP*. Nach dem Entpacken liegt
     `Install.cmd` im Unterordner `jarvis`.
   - **Release-Paket** `JARVIS-Setup-<version>.zip` (samt `.sha256`), sobald es unter *Releases*
     veröffentlicht ist – oder selbst gebaut mit `scripts\build_installer.py` (siehe
     [Entwicklung](#entwicklung-und-tests)).
   - **Repository klonen** (braucht git); `Install.cmd` liegt dann im Ordner `jarvis`.
2. **ZIP freigeben, dann entpacken:** Rechtsklick auf die ZIP-Datei → *Eigenschaften* → unten bei
   „Sicherheit“ **Zulassen** anhaken → *OK*. Danach Rechtsklick → *Alle extrahieren …* – am besten
   nach `C:\JARVIS-Setup`, **nicht** in Desktop oder Dokumente, wenn diese mit OneDrive
   synchronisiert werden (dort sind Dateien oft nur „online verfügbar“; der Installer bricht dann mit
   einem Hinweis ab). Schon entpackt, ohne freizugeben? Dann fragt Windows beim Doppelklick „Datei
   öffnen – Sicherheitswarnung“: auf **Ausführen** klicken; bei SmartScreen („Der Computer wurde durch
   Windows geschützt“) auf **Weitere Informationen** → **Trotzdem ausführen**. Das ist unbedenklich:
   Das Paket ist nicht signiert, die Prüfsumme steht in der `.sha256`-Datei, und der Installer
   entfernt die Download-Markierung an den installierten Dateien selbst.
3. **Doppelklick auf `Install.cmd`** im entpackten Ordner – **nicht** „Als Administrator
   ausführen“. Läuft das Fenster mit Adminrechten, bricht der Installer ab: JARVIS soll unter deinem
   normalen Konto laufen (Notbremse für Ausnahmen: `Install.cmd -AllowAdmin`).

**Was der Installer fragt** (Enter nimmt jeweils den Vorschlag):

- **uv** fehlt? Dann sagt er zuerst, ob ein passendes Python (3.11 oder neuer) da ist, und fragt,
  ob er den offiziellen uv-Installer von astral.sh laden darf (Vorgabe: **Nein**; `-Yes` lädt uv nie,
  dafür gibt es `-InstallUv`). uv bringt bei Bedarf ein eigenes Python mit (von GitHub, siehe Tabelle
  unten). Ohne Zustimmung nimmt er `python -m venv` + `pip` mit der Hash-geprüften
  `requirements.txt` – dafür muss Python 3.11 oder neuer installiert sein. Fehlt es, bricht er mit
  einer Anleitung ab. Python ohne Adminrechte: python.org-Installer, auf der ersten Seite den Haken bei
  „Use admin privileges when installing py.exe“ **entfernen**, dann „Install Now“ – oder der „Python
  install manager“ von python.org. **Python aus dem Microsoft Store funktioniert hier nicht** (Windows
  leitet dessen Dateien unter AppData in einen eigenen, für den Installer unsichtbaren Ordner um).
  Am einfachsten ist die Zustimmung zum uv-Download.
- **Einrichtungsassistent** (bei der ersten Installation): WLED-Adresse, ESPHome-Gerät und
  Sensornamen, CS2-Skript (Pfad; der Startbefehl wird vorgeschlagen), Ollama-Modell, Port. Auf
  Wunsch testet er die Geräte kurz – nur lesend (GET), es wird nichts geschaltet. In Klammern steht
  der aktuelle Wert, Enter übernimmt ihn. `-` überspringt einen noch nicht eingerichteten Punkt
  (bleibt TODO, JARVIS startet trotzdem) bzw. setzt einen schon eingerichteten Wert auf TODO zurück.
  Gespeichert wird erst nach einer Zusammenfassung. **Strg+C** bricht ab: `config.yaml` bleibt
  unverändert, Windows fragt danach „Batchvorgang abbrechen (J/N)?“ – mit J bestätigen und
  `Install.cmd` später einfach erneut starten.
- **Einstellungen übernehmen?** – nur, wenn im Zielordner schon `config.yaml`/`secrets.yaml` ohne
  Installationsmarker liegen (Vorgabe: Nein), oder wenn du `Install.cmd` aus einem Repo-Ordner mit
  eigener `config.yaml` startest ([Umstieg](#umstieg-von-der-manuellen-einrichtung)).
- **Autostart** bei jeder Anmeldung, ohne Fenster (Vorgabe: Ja).
- **Firewall-Befehle** in die Zwischenablage kopieren (Vorgabe: Nein).

Kurz vor dem Start kann Windows fragen, ob „Python“ Netzwerkzugriff bekommt („Zugriff zulassen?“).
Ohne Adminrechte kannst du das nicht erlauben – **Abbrechen** ist in Ordnung, Windows legt dann aber
Block-Regeln an; wie du sie entfernst, steht in [Abschnitt 5](#5-windows-firewall).

Am Ende stehen da: die Adressen für PC, Heimnetz und Tailscale, die noch offenen TODO-Werte und
die Firewall-Befehle für [Abschnitt 5](#5-windows-firewall). Der **API-Token** wird bei der ersten
Installation in Schritt 6 einmal angezeigt (die Zusammenfassung sagt, ob er neu ist) und steht
immer in `secrets.yaml`. Danach läuft JARVIS schon. Klappt der Start nicht (z. B. Port belegt), steht
„installiert, aber nicht gestartet“ da, mit den letzten Zeilen aus dem Log und einem Hinweis.

**Was er am System ändert:**

| Wo | Was |
|---|---|
| `%LOCALAPPDATA%\JARVIS` | Programm, `.venv`, `config.yaml`, `secrets.yaml`, `state\` und der Installationsmarker `.jarvis-install.json` (anderer Ordner: `Install.cmd -Target D:\Tools\JARVIS`, siehe unten) |
| Startmenü → *JARVIS* | „JARVIS öffnen“, „JARVIS starten“ (mit Rückmeldung, öffnet die Oberfläche), „JARVIS beenden“, „JARVIS deinstallieren“ |
| Autostart-Ordner (`shell:startup`) | `JARVIS.lnk`, nur wenn gewünscht |
| `%USERPROFILE%\.local\bin` | uv (`uv.exe`, `uvx.exe`) – nur nach deiner Zustimmung; PATH und Profile bleiben unverändert. Der uv-Installer legt dazu `%LOCALAPPDATA%\uv\uv-receipt.json` an. |
| `%APPDATA%\uv` | nur mit uv und nur, wenn kein passendes Python da war: das von uv geladene Python (python-build-standalone von GitHub; Ort zeigt `uv python dir`) |
| `%LOCALAPPDATA%\uv\cache` bzw. `%LOCALAPPDATA%\pip\Cache` | Download-Caches von uv bzw. pip (Pakete von pypi.org) |

**Was er nicht anfasst:** keine Adminrechte, keine Firewall-Regel und kein Netzwerkprofil (die Befehle
werden nur angezeigt), keine Aufgabenplanung, keine Registry- oder Systemeinstellungen, kein Router/keine
FRITZ!Box, kein OpenRGB, keine Ollama-Installation (nur lesende Abfrage, nie `ollama pull`). Das
CS2-Skript wird weder geöffnet noch verändert – der Assistent prüft nur, ob die Datei existiert.
Kein Konto, keine Cloud, kein Tracking.

Weitere Optionen (an `Install.cmd` anhängen, z. B. `Install.cmd -NoAutostart`):

| Option | Wirkung |
|---|---|
| `-Target <Ordner>` | anderer Installationsordner – ohne `\` am Ende angeben (`-Target "D:\Tools\JARVIS"`, nicht `"D:\Tools\JARVIS\"`: das `\"` verschluckt sonst die folgenden Optionen) |
| `-Yes` | keine Rückfragen, Vorgaben nehmen (neue `config.yaml` dann mit TODO-Werten; lädt nie uv) |
| `-NoAutostart`, `-NoShortcuts`, `-NoStart` | keinen Autostart / keine Startmenü-Einträge / Server am Ende nicht starten |
| `-InstallUv` / `-NoUv` | uv ohne Rückfrage laden / uv gar nicht verwenden (venv + pip) |

**Anderer Zielordner (`-Target`):** Der Ordner muss leer sein, neu angelegt werden oder schon eine
JARVIS-Installation enthalten – in einen fremden, nicht leeren Ordner installiert der Installer
nicht (gleichnamige Dateien würden sonst überschrieben); dann einen Unterordner nehmen, z. B.
`-Target "D:\Tools\JARVIS"`. Liegt der Ordner außerhalb deines Benutzerprofils, könnten andere Konten
des PCs dort meist mitlesen (auch den Token in `secrets.yaml`) und Programmdateien ändern: Einen neu
angelegten Ordner beschränkt der Installer deshalb auf dich (plus SYSTEM/Administratoren), sonst
wenigstens `secrets.yaml`. Empfohlen bleibt `%LOCALAPPDATA%\JARVIS`.

**Aktualisieren:** das neue Paket entpacken und dessen `Install.cmd` starten – also genauso wie
beim ersten Mal. `config.yaml`, `secrets.yaml` und `state\` werden dabei **nie** überschrieben
oder gelöscht; der Server wird vorher beendet und danach neu gestartet, Dateien der alten Version
werden entfernt. Die Frage „Konfiguration jetzt anpassen?“ startet den Assistenten erneut (Vorgabe:
Nein = nur prüfen). Das geht auch ohne neues Paket mit `Install.cmd` im Installationsordner (fehlt dort
der Installationsmarker oder ist er beschädigt, verweigert der Installer das und bittet, das Paket neu
zu entpacken – sonst wüsste er nicht, welche Dateien zu JARVIS gehören).

**Deinstallieren:** Startmenü → *JARVIS* → „JARVIS deinstallieren“ (oder `Uninstall.cmd` im
Installationsordner). Entfernt Programm, `.venv`, Startmenü-Einträge und Autostart.
`config.yaml`, `secrets.yaml` (Token) und `state\` bleiben, außer du beantwortest die Frage
„Auch Einstellungen und Daten löschen?“ mit Ja oder startest `Uninstall.cmd -Purge`. Später
lässt sich der Rest mit `Uninstall.cmd -Target "%LOCALAPPDATA%\JARVIS" -Purge` aus dem
entpackten Paket löschen – oder einfach den Ordner von Hand. Gelöscht wird nur, was laut
`.jarvis-install.json` vom Installer stammt; eigene Dateien im Ordner bleiben liegen, ebenso
Einstellungen, die schon vor der ersten Installation im Ordner lagen (auch mit `-Purge`). Programme
in Ordnern, die du selbst durch einen Link/eine Junction ersetzt hast, lässt er ebenfalls liegen.
uv, Python und Ollama bleiben installiert. Die Download-Caches räumst du bei Bedarf selbst auf:
`uv cache clean` und `uv python uninstall --all` (oder die Ordner aus der Tabelle oben löschen),
`python -m pip cache purge` bzw. `%LOCALAPPDATA%\pip\Cache` löschen; uv selbst entfernst du, indem du
`uv.exe` und `uvx.exe` in `%USERPROFILE%\.local\bin` und `%LOCALAPPDATA%\uv` löschst.

### Umstieg von der manuellen Einrichtung

Hast du JARVIS früher von Hand im Repo-Ordner eingerichtet (`config.yaml`, `secrets.yaml`,
`scripts\start.ps1`), dann `Install.cmd` **im Repo-Ordner `jarvis`** starten: Der Installer findet die
vorhandenen Einstellungen dort und fragt „Vorhandene Einstellungen … übernehmen?“ (Vorgabe: Ja). Er
kopiert sie nach `%LOCALAPPDATA%\JARVIS` (die Dateien im Repo bleiben, nichts wird überschrieben);
so bleibt auch der Token gleich, den das Handy schon kennt. Läuft die alte Instanz noch auf
Port 8765, zeigt Schritt 9 an, aus welchem Ordner – dort zuerst mit `scripts\stop.ps1` beenden. Ein
Autostart-Eintrag der alten Einrichtung wird auf Nachfrage auf die neue Installation umgestellt.

> Alle weiteren Befehle in dieser Anleitung (`scripts\…`, `.venv\…`) funktionieren im
> Installationsordner `%LOCALAPPDATA%\JARVIS` genauso wie im Repo-Ordner `jarvis` – vorher mit
> `cd "$env:LOCALAPPDATA\JARVIS"` dorthin wechseln. Ausnahme sind die Tests (`pytest`): Die gibt es nur
> im Repo, der Installer kopiert sie nicht.

### Manuell (ohne Installer)

Voraussetzungen: Windows, Python 3.11 oder neuer, [uv](https://docs.astral.sh/uv/), Ollama
(optional). Alles läuft unter dem normalen Benutzerkonto, Adminrechte sind nur für die
Firewall-Regel nötig (siehe unten).

Versionen prüfen (PowerShell):

```powershell
python --version
uv --version
ollama --version
ollama list
```

Im Ordner `jarvis` die virtuelle Umgebung anlegen (installiert die Pakete aus `uv.lock`
nach `.venv`, nichts global):

```powershell
cd <Pfad>\jarvis
uv sync
```

Ohne uv geht es auch mit venv (Paketversionen und Hashes aus `requirements.txt`; `pytest` und
`anyio` nur für die Tests):

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
.venv\Scripts\python.exe -m pip install pytest anyio
```

> PowerShell-Skripte sind für Standardbenutzer oft gesperrt. Ohne Adminrechte startest du
> sie mit `powershell -ExecutionPolicy Bypass -File scripts\<name>.ps1`.

## 2. Konfiguration (`config.yaml`)

Mit dem Installer ist `config.yaml` schon angelegt. Am einfachsten änderst du sie, indem du
`Install.cmd` erneut startest und „Konfiguration jetzt anpassen?“ mit **j** beantwortest – der
Installer beendet JARVIS vorher und startet es danach neu. Den Assistenten kannst du auch direkt
starten (vorhandene Werte sind die Vorgabe, die alte Datei wird als `config.yaml.bak` gesichert):

```powershell
.venv\Scripts\python.exe -m app.setup_wizard configure --config config.yaml
```

> **Änderungen wirken erst nach einem Neustart** von JARVIS (die Datei wird nur beim Start gelesen):
> Startmenü → *JARVIS* → „JARVIS beenden“, dann „JARVIS starten“. Das gilt auch, wenn du
> `config.yaml` mit Notepad änderst. Die Datei als **UTF-8** speichern (nicht „ANSI“).

Von Hand:

```powershell
copy config.example.yaml config.yaml
notepad config.yaml
```

Alle Werte mit `TODO` ersetzen. Fehlt noch etwas, startet der Server trotzdem; das
betroffene Tool meldet „noch nicht eingerichtet“.

| Abschnitt | Was eintragen |
|---|---|
| `server` | Normalerweise nichts ändern: `bind: 0.0.0.0`, `port: 8765`. |
| `llm.model` | Name des Ollama-Modells (siehe [Modell auswählen](#3-modell-auswählen)). `force_fallback: true` schaltet das LLM ab. `keep_alive: 2m` gibt den VRAM zwei Minuten nach der letzten Anfrage wieder frei. |
| `wled.base_url` | `http://<IP des WLED-ESP32>` (in der WLED-App oder der FRITZ!Box nachsehen). |
| `sensors` | Pro Messwert ein Eintrag: `base_url: http://<IP des ESPHome-ESP32>`, `entity_id` = Name der Entität **genau wie in der ESPHome-YAML** (z. B. `BME280 Temperature`). Ältere ESPHome-Firmware erwartet stattdessen die object_id (`bme280_temperature`); JARVIS probiert diese Form automatisch, wenn der Name 404 liefert. |
| `scripts` | Dein CS2-Skript: `cwd` = Ordner, `command` = **Liste** aus Programm und Argumenten (kein Shell-String). |

Beispiele für `command` (Pfade sind Platzhalter):

```yaml
command: ["C:\\Pfad\\zu\\python.exe", "skript.py"]
command: ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "skript.ps1"]
command: ["C:\\Program Files\\AutoHotkey\\v2\\AutoHotkey.exe", "skript.ahk"]
```

`hide_window: true` startet ohne Konsolenfenster und schreibt die Ausgabe nach
`state\logs\<id>.log`; bei `false` bekommt das Skript ein eigenes Fenster.

**Stoppen:** JARVIS beendet erst sanft (`taskkill /PID … /T` ohne `/F`, das schließt
Fenster-Programme wie AutoHotkey sauber) und nach 5 Sekunden hart (den ganzen Prozessbaum).
Reine Konsolenprogramme lassen sich unter Windows meist nur hart beenden – sie werden dann
nach 5 Sekunden beendet.

### ESPHome: REST-Schnittstelle einschalten

JARVIS liest Sensoren über `GET http://<IP>/sensor/<Name>`. Das gibt es nur, wenn in der
ESPHome-YAML der Webserver aktiv ist. Fehlt er, diese Zeilen ergänzen und das Gerät neu
flashen (macht JARVIS bewusst nicht selbst):

```yaml
web_server:
  port: 80
```

Ob es klappt, prüfst du nur lesend mit:

```powershell
.venv\Scripts\python.exe scripts\probe.py --esphome http://<IP> --entity "BME280 Temperature"
.venv\Scripts\python.exe scripts\probe.py          # prüft WLED und alle Sensoren aus config.yaml
```

`probe.py` schickt nur GET-Anfragen und schaltet nichts. Die Sensor-Namen siehst du auch,
wenn du `http://<IP>/` im Browser öffnest.

## 3. Modell auswählen

Gesucht ist das kleinste installierte Modell, das Tools kann:

```powershell
.venv\Scripts\python.exe scripts\pick_model.py
```

Das Skript fragt Ollama nur lesend ab (`/api/tags`, `/api/show`) und zeigt pro Modell die
`capabilities`. Enthält keins `tools`, ein kleines Tool-Modell ziehen (Auswahl:
<https://ollama.com/search?c=tools>), z. B. mit `ollama pull <name>`, und das Skript erneut
starten. Den empfohlenen Namen in `config.yaml` unter `llm.model` eintragen – oder `Install.cmd`
erneut starten, „Konfiguration jetzt anpassen?“ mit j beantworten und das Modell im Assistenten
wählen. Manuell geht es mit `ollama show <name>`: Unter „Capabilities“ muss `tools` stehen.

Bei „Thinking“-Modellen spart `llm.think: false` Zeit.

## 4. Erster Start und Token

**Mit dem Installer** läuft JARVIS schon; Starten und Beenden geht über das Startmenü („JARVIS
starten“ meldet, ob es geklappt hat, und öffnet die Oberfläche). Den API-Token hat der Installer bei
der ersten Installation in Schritt 6 einmal angezeigt; er steht außerdem in
`%LOCALAPPDATA%\JARVIS\secrets.yaml` (Eintrag `api_token`). Neuer Token: `secrets.yaml` löschen und
`Install.cmd` erneut starten.

**Nur ohne Installer** (Repo-Ordner):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Dann erzeugt JARVIS beim ersten Start einen API-Token, speichert ihn in `secrets.yaml` und zeigt ihn
**einmal** in der Konsole an. Neuer Token: `secrets.yaml` löschen und JARVIS neu starten.

Den Token gibst du beim ersten Öffnen der Oberfläche ein (am PC und auf dem Handy je einmal); der
Browser merkt ihn sich (localStorage). Vergessen? Er steht in `secrets.yaml`. Tipp fürs Handy: Den
Token am PC aus `secrets.yaml` kopieren und z. B. über eine Notiz-App oder einen Messenger an dich
selbst schicken, statt die 43 Zeichen abzutippen.

Oberfläche: `http://localhost:8765` am PC, vom Handy `http://<Tailscale-IP des PCs>:8765`.

## 5. Windows-Firewall

Damit das Handy den Server erreicht, braucht es eingehende Regeln. Das geht nur **einmalig mit
Adminrechten** – der Installer zeigt die Befehle nur an (mit dem genauen Python-Pfad deiner
Installation) und führt sie nie selbst aus.

**Admin-PowerShell öffnen** (auch als Standardbenutzer): Start → „PowerShell“ tippen → Rechtsklick
auf „Windows PowerShell“ → **Als Administrator ausführen** → Name und Kennwort eines
Administratorkontos eingeben.

**Vorher prüfen:** `Get-NetConnectionProfile` – das Heim-WLAN/LAN muss „Private“ sein (sonst greift
die LAN-Regel nicht). Steht dort „Public“, in derselben Admin-PowerShell umstellen:
`Set-NetConnectionProfile -InterfaceAlias "<Name aus InterfaceAlias>" -NetworkCategory Private`
(nur fürs eigene Heimnetz!). `Get-NetAdapter` zeigt, wie der Tailscale-Adapter heißt (ggf.
`-InterfaceAlias` unten anpassen).

```powershell
# Heimnetz (Netzwerkprofil "Privat"), nur aus dem eigenen Subnetz
New-NetFirewallRule -DisplayName "JARVIS 8765 (LAN)" -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 8765 -Profile Private -RemoteAddress LocalSubnet

# Tailscale, nur aus dem Tailscale-Adressbereich und nur über den Tailscale-Adapter
New-NetFirewallRule -DisplayName "JARVIS 8765 (Tailscale)" -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 8765 -InterfaceAlias "Tailscale" -RemoteAddress 100.64.0.0/10

# Block-Regeln, die Windows für Python angelegt hat, löschen (siehe unten). Erst ansehen:
# denselben Befehl ohne das "| Remove-NetFirewallRule" am Ende ausführen.
Get-NetFirewallApplicationFilter | Where-Object { $_.Program -like '*\python*.exe' } | Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' } | Remove-NetFirewallRule
```

**Warum der dritte Befehl?** Beim ersten Start fragt Windows oft „Die Windows Defender Firewall hat
einige Features dieser App blockiert – Zugriff zulassen?“ für „Python“. Ohne Adminrechte lässt sich
das nicht erlauben; wer **Abbrechen** klickt (oder gar nicht gefragt wird), bekommt von Windows
eingehende **Block-Regeln** für `python.exe`/`pythonw.exe`. Block-Regeln gehen den Freigaben vor –
das Handy erreicht JARVIS dann trotz der beiden Regeln nicht, und Windows fragt nie wieder. Der
dritte Befehl löscht genau diese Block-Regeln (in der Zusammenfassung des Installers steht er mit
dem Python-Ordner deiner Installation statt `*`). Per Maus geht es auch: `wf.msc` →
*Eingehende Regeln* → Einträge „Python“ mit rotem Symbol → *Löschen*. Wer die Admin-Befehle schon vor
dem ersten Start ausführt, bekommt die Frage meist gar nicht. Mit Adminrechten gilt: im Dialog nur
„Private Netzwerke“ erlauben, nie „Öffentliche“.

## 6. Tailscale auf PC und Handy

1. Tailscale auf dem PC installieren und anmelden (<https://tailscale.com/download>).
2. Tailscale-App auf dem Handy installieren, mit demselben Konto anmelden, VPN einschalten.
3. Die Tailscale-IP des PCs (beginnt mit `100.`) steht in der Tailscale-App oder per
   `tailscale ip -4` am PC.
4. Test vom Handy, **im Mobilfunknetz** (WLAN aus): `http://100.x.y.z:8765` öffnen.

## 7. Autostart

Der Installer fragt danach. Später ein- oder ausschalten: `Install.cmd` erneut starten (fragt wieder)
– oder direkt:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\JARVIS\scripts\install_autostart.ps1"
```

Legt `JARVIS.lnk` im Autostart-Ordner des Benutzers an (`shell:startup`), Ziel
`.venv\Scripts\pythonw.exe -m app` – ohne Konsolenfenster, ohne Adminrechte, ohne
Aufgabenplanung. Ausgaben landen in `state\logs\server.log`. Entfernen mit `-Remove`,
laufenden Server beenden mit Startmenü → *JARVIS* → „JARVIS beenden“ (oder `scripts\stop.ps1`).

## 8. Handy-Launcher `wake.html`

`launcher/wake.html` ist **eine einzelne Datei** für das Handy. Sie startet den
ausgeschalteten PC über die Depicus-Wake-on-LAN-Seite und leitet danach zu JARVIS weiter.

**Einrichten:** Datei aufs Handy kopieren und öffnen. Beim ersten Öffnen erscheinen die
Einstellungen: MAC-Adresse des PCs, MyFRITZ!-Adresse, UDP-Port und JARVIS-Adresse
(`http://<Tailscale-IP>:8765`). Die Werte bleiben nur auf dem Handy (localStorage), in der
Datei steht nichts davon.

**Ablauf:**
1. „PC starten“ öffnet in einem neuen Tab
   `https://www.depicus.com/wake-on-lan/woli?m=<MAC ohne Trennzeichen>&i=<Adresse>&s=255.255.255.255&p=<Port>`.
   Depicus schickt das Magic Packet an deine FRITZ!Box. Dass die FRITZ!Box das Paket an den PC
   weiterreicht, wird vorausgesetzt und hier nicht eingerichtet.
2. Zurück im wake.html-Tab steht „Warte auf PC …“. Alle 3 Sekunden fragt die Seite
   `<JARVIS-Adresse>/api/health` ab (2,5 s Timeout).
3. Antwortet der Server, erscheint „JARVIS öffnen“ und nach 3 Sekunden die Weiterleitung
   („Hier bleiben“ stoppt sie).
4. „JARVIS direkt öffnen“ ist für den Fall, dass der PC schon läuft.

**Ehrliche Einschränkungen:**
- **Tailscale muss auf dem Handy aktiv sein**, sonst ist die 100.x-Adresse nicht erreichbar
  und die Seite wartet bis zum Timeout (5 Minuten).
- **Datei öffnen (file://):** Wie gut das klappt, hängt vom Browser ab. Android-Chrome öffnet
  Dateien aus dem Download-Ordner teils nur über `content://`-Adressen; dort funktioniert
  localStorage nicht überall zuverlässig. Auf iOS kann Safari lokale HTML-Dateien aus der
  Dateien-App nicht direkt öffnen. Klappt es nicht, lässt sich die Datei mit einer
  Datei-Manager-App oder einem Browser öffnen, der lokale Dateien unterstützt (z. B. Firefox
  für Android). Bitte einmal ausprobieren, bevor du dich unterwegs darauf verlässt.
- **Mixed Content:** Die Seite fragt JARVIS per `http://` ab. Liegt `wake.html` auf einer
  `https://`-Webseite, blockiert der Browser diese Abfragen. Deshalb die Datei **lokal**
  öffnen und nicht auf einen HTTPS-Webspace legen.
- **Zugriff auf lokale Netzwerke:** Neuere Chrome-Versionen fragen eventuell nach der
  Erlaubnis, Geräte im lokalen Netzwerk zu erreichen. Dann „Zulassen“ wählen.
- **Hintergrund-Tabs:** Während du auf der Depicus-Seite bist, bremst der Browser die Timer
  des wake.html-Tabs. Beim Zurückwechseln prüft die Seite sofort erneut.
- `mode: "no-cors"` heißt, die Seite sieht den Antwortinhalt nicht. „Online“ bedeutet:
  Der Server hat überhaupt geantwortet.

## 9. Test-Checkliste

**Mit dem Installer:**

- [ ] `Install.cmd` per Doppelklick läuft ohne Fehler durch; Startmenü-Ordner „JARVIS“ mit vier
      Einträgen, „JARVIS öffnen“ zeigt die Oberfläche.
- [ ] Am besten einmal mit einem Benutzernamen/Pfad mit Leerzeichen oder Umlaut (z. B.
      `C:\Users\Max Müller`) – oder `Install.cmd -Target "D:\Test Ordner\JARVIS"`.
- [ ] Token aus Schritt 6 (bzw. aus `secrets.yaml`) in der Oberfläche eingegeben, Statusleiste zeigt
      „Server“ grün.
- [ ] `Install.cmd` erneut (Update): `config.yaml` und `secrets.yaml` unverändert, Server läuft wieder.
- [ ] `Install.cmd` erneut → „Konfiguration jetzt anpassen?“ = j → Wert ändern → wirkt nach dem
      automatischen Neustart.
- [ ] Startmenü „JARVIS beenden“ / „JARVIS starten“ funktionieren („starten“ meldet sich und öffnet
      die Oberfläche).
- [ ] Firewall-Befehle aus [Abschnitt 5](#5-windows-firewall) in der Admin-PowerShell ausgeführt
      (inkl. Aufräumen der Python-Block-Regeln).
- [ ] Handy **im Heim-WLAN** öffnet `http://<LAN-IP des PCs>:8765` (prüft die LAN-Regel).
- [ ] `http://localhost:8765/api/health` liefert `{"status":"ok"}`, `http://localhost:8765/api/tools` ohne Token 401.

**Nur Repo/Entwicklung (ohne Installer):**

- [ ] `uv sync` ohne Fehler, `.venv\Scripts\python.exe -m pytest` grün.
- [ ] `scripts\start.ps1` startet, Token erscheint einmal in der Konsole.

**Geräte und Alltag:**

- [ ] `scripts\probe.py` meldet WLED und Sensoren mit HTTP 200.
- [ ] Klima-Knoten zeigt Temperatur und Luftfeuchte.
- [ ] **Erster echter LED-Test:** „An“, „Aus“, Helligkeit-Slider, eine Farbe.
- [ ] CS2-Skript im Skripte-Panel starten und stoppen; Status stimmt nach JARVIS-Neustart.
- [ ] Chat: „Licht auf 30 % und rot“ – mit und ohne laufendes Ollama.
- [ ] „Herunterfahren“ → Dialog mit Countdown → **Abbrechen** (nichts passiert); dann
      Bestätigen → nach 15 s fährt der PC herunter (vorher „Abbrechen“ im PC-Panel testen).
- [ ] Handy im Mobilfunknetz mit Tailscale: JARVIS erreichbar.
- [ ] **Praxistest:** PC aus, Handy auf mobilen Daten, `wake.html` → „PC starten“ → JARVIS öffnet sich.
- [ ] Autostart einrichten, abmelden/anmelden, JARVIS läuft ohne Fenster.
- [ ] Zum Schluss (optional): „JARVIS deinstallieren“ – Startmenü und Autostart weg,
      `config.yaml`/`secrets.yaml` bleiben; danach `Install.cmd` übernimmt sie wieder.
- [ ] Optional: `Uninstall.cmd -Target "%LOCALAPPDATA%\JARVIS" -Purge` aus dem entpackten Paket –
      der Ordner ist danach weg (eigene Dateien darin bleiben).

## Bedienung

Chat-Beispiele (funktionieren auch ohne LLM):

| Satz | Wirkung |
|---|---|
| „Licht an“ / „Licht aus“ | LED an/aus |
| „Helligkeit 40“, „Licht auf 20 %“ | Helligkeit |
| „Farbe rot“, „mach das Licht warmweiß“ | Farbe (rot, grün, blau, weiß, warmweiß, lila, orange, pink, gelb, #RRGGBB) |
| „Licht an, Helligkeit 40 und Farbe blau“ | alles nacheinander |
| „Effekt Rainbow“, „Preset Abend“ | WLED-Effekt/-Preset per Name oder Nummer |
| „Wie warm ist es?“ | Sensoren lesen |
| „Starte CS2“, „Stoppe CS2“, „Welche Skripte laufen?“ | Skripte |
| „PC ausschalten“ | Bestätigungsdialog (der PC fährt erst nach „Bestätigen“ herunter) |
| „Herunterfahren abbrechen“ | `shutdown /a` |

## Sicherheit

- Nur LAN und Tailscale (IP-Filter), alles unter `/api/` außer `/api/health` nur mit Token
  (`Authorization: Bearer …`). Nach 5 falschen Versuchen ist die IP 60 s gesperrt.
- Das Sprachmodell hat nur die Tools aus der Registry: keine Shell, kein Dateizugriff.
  Erfundene Tool-Namen werden abgelehnt.
- Herunterfahren passiert nur über `POST /api/confirm/<id>` – das ruft ausschließlich der
  Bestätigen-Button auf, nie das Modell. Eine Bestätigung gilt 30 s und nur einmal.
- Skripte: nur die aus `config.yaml`, Start als Argumentliste ohne Shell.
- Keine Cloud-Dienste, kein Tracking. `config.yaml`, `secrets.yaml` und `state/` stehen in
  `.gitignore`.

## Entwicklung und Tests

```powershell
.venv\Scripts\python.exe -m pytest
```

Die Tests mocken alle Geräte und Ollama, starten nie `shutdown` und schicken nichts an echte
Geräte. Der Browser-Test für `wake.html` (`tests/web/wake.test.mjs`) läuft nur, wenn Node und
Playwright installiert sind, sonst wird er übersprungen.

Die Installer-Tests (`tests/test_installer_ps.py` mit `tests/ps/*.tests.ps1`) brauchen PowerShell 7
(`pwsh` im PATH oder Umgebungsvariable `JARVIS_PWSH`) und laufen im Testmodus `-AllowNonWindows`
auch unter Linux/macOS. `tests/test_installer_e2e.py` baut das ZIP, installiert daraus mit echtem
Server, aktualisiert und deinstalliert wieder (braucht zusätzlich uv, dauert etwas). Langsame Tests
abwählen: `pytest -m "not slow"`.

**Release-Paket bauen:**

```powershell
.venv\Scripts\python.exe scripts\build_installer.py            # -> dist\JARVIS-Setup-<version>.zip
.venv\Scripts\python.exe scripts\build_installer.py --out C:\Temp
```

Die Version kommt aus `pyproject.toml`. Das ZIP enthält einen Ordner `JARVIS-Setup-<version>\` mit
`Install.cmd`, `Uninstall.cmd`, `README.md`, `config.example.yaml`, `pyproject.toml`, `uv.lock`,
`requirements.txt`, `app\`, `web\`, `launcher\` und `scripts\` – nie `tests\`, `.venv`, `state\`,
`config.yaml` oder `secrets.yaml` (liegt so etwas in einem der Ordner – auch Kopien wie
`config.yaml.orig`, `*.bak`/`*.orig`/`*.old` oder eine Datei mit einem echt aussehenden `api_token`,
auch als JSON –, bricht der Build ab). `.ps1`/`.cmd` bekommen CRLF-Zeilenenden, alle anderen
Textdateien LF; der Build ist reproduzierbar (gleiche Quelle = gleiche SHA-256, steht in
`<zip>.sha256`, unabhängig von `core.autocrlf`). Nach Änderungen an den Abhängigkeiten
`requirements.txt` neu erzeugen:

```powershell
uv lock
uv export --frozen --no-dev --format requirements-txt -o requirements.txt
```

`.ps1`- und `.cmd`-Dateien müssen reines ASCII sein (Windows PowerShell 5.1 liest UTF-8 ohne BOM
als ANSI) und unter PowerShell 5.1 laufen – ein Test prüft beides grob.

## Fehlerbehebung

| Meldung | Lösung |
|---|---|
| „Datei öffnen – Sicherheitswarnung“ / SmartScreen beim Doppelklick auf `Install.cmd` | *Ausführen* bzw. *Weitere Informationen* → *Trotzdem ausführen* (unbedenklich, siehe [Installation](#1-installation)); oder künftig das ZIP vor dem Entpacken freigeben (Eigenschaften → Zulassen) |
| Installer: „Dieses Fenster läuft mit Administratorrechten“ | `Install.cmd` normal per Doppelklick starten, nicht „Als Administrator ausführen“ |
| Installer: „Im Quellordner … fehlen Programmdateien … OneDrive“ | ZIP z. B. nach `C:\JARVIS-Setup` entpacken (nicht in einen OneDrive-Ordner) und `Install.cmd` dort starten |
| Installer: „Der Zielordner … ist nicht leer und enthält keine JARVIS-Installation“ | einen leeren oder neuen Unterordner angeben, z. B. `-Target "D:\Tools\JARVIS"` |
| Installer: „Der Pfad bei -Target endet vermutlich mit \"“ | `-Target` ohne abschließenden Backslash angeben: `-Target "D:\JARVIS"` |
| Installer: „… aus dem Installationsordner gestartet, aber der Installationsmarker fehlt oder ist beschädigt“ | Installationspaket neu entpacken und dessen `Install.cmd` starten (Einstellungen bleiben erhalten) |
| Installer: „Weder uv noch Python 3.11 … gefunden“ | dem uv-Download zustimmen (am einfachsten) oder Python 3.11+ von python.org ohne Adminrechte installieren (Haken bei „Use admin privileges when installing py.exe“ entfernen), dann `Install.cmd` erneut |
| Installer: „Python aus dem Microsoft Store wird nicht verwendet“ / „.venv … wurde nicht angelegt“ | dem uv-Download zustimmen oder Python von python.org bzw. den „Python install manager“ nehmen |
| Installer: pip-Fehler beim Einrichten der `.venv` | Internetverbindung zu pypi.org prüfen; oder dem uv-Download zustimmen (uv bringt ein passendes Python mit) |
| Installer: „installiert, aber nicht gestartet“ / „Port 8765 ist belegt“ | anderes Programm (oder eine alte JARVIS-Instanz, der Installer nennt deren Ordner) beenden – oder `Install.cmd` erneut, „Konfiguration jetzt anpassen?“ = j, anderen Port wählen (Firewall-Regeln mit dem neuen Port anlegen) |
| „Abgebrochen (Strg+C)“, danach „Batchvorgang abbrechen (J/N)?“ | mit J bestätigen und `Install.cmd` bzw. `Uninstall.cmd` einfach erneut starten – Einstellungen bleiben erhalten |
| „JARVIS starten“ (Startmenü) meldet „JARVIS antwortet nicht“ | die angezeigten Log-Zeilen bzw. den Hinweis darunter lesen (Port belegt, `config.yaml` fehlerhaft …); Log: `state\logs\server.log` |
| Deinstallation: „keinen Installationsmarker“ | Ordner ist keine Installer-Installation – dann von Hand aufräumen |
| „Konfigurationsdatei nicht gefunden“ | `Install.cmd` erneut starten (oder von Hand: `copy config.example.yaml config.yaml`) |
| „config.yaml ist nicht UTF-8-kodiert“ | Datei im Editor mit Codierung **UTF-8** speichern (nicht „ANSI“) |
| „config.yaml ist ungültig: … unbekannter Schlüssel“ | Tippfehler im Schlüssel, siehe `config.example.yaml` |
| Änderung in `config.yaml` wirkt nicht | JARVIS neu starten (Startmenü: beenden, dann starten) |
| „… keine gültige IP-Adresse“ / „Ungültige … Adresse“ | Tippfehler in der Adresse (vier Zahlen von 0 bis 255, ohne führende Nullen, z. B. `192.168.0.50`) |
| „… noch nicht eingerichtet“ | TODO-Wert in `config.yaml` ausfüllen |
| Statusleiste „LLM offline“ | Ollama läuft nicht – Buttons und Regel-Parser gehen trotzdem |
| „Modell … ist nicht installiert“ | `ollama pull <name>` oder anderen Namen eintragen |
| „Modell … unterstützt keine Tools“ | `scripts\pick_model.py` ausführen |
| Sensor „404“ | Name in `entity_id` prüfen, `web_server:` in ESPHome ergänzen |
| Handy erreicht nichts | Tailscale aktiv? Firewall-Regeln gesetzt ([Abschnitt 5](#5-windows-firewall))? `http://` statt `https://`? |
| Handy erreicht nichts, **obwohl** die Firewall-Regeln gesetzt sind | Block-Regeln für Python löschen (dritter Befehl in [Abschnitt 5](#5-windows-firewall)); Heimnetz auf „Privat“ stellen (`Get-NetConnectionProfile`) |
| HTTP 403 | Anfrage kommt nicht aus LAN/Tailscale |
| HTTP 429 | 5 falsche Tokens – 60 s warten |
