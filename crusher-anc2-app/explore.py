"""Schritt 3: GATT-Struktur des Geräts auslesen (nur lesend!).

Verbindet sich per BLE, listet alle Services, Characteristics, deren
Properties (read/write/notify/...) und Descriptors. Characteristics mit
"read"-Property werden einmal gelesen. Es wird NICHTS geschrieben.

Die Ausgabe erscheint im Terminal und wird unverändert in
docs/gatt-dump.txt gespeichert.

Aufruf:
    python explore.py --address AA:BB:CC:DD:EE:FF   # Adresse aus scan.py
    python explore.py --name Crusher                # oder per Namensteil
"""

import argparse
import asyncio
import datetime
from pathlib import Path

from bleak import BleakClient
from bleak.exc import BleakError

from ble_common import DEFAULT_NAME_HINT, find_device, fmt_bytes

OUT_FILE = Path(__file__).parent / "docs" / "gatt-dump.txt"


async def main() -> None:
    parser = argparse.ArgumentParser(description="GATT-Services/Characteristics auflisten (nur lesen)")
    parser.add_argument("--address", help="BLE-Adresse aus scan.py")
    parser.add_argument("--name", default=DEFAULT_NAME_HINT, help="Namensteil, falls keine Adresse angegeben")
    args = parser.parse_args()

    lines: list[str] = []

    def out(text: str = "") -> None:
        """Gleichzeitig ausgeben und für die Datei sammeln."""
        print(text)
        lines.append(text)

    device = await find_device(args.address, args.name)
    if device is None:
        print("Gerät nicht gefunden. Erst 'python scan.py' ausführen und Hinweise dort beachten.")
        return

    out(f"# GATT-Dump erstellt am {datetime.datetime.now().isoformat(timespec='seconds')}")
    out(f"# Gerät: {device.name}  Adresse: {device.address}")
    out()

    try:
        async with BleakClient(device) as client:
            for service in client.services:
                out(f"[Service] {service.uuid}  ({service.description})  handle={service.handle}")
                for char in service.characteristics:
                    props = ",".join(char.properties)
                    out(f"  [Char] {char.uuid}  ({char.description})  handle={char.handle}  props=[{props}]")

                    # Nur lesen, wenn das Gerät "read" selbst anbietet
                    if "read" in char.properties:
                        try:
                            value = await client.read_gatt_char(char)
                            out(f"      Wert: {fmt_bytes(value)}")
                        except BleakError as err:
                            out(f"      Wert: <Lesefehler: {err}>")

                    for desc in char.descriptors:
                        try:
                            value = await client.read_gatt_descriptor(desc)
                            out(f"      [Desc] {desc.uuid}  ({desc.description})  handle={desc.handle}  Wert: {fmt_bytes(value)}")
                        except BleakError as err:
                            out(f"      [Desc] {desc.uuid}  ({desc.description})  handle={desc.handle}  <Lesefehler: {err}>")
                out()
    except (BleakError, TimeoutError, OSError) as err:
        out(f"FEHLER bei Verbindung/Abfrage: {type(err).__name__}: {err}")
        out("Tipp: Kopfhörer vom Handy trennen, ggf. in Windows koppeln, dann erneut versuchen.")

    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nGespeichert: {OUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
