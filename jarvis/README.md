# JARVIS – lokaler Assistent für PC, LED-Strip, Zimmersensoren und Skripte

Ein kleiner Webserver auf dem Windows-PC mit einer JARVIS-Oberfläche fürs Handy:
Schnellaktionen (Licht, Helligkeit, Farbe, Temperatur, Skripte, PC herunterfahren) und ein
Chat-Feld. Ein kleines **lokales** Ollama-Modell übersetzt Sätze wie „Licht auf 40 % und
blau“ in Tool-Aufrufe. Ohne Ollama übernimmt ein Regel-Parser die Kernbefehle; die Buttons
brauchen das Modell ohnehin nicht.

> [!CAUTION]
> **Niemals Port 8765 in der FRITZ!Box (oder einem anderen Router) freigeben.**
> JARVIS ist nur fürs Heimnetz und für Tailscale gedacht. Von unterwegs kommst du über
> Tailscale (VPN) an den Server, nicht über eine Portfreigabe. Zusätzlich lehnt der Server
> jede Anfrage ab, die nicht aus 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 oder
> 100.64.0.0/10 (Tailscale) kommt.

## Inhalt

1. [Was wo liegt](#was-wo-liegt)
2. [Installation](#1-installation)
3. [Konfiguration](#2-konfiguration-configyaml)
4. [Modell auswählen](#3-modell-auswählen)
5. [Erster Start und Token](#4-erster-start-und-token)
6. [Firewall](#5-windows-firewall)
7. [Tailscale](#6-tailscale-auf-pc-und-handy)
8. [Autostart](#7-autostart)
9. [Handy-Launcher wake.html](#8-handy-launcher-wakehtml)
10. [Test-Checkliste](#9-test-checkliste)
11. [Bedienung](#bedienung)
12. [Sicherheit](#sicherheit)
13. [Entwicklung und Tests](#entwicklung-und-tests)
14. [Fehlerbehebung](#fehlerbehebung)

## Was wo liegt

| Pfad | Zweck |
|---|---|
| `app/main.py` | FastAPI-App: Routen `/`, `/api/health`, `/api/status`, `/api/tools`, `/api/tools/{name}`, `/api/confirm/{id}`, `/api/chat` |
| `app/__main__.py` | `python -m app` – startet mit Host/Port aus `config.yaml` |
| `app/config.py` | Laden und Prüfen von `config.yaml` |
| `app/auth.py` | API-Token, Sperre nach Fehlversuchen |
| `app/netguard.py` | IP-Filter (nur LAN/Tailscale) |
| `app/tools/` | Tools: `pc.py`, `led.py`, `sensors.py`, `scripts.py`, `registry.py` |
| `app/llm.py` | Ollama-Agent (`/api/chat` mit Tools) |
| `app/fallback.py` | Regel-Parser ohne LLM |
| `web/index.html` | JARVIS-Oberfläche (eine Datei, keine externen Ressourcen) |
| `launcher/wake.html` | Handy-Seite: PC per Wake-on-LAN starten, dann zu JARVIS |
| `scripts/` | `start.ps1`, `install_autostart.ps1`, `stop.ps1`, `probe.py`, `pick_model.py` |
| `config.example.yaml` | Vorlage – die echte `config.yaml` und `secrets.yaml` werden nie committet |

## 1. Installation

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

Ohne uv geht es auch mit venv:

```powershell
python -m venv .venv
.venv\Scripts\pip install fastapi uvicorn httpx pydantic pyyaml psutil pytest anyio
```

> PowerShell-Skripte sind für Standardbenutzer oft gesperrt. Ohne Adminrechte startest du
> sie mit `powershell -ExecutionPolicy Bypass -File scripts\<name>.ps1`.

## 2. Konfiguration (`config.yaml`)

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
starten. Den empfohlenen Namen in `config.yaml` unter `llm.model` eintragen. Manuell geht es
mit `ollama show <name>`: Unter „Capabilities“ muss `tools` stehen.

Bei „Thinking“-Modellen spart `llm.think: false` Zeit.

## 4. Erster Start und Token

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Beim ersten Start erzeugt JARVIS einen API-Token, speichert ihn in `secrets.yaml` und zeigt
ihn **einmal** in der Konsole an. Diesen Token gibst du beim ersten Öffnen der Oberfläche
ein; das Handy merkt ihn sich (localStorage). Vergessen? Er steht in `secrets.yaml`. Neuer
Token: `secrets.yaml` löschen und JARVIS neu starten.

Oberfläche: `http://localhost:8765` am PC, vom Handy `http://<Tailscale-IP des PCs>:8765`.

## 5. Windows-Firewall

Damit das Handy den Server erreicht, braucht es eine eingehende Regel. Das geht nur
**einmalig mit Adminrechten**. In einer Admin-PowerShell:

```powershell
# Heimnetz (Netzwerkprofil "Privat"), nur aus dem eigenen Subnetz
New-NetFirewallRule -DisplayName "JARVIS 8765 (LAN)" -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 8765 -Profile Private -RemoteAddress LocalSubnet

# Tailscale, nur aus dem Tailscale-Adressbereich und nur über den Tailscale-Adapter
New-NetFirewallRule -DisplayName "JARVIS 8765 (Tailscale)" -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 8765 -InterfaceAlias "Tailscale" -RemoteAddress 100.64.0.0/10
```

Vorher prüfen: `Get-NetConnectionProfile` (das Heim-WLAN/LAN muss „Private“ sein) und
`Get-NetAdapter` (wie der Tailscale-Adapter heißt; ggf. `-InterfaceAlias` anpassen). Fragt
Windows beim ersten Start „Zugriff zulassen?“, nur „Private Netzwerke“ erlauben, nie
„Öffentliche“.

## 6. Tailscale auf PC und Handy

1. Tailscale auf dem PC installieren und anmelden (<https://tailscale.com/download>).
2. Tailscale-App auf dem Handy installieren, mit demselben Konto anmelden, VPN einschalten.
3. Die Tailscale-IP des PCs (beginnt mit `100.`) steht in der Tailscale-App oder per
   `tailscale ip -4` am PC.
4. Test vom Handy, **im Mobilfunknetz** (WLAN aus): `http://100.x.y.z:8765` öffnen.

## 7. Autostart

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
```

Legt `JARVIS.lnk` im Autostart-Ordner des Benutzers an (`shell:startup`), Ziel
`.venv\Scripts\pythonw.exe -m app` – ohne Konsolenfenster, ohne Adminrechte, ohne
Aufgabenplanung. Ausgaben landen in `state\logs\server.log`. Entfernen mit `-Remove`,
laufenden Server beenden mit `scripts\stop.ps1`.

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

- [ ] `uv sync` ohne Fehler, `.venv\Scripts\python.exe -m pytest` grün.
- [ ] `scripts\start.ps1` startet, Token erscheint einmal in der Konsole.
- [ ] `http://localhost:8765/api/health` liefert `{"status":"ok"}`, `http://localhost:8765/api/tools` ohne Token 401.
- [ ] Oberfläche am PC öffnen, Token eingeben, Statusleiste zeigt „Server“ grün.
- [ ] `scripts\probe.py` meldet WLED und Sensoren mit HTTP 200.
- [ ] Klima-Kachel zeigt Temperatur und Luftfeuchte.
- [ ] **Erster echter LED-Test:** „An“, „Aus“, Helligkeit-Slider, eine Farbe.
- [ ] CS2-Skript über die Kachel starten und stoppen; Status stimmt nach JARVIS-Neustart.
- [ ] Chat: „Licht auf 30 % und rot“ – mit und ohne laufendes Ollama.
- [ ] „Herunterfahren“ → Dialog mit Countdown → **Abbrechen** (nichts passiert); dann
      Bestätigen → nach 15 s fährt der PC herunter (vorher „Abbrechen“ in der PC-Kachel testen).
- [ ] Handy im Mobilfunknetz mit Tailscale: JARVIS erreichbar.
- [ ] **Praxistest:** PC aus, Handy auf mobilen Daten, `wake.html` → „PC starten“ → JARVIS öffnet sich.
- [ ] Autostart einrichten, abmelden/anmelden, JARVIS läuft ohne Fenster.

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

## Fehlerbehebung

| Meldung | Lösung |
|---|---|
| „Konfigurationsdatei nicht gefunden“ | `copy config.example.yaml config.yaml` |
| „config.yaml ist ungültig: … unbekannter Schlüssel“ | Tippfehler im Schlüssel, siehe `config.example.yaml` |
| „… noch nicht eingerichtet“ | TODO-Wert in `config.yaml` ausfüllen |
| Statusleiste „LLM offline“ | Ollama läuft nicht – Buttons und Regel-Parser gehen trotzdem |
| „Modell … ist nicht installiert“ | `ollama pull <name>` oder anderen Namen eintragen |
| „Modell … unterstützt keine Tools“ | `scripts\pick_model.py` ausführen |
| Sensor „404“ | Name in `entity_id` prüfen, `web_server:` in ESPHome ergänzen |
| Handy erreicht nichts | Tailscale aktiv? Firewall-Regel? `http://` statt `https://`? |
| HTTP 403 | Anfrage kommt nicht aus LAN/Tailscale |
| HTTP 429 | 5 falsche Tokens – 60 s warten |
