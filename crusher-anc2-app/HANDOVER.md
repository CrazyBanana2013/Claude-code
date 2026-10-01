# Handover – crusher-anc2-app (ANC-Optimierung Skullcandy Crusher ANC 2)
Letztes Update: 2026-10-01 15:55 UTC

## Ziel
Python-CLI-Prototyp, der
(a) prüft, ob sich ANC-Modus/-Stufe der Skullcandy Crusher ANC 2 per Bluetooth lesen bzw. steuern lässt (nur mit belegten Befehlen, nichts erfunden), und
(b) die ANC-Wirkung als Dämpfung pro Oktavband (63 Hz – 8 kHz) messbar macht (Mikrofon vor der Ohrmuschel, Vergleich ANC an/aus bzw. je Einstellung), um die beste Einstellung für eine Umgebung zu finden.
Die Messung ist eine **Näherung, kein Laborwert**. Das steht so auch in Tool-Ausgabe, JSON und Docs. Eine GUI folgt erst in einem späteren Paket.

## Umgebung
- **Zielsystem (laut Auftrag):** Windows 10/11, Python 3.11+. **Noch nicht geprüft.** Auf dem Ziel-PC `ver` und `python --version` ausführen und hier eintragen:
  - Windows-Version: _offen_
  - Python-Version: _offen_
- **Tatsächliche Entwicklungsumgebung bisher:** Linux-Cloud-Container (Ubuntu 24.04.4 LTS), Python 3.11.15, **ohne Bluetooth-Adapter und ohne Audiogerät/PortAudio**. Deshalb liefen BLE- und Mikrofon-Teile hier nur bis zur (sauber abgefangenen) Fehlermeldung.
- Paketmanager: pip + venv (`.venv/` im Projektordner, nicht versioniert).
- Installierte Versionen (Container, 2026-10-01): bleak 3.0.2, dbus-fast 5.0.24 (nur Linux), sounddevice 0.5.6, numpy 2.4.6, scipy 1.17.1, matplotlib 3.11.2. Mindestversionen in `requirements.txt`.
- Einrichtung auf Windows (PowerShell, im Ordner `crusher-anc2-app`):
  ```
  python -m venv .venv
  .venv\Scripts\activate
  pip install -r requirements.txt
  ```
  `sounddevice` bringt unter Windows PortAudio mit, es ist kein extra Treiber nötig. Es wurden keine systemweiten Treiber installiert.
- Befehle:
  - `python scan.py [--seconds 20] [--name Crusher]`: BLE-Scan
  - `python explore.py --address <ADR>`: GATT-Dump nach `docs/gatt-dump.txt`
  - `python watch.py --address <ADR> --seconds 120`: Notifications loggen nach `docs/notify-log.txt` (Text + Enter = Marke im Log)
  - `python measure.py --list-devices`: Mikrofone anzeigen
  - `python measure.py --label anc_aus [--device N] [--seconds 10] [--wav datei.wav]`
  - `python compare.py anc_aus anc_an [weitere...]`: Tabelle + `docs/vergleich.png`
  - `python selftest.py`: Test der Auswertung ohne Hardware (synthetische Signale)

## Projektstruktur
- `HANDOVER.md`: diese Datei, einzige Quelle der Wahrheit für den Projektstand
- `requirements.txt`: Python-Abhängigkeiten
- `.gitignore`: schließt `.venv/`, Caches und Selbsttest-Ergebnisse aus
- `ble_common.py`: gemeinsame BLE-Helfer (Gerät per Adresse/Namensteil finden, Bytes als Hex/Text formatieren)
- `scan.py`: Schritt 2, 10-s-BLE-Scan (Name, Adresse, RSSI), mit Hinweisen, falls die Kopfhörer fehlen
- `explore.py`: Schritt 3, listet Services/Characteristics/Properties/Descriptors, liest nur „read“-Werte und schreibt nichts
- `watch.py`: Schritt 4, abonniert alle notify/indicate-Characteristics und loggt mit Zeitstempel und Markierung NEU/GEÄNDERT/gleich
- `measure.py`: Schritt 6, Aufnahme 48 kHz mono, Welch-PSD, Oktavbänder, `data/<label>.json`
- `compare.py`: Schritt 7, Dämpfung = Pegel(Referenz) − Pegel(Messung) pro Band, Tabelle, beste/schlechteste Bänder, Diagramm
- `selftest.py`: prüft measure/compare mit synthetischen Signalen (Sinus mit bekanntem Pegel, Rauschen −10 dB)
- `data/`: Messergebnisse (`*.json`); `selftest_*.json` wird ignoriert
- `docs/gatt-dump.txt`: GATT-Dump; aktuell nur eine begründete Notiz, warum noch kein Dump existiert
- `docs/machbarkeit.md`: Fazit (sicher/eventuell/nicht machbar) und Empfehlung
- `docs/notify-log.txt`: entsteht beim ersten Lauf von `watch.py` (existiert noch nicht)
- `docs/vergleich.png`: entsteht durch `compare.py` (existiert noch nicht, da keine echten Messungen vorliegen)

## Architektur & Entscheidungen
- **Kein eigenes Git-Repo in `crusher-anc2-app/`:** Der Ordner liegt im bestehenden Repository `CrazyBanana2013/Claude-code` auf dem eigenen Branch `ccr-107e7d60-vrx363`. Ein verschachteltes Repo hätte sich nicht zusammen mit diesem Repo pushen lassen. Bei Bedarf kann der Ordner später mit `git subtree split` in ein eigenes Repo ausgelagert werden.
- **Keine erfundenen UUIDs/Befehle:** Alle UUIDs/Handles werden zur Laufzeit vom Gerät abgefragt. Die einzige Annahme ist der Namens-Suchbegriff `"Crusher"` (`ble_common.DEFAULT_NAME_HINT`), im Code als ANNAHME markiert und per `--name`/`--address` überschreibbar.
- **Einziger Schreibzugriff:** `watch.py` → `start_notify` schreibt den standardisierten CCCD (0x2902), um Notifications einzuschalten. Das ist im Bluetooth-Standard so vorgesehen und kein Herstellerbefehl. Sonst wird nirgends geschrieben.
- **Messung:** Welch mit Hann-Fenster, `nperseg=16384` (≈2,9 Hz Auflösung bei 48 kHz, damit das 63-Hz-Band genug Stützstellen hat). Bandleistung = Summe PSD × Δf zwischen fc/√2 und fc·√2. Einheit: dBFS (unkalibriert). Nennmittenfrequenzen nach IEC 61260.
- **Referenz in compare.py ist immer die erste Messung**, positive Dämpfung = leiser als Referenz.
- **`measure.py --wav`:** analysiert eine WAV-Datei statt aufzunehmen. Damit ist die Auswertung ohne Mikrofon testbar, und Aufnahmen aus anderen Programmen lassen sich verwenden. `sounddevice` wird nur bei echter Aufnahme importiert.
- **matplotlib mit Backend „Agg“:** rendert ohne Fenster, das Diagramm geht nur in die Datei.
- **Verworfen:** eigenes Anti-Noise-Signal (Latenz zu hoch, laut Auftrag ausgeschlossen); Headset-Mikro der Crusher als Messmikro (Windows wechselt auf HFP, das verfälscht die Messung).

## Erledigt
- [x] Schritt 1: Projektordner, venv, Pakete. Dateien: `requirements.txt`, `.gitignore`, `HANDOVER.md`. Verifiziert: `pip install` erfolgreich, Versionen siehe „Umgebung“. **Windows-/Python-Version am Ziel-PC noch nicht eingetragen.**
- [x] Schritt 2 (Code): `scan.py`. Verifiziert nur bis zur Fehlerbehandlung: im Container `FEHLER: BLE-Scan nicht möglich: FileNotFoundError: [Errno 2] No such file or directory` (kein Bluetooth), Exit-Code 1. **Test mit Kopfhörern offen.**
- [x] Schritt 3 (Code): `explore.py`. Im Container: `FEHLER: BLE nicht verfügbar: FileNotFoundError ...`. `docs/gatt-dump.txt` enthält eine begründete Notiz. **Echter Dump offen.**
- [x] Schritt 4 (Code): `watch.py`. Im Container nur die Fehlerbehandlung geprüft. **Beobachtung mit ANC-Umschaltung offen.**
- [x] Schritt 5: bewusst übersprungen, Begründung in `docs/machbarkeit.md` (kein beobachtetes Muster, kein öffentliches Reverse Engineering gefunden).
- [x] Schritt 6: `measure.py`. Verifiziert per `python selftest.py`: Sinus 1 kHz mit Amplitude 0,5 → −9,03 dBFS im 1-kHz-Band (theoretisch −9,03). Mikrofonaufnahme im Container nicht möglich (`Audiofehler: PortAudio library not found`). **`python measure.py --label test` auf Windows offen.**
- [x] Schritt 7: `compare.py`. Verifiziert per `selftest.py`: Rauschen vs. dasselbe Rauschen −10 dB → in allen 8 Bändern +10,0 dB, Diagramm wird erzeugt.
- [x] Schritt 8: `docs/machbarkeit.md` (vorläufig, Stand ohne Hardwaretest).
- [x] Websuche nach Reverse Engineering: nichts gefunden (Details und Quellen in `docs/machbarkeit.md`).

## In Arbeit
Nichts. Der Code ist fertig. Blockiert, bis die Skripte auf dem Windows-PC mit Kopfhörern und Mikrofon laufen.

## Nächste Schritte
1. Auf dem Windows-PC: `ver` und `python --version` ausführen und unter „Umgebung“ eintragen. venv wie oben einrichten. `python selftest.py` muss „Alle Tests bestanden.“ ausgeben.
2. Kopfhörer einschalten, **vom Handy trennen** (Bluetooth am Handy aus). `python scan.py`. Erscheint kein Gerät mit „Crusher“ im Namen: Pairing-Modus laut Skullcandy-Anleitung aktivieren, `python scan.py --seconds 20`, Liste nach unbekannten Namen durchsehen. Ergebnis (Name, Adresse) hier eintragen.
3. `python explore.py --address <ADR>`, danach `docs/gatt-dump.txt` prüfen: Gibt es Services außerhalb der Standard-UUIDs (`0000xxxx-0000-1000-8000-00805f9b34fb`)? Liste hier eintragen.
4. `python watch.py --address <ADR> --seconds 120`, dabei ANC am Gerät mehrfach umschalten (z. B. ANC an → Transparenz → aus → ANC an) und **vor jedem Umschalten** eine Marke eingeben (z. B. `ANC an` + Enter). Danach `docs/notify-log.txt` auswerten: Ändert sich ein Wert reproduzierbar mit dem Modus? Ergebnis hier und in `docs/machbarkeit.md` festhalten.
5. Nur bei eindeutigem Muster: `anc_control.py` bauen, **nur lesend**. Schreiben erst, wenn der Befehl zweifelsfrei belegt ist, und erst nach ausdrücklicher Rückfrage beim User.
6. Kein BLE-Muster: klassisches Bluetooth (RFCOMM/SPP) prüfen, z. B. SDP-Dienste des Geräts auflisten. Unter Windows gibt es dafür kein Standard-Python-Paket; die Optionen vor einer Treiber-/Paketinstallation hier notieren.
7. Messreihe: Mikrofon (PC-/USB-Mikro, **nicht** das der Crusher) direkt vor die Ohrmuschel bzw. ins Ohrpolster legen, Kopfhörer auf (Kunst-)Kopf oder fest aufliegend, gleichmäßiges Geräusch. Windows-Mikrofonverbesserungen aus. Dann `python measure.py --label test` (Plausibilität: keine Warnung, Werte etwa −90…−20 dBFS), danach `python measure.py --label anc_aus`, ANC am Gerät einschalten, `python measure.py --label anc_an`, dann `python compare.py anc_aus anc_an`. Jede Einstellung 2–3× messen (`anc_an_1`, `anc_an_2` …).
8. Danach nächstes Paket: einfache GUI (Tkinter) mit Knöpfen „Referenz aufnehmen“ / „Einstellung aufnehmen“ und Live-Balkendiagramm, siehe Empfehlung in `docs/machbarkeit.md`.

## Offene Fragen / Blocker
- **Blocker (Hardware):** Alle Bluetooth- und Mikrofon-Tests brauchen den Windows-PC mit Kopfhörern. Fehlermeldungen in der Entwicklungsumgebung:
  - BLE: `FileNotFoundError: [Errno 2] No such file or directory` (kein BlueZ/D-Bus im Container)
  - Audio: `OSError: PortAudio library not found`
- **Frage an den User:** Welche ANC-Modi bietet das Gerät per Taste genau (z. B. ANC / Stay-Aware / aus) und gibt es in der App Stufen? Die Antwort ist nötig, um `watch.py`-Logs zuzuordnen.
- **Frage an den User:** Soll der Ordner später ein eigenes Git-Repository werden (siehe Entscheidung oben)?

## Konventionen
- Sprache: Code-Kommentare, Ausgaben und Docs auf Deutsch. Bezeichner im Code englisch, JSON-Schlüssel deutsch (`oktavbaender_db`, `warnungen`, …).
- Ein Skript pro Schritt, flach im Projektroot, keine Klassen/Abstraktionen außer `ble_common.py` für doppelt genutzte BLE-Helfer.
- Messdateien: `data/<label>.json`, Label in Kleinbuchstaben mit Unterstrich (`anc_aus`, `anc_an`, `transparenz`, `anc_an_2`).
- Annahmen im Code mit `ANNAHME:` kennzeichnen.
- Commits: kurze Betreffzeile im Imperativ auf Deutsch, Präfix `crusher-anc2:`, ein Commit pro abgeschlossenem Schritt.
- Branch: `ccr-107e7d60-vrx363`.
