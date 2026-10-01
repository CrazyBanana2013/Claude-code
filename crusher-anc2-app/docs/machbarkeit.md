# Machbarkeit – ANC-Optimierung Skullcandy Crusher ANC 2

Stand: 2026-10-01. Vorläufige Bewertung, da noch kein Test mit echter Hardware gelaufen ist (siehe „Grundlage“).

## Grundlage dieser Bewertung

- Der Code wurde in einem Linux-Cloud-Container ohne Bluetooth-Adapter und ohne Audiogerät entwickelt.
  - `python scan.py` → `FEHLER: BLE-Scan nicht möglich: FileNotFoundError: [Errno 2] No such file or directory` (kein BlueZ/D-Bus).
  - `python measure.py --label test` → `Audiofehler: PortAudio library not found`.
- Die Auswertung (Welch-Spektrum, Oktavbänder, Vergleich, Diagramm) wurde mit synthetischen Signalen geprüft (`python selftest.py`, alle Tests bestanden).
- Websuche (2026-10-01) nach Reverse Engineering des Skullcandy-Steuerprotokolls: **nichts Öffentliches gefunden.** Gesucht wurde nach „Skullcandy Crusher ANC 2 reverse engineering bluetooth protocol GATT“ und „Skullcandy app protocol reverse engineered github ANC mode“. Gefunden wurden nur Produktseiten und Support-Artikel, z. B.:
  - https://www.skullcandy.com/products/crusher-anc-2-sensory-bass-headphones-with-active-noise-canceling
  - https://support.skullcandy.com/hc/en-us/articles/14125077268631-Crusher-ANC-2 (konnte wegen Netzwerkbeschränkung nicht abgerufen werden)
  - https://support.skullcandy.com/hc/en-us/articles/10600491148695-Skull-iQ-App-FAQs
  - Vergleichbare Projekte für andere Hersteller existieren (z. B. https://github.com/ici0/sony-device-center für Sony). Daraus lässt sich aber **nichts** auf Skullcandy übertragen.
- Laut Produktseiten wird die Crusher ANC 2 über die Skullcandy-App (Skull-iQ) gesteuert. Ein Steuerkanal existiert also, ist aber undokumentiert.

## Was sicher geht: Messung

- `measure.py` und `compare.py` sind fertig und rechnerisch geprüft.
- Damit lässt sich die Wirkung jeder Einstellung (ANC an/aus, Transparenz, falls vorhanden) **relativ** vergleichen, pro Oktavband 63 Hz bis 8 kHz.
- **Einschränkung (gilt immer):** Das ist eine Näherung, kein Laborwert. Das Mikrofon liegt vor bzw. in der Ohrmuschel statt in einem Ohrsimulator, die Werte sind unkalibriert (dBFS), und das Umgebungsgeräusch schwankt zwischen den Aufnahmen. Unterschiede unter etwa 2–3 dB sind nicht belastbar.
- Praxistipps:
  - Nicht das Headset-Mikrofon der Crusher selbst verwenden. Unter Windows schaltet das auf das Freisprechprofil (HFP) um, und die Messung beeinflusst sich selbst.
  - Windows-Mikrofonverbesserungen und Rauschunterdrückung abschalten, sonst „misst“ man den Windows-Filter.
  - Möglichst gleichmäßiges Geräusch verwenden (Lüfter, Rauschen aus einem Lautsprecher) und jede Einstellung 2–3× messen.

## Was eventuell geht: Steuerung per Bluetooth

| Weg | Status | Bewertung |
|---|---|---|
| BLE-GATT (`bleak`) | Werkzeuge fertig (`explore.py`, `watch.py`), **noch nicht am Gerät ausgeführt** | Offen. Nur wenn `watch.py` beim Umschalten am Gerät eindeutige, reproduzierbare Notifications zeigt, kann ein **Lesen** des Modus gebaut werden. |
| Klassisches Bluetooth (RFCOMM/SPP) | Nicht begonnen | Viele Kopfhörer-Apps nutzen einen herstellereigenen RFCOMM-Kanal. Ohne dokumentiertes Protokoll heißt das: Mitschnitt des App-Verkehrs nötig (z. B. Android-HCI-Snoop-Log). Das ist aufwendig und rechtlich/ethisch nur am eigenen Gerät vertretbar. |
| Öffentliches Reverse Engineering | Nichts gefunden | Kein Startpunkt vorhanden. |

**Schritt 5 (`anc_control.py`) wurde übersprungen:** Es gibt bisher kein beobachtetes Muster, aus dem sich ein Lese- oder gar Schreibbefehl ableiten ließe. Erfundene UUIDs oder Byte-Sequenzen werden bewusst nicht verwendet.

## Was nicht geht bzw. ausgeschlossen ist

- Eigenes Anti-Schall-Signal vom PC: Die Latenz ist viel zu hoch, das bringt kein echtes ANC (bewusst nicht umgesetzt).
- Schreiben unbekannter Befehle „ins Blaue“, Firmware-Änderungen, Flashen: ausgeschlossen (Risiko für das Gerät).
- Die ANC-Stärke stufenlos per PC regeln: nur denkbar, wenn das Gerät/die App so etwas überhaupt anbietet und das Protokoll belegt ist. Bisher unbekannt.

## Empfehlung für den nächsten Schritt

1. **Auf dem Windows-PC mit Hardware:** `scan.py` → `explore.py` → `watch.py` (dabei ANC am Gerät umschalten und mit Marken loggen). Damit ist die BLE-Frage in etwa 15 Minuten geklärt.
2. **Messreihe:** `measure.py` je Einstellung 2–3×, dann `compare.py anc_aus anc_an`. Das liefert sofort nutzbare Ergebnisse.
3. Danach je nach Ergebnis:
   - **BLE-Muster gefunden:** `anc_control.py` nur lesend bauen. Schreiben erst nach Rückfrage und eindeutigem Beleg.
   - **Kein Muster:** Steuerung manuell am Gerät lassen und als nächstes Paket eine **einfache GUI** bauen (z. B. Tkinter): Knopf „Referenz aufnehmen“, Knopf „Einstellung X aufnehmen“, Live-Balkendiagramm der Dämpfung pro Band. Der User schaltet den Modus am Gerät um, die GUI misst und vergleicht.
