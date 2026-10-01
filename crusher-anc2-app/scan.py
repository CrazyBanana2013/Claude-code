"""Schritt 2: 10-Sekunden-BLE-Scan.

Listet alle gefundenen Bluetooth-Low-Energy-Geräte mit Name, Adresse und
RSSI (Signalstärke in dBm, näher an 0 = stärker) auf.

Aufruf:
    python scan.py              # 10 Sekunden scannen
    python scan.py --seconds 20 # länger scannen

Hinweis: Viele Kopfhörer senden BLE-Advertisements nur, solange sie NICHT
mit einem anderen Gerät (z. B. Handy) verbunden sind oder im Pairing-Modus
sind. Erscheinen die Kopfhörer nicht, siehe Ausgabe am Ende.
"""

import argparse
import asyncio
import sys

from bleak import BleakScanner
from bleak.exc import BleakError

from ble_common import DEFAULT_NAME_HINT


async def main() -> None:
    parser = argparse.ArgumentParser(description="BLE-Scan: Name, Adresse, RSSI")
    parser.add_argument("--seconds", type=float, default=10.0, help="Scandauer in Sekunden (Standard: 10)")
    parser.add_argument("--name", default=DEFAULT_NAME_HINT, help="Namensteil, nach dem markiert wird")
    args = parser.parse_args()

    print(f"Scanne {args.seconds:.0f} Sekunden nach BLE-Geräten ...")
    # return_adv=True liefert zusätzlich die Advertisement-Daten (u. a. RSSI)
    try:
        found = await BleakScanner.discover(timeout=args.seconds, return_adv=True)
    except (BleakError, OSError) as err:
        # z. B. kein Bluetooth-Adapter, Bluetooth ausgeschaltet, (Linux) kein BlueZ/D-Bus
        print(f"FEHLER: BLE-Scan nicht möglich: {type(err).__name__}: {err}")
        print("Prüfen: Bluetooth-Adapter vorhanden und in den Windows-Einstellungen eingeschaltet?")
        sys.exit(1)

    # Nach Signalstärke sortieren: stärkstes (nächstes) Gerät zuerst
    rows = sorted(found.values(), key=lambda pair: pair[1].rssi, reverse=True)

    print(f"\n{len(rows)} Gerät(e) gefunden:\n")
    print(f"{'Name':<32} {'Adresse':<40} {'RSSI':>6}")
    print("-" * 80)
    hits = 0
    needle = args.name.lower()
    for device, adv in rows:
        name = adv.local_name or device.name or "(ohne Namen)"
        mark = ""
        if needle in name.lower():
            mark = "  <-- passt zu '" + args.name + "'"
            hits += 1
        print(f"{name:<32} {device.address:<40} {adv.rssi:>4} dBm{mark}")

    print()
    if hits:
        print(f"{hits} Gerät(e) passen zu '{args.name}'. Adresse für explore.py/watch.py notieren.")
    else:
        print(f"Kein Gerät mit '{args.name}' im Namen gefunden. Bitte prüfen:")
        print("  1. Kopfhörer eingeschaltet?")
        print("  2. Kopfhörer vom Handy trennen (Bluetooth am Handy aus) – sonst oft unsichtbar.")
        print("  3. Pairing-Modus aktivieren (Tastenfolge laut Skullcandy-Anleitung der Crusher ANC 2).")
        print("  4. Windows: Einstellungen > Bluetooth muss eingeschaltet sein.")
        print("  5. Erneut ausführen: python scan.py --seconds 20")
        print("  Hinweis: Gerät heißt evtl. anders – Liste oben nach unbekannten Namen durchsehen.")


if __name__ == "__main__":
    asyncio.run(main())
