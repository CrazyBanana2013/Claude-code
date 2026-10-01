"""Schritt 4: Alle Notify-/Indicate-Characteristics beobachten.

Abonniert jede Characteristic, die "notify" oder "indicate" anbietet, und
schreibt jede eingehende Nachricht mit Zeitstempel nach docs/notify-log.txt.
Währenddessen schaltet der User am Kopfhörer den ANC-Modus um. Über
Tastatur-Markierungen (Enter drücken + Text) lassen sich die Umschaltungen
im Log festhalten, z. B. "ANC an" -> danach eingehende Daten zuordnen.

Wichtig zur Sicherheit: Das Abonnieren schreibt laut Bluetooth-Standard den
"Client Characteristic Configuration Descriptor" (CCCD, 0x2902). Das ist der
normale, dokumentierte Weg, Benachrichtigungen einzuschalten – KEIN
herstellerspezifischer Befehl. Andere Schreibzugriffe gibt es hier nicht.

Aufruf:
    python watch.py --address AA:BB:CC:DD:EE:FF --seconds 120
"""

import argparse
import asyncio
import datetime
import sys
import threading
from pathlib import Path

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError

from ble_common import DEFAULT_NAME_HINT, find_device, fmt_bytes

OUT_FILE = Path(__file__).parent / "docs" / "notify-log.txt"


def now() -> str:
    return datetime.datetime.now().isoformat(timespec="milliseconds")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Notify-Characteristics abonnieren und Änderungen loggen")
    parser.add_argument("--address", help="BLE-Adresse aus scan.py")
    parser.add_argument("--name", default=DEFAULT_NAME_HINT, help="Namensteil, falls keine Adresse angegeben")
    parser.add_argument("--seconds", type=float, default=120.0, help="Beobachtungsdauer (Standard: 120 s)")
    args = parser.parse_args()

    device = await find_device(args.address, args.name)
    if device is None:
        print("Gerät nicht gefunden. Erst 'python scan.py' ausführen und Hinweise dort beachten.")
        return

    OUT_FILE.parent.mkdir(exist_ok=True)
    log = OUT_FILE.open("a", encoding="utf-8")  # anhängen: frühere Läufe bleiben erhalten

    def write(text: str) -> None:
        line = f"{now()}  {text}"
        print(line)
        log.write(line + "\n")
        log.flush()

    # Letzter Wert je Characteristic, um echte Änderungen zu markieren
    last_value: dict[int, bytes] = {}

    def on_notify(char: BleakGATTCharacteristic, data: bytearray) -> None:
        old = last_value.get(char.handle)
        changed = "NEU" if old is None else ("GEÄNDERT" if old != bytes(data) else "gleich")
        last_value[char.handle] = bytes(data)
        write(f"NOTIFY handle={char.handle} uuid={char.uuid} [{changed}] {fmt_bytes(data)}")

    write(f"=== Start Beobachtung: {device.name} {device.address}, Dauer {args.seconds:.0f} s ===")
    try:
        async with BleakClient(device) as client:
            subscribed = 0
            for service in client.services:
                for char in service.characteristics:
                    if "notify" in char.properties or "indicate" in char.properties:
                        try:
                            await client.start_notify(char, on_notify)
                            subscribed += 1
                            write(f"Abonniert: handle={char.handle} uuid={char.uuid} service={service.uuid}")
                        except BleakError as err:
                            write(f"Abo fehlgeschlagen: handle={char.handle} uuid={char.uuid}: {err}")
            write(f"{subscribed} Characteristic(s) abonniert.")
            if subscribed == 0:
                write("Keine Notify-Characteristics vorhanden – nichts zu beobachten.")
                return

            print("\nJetzt am Kopfhörer den ANC-Modus umschalten.")
            print("Optional: Text eingeben + Enter, um eine Markierung ins Log zu schreiben (z. B. 'ANC an').\n")

            # Tastatureingaben in einem Daemon-Thread lesen, damit BLE weiterläuft.
            # Daemon = blockiert das Programmende nicht, falls nie Enter kommt.
            loop = asyncio.get_running_loop()

            def read_markers() -> None:
                for text in sys.stdin:
                    if text.strip():
                        loop.call_soon_threadsafe(write, f"MARKE: {text.strip()}")

            threading.Thread(target=read_markers, daemon=True).start()
            await asyncio.sleep(args.seconds)
    except (BleakError, TimeoutError, OSError) as err:
        write(f"FEHLER: {type(err).__name__}: {err}")
    finally:
        write("=== Ende Beobachtung ===")
        log.close()
        print(f"\nLog: {OUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
