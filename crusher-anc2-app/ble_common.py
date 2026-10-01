"""Gemeinsame Hilfsfunktionen für die BLE-Skripte (explore.py, watch.py).

Hier steht nur, was beide Skripte gleich brauchen: das Gerät finden und
Bytes lesbar formatieren. Es werden KEINE geräte­spezifischen UUIDs oder
Befehle verwendet – alles wird zur Laufzeit vom Gerät selbst abgefragt.
"""

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

# Standard-Suchbegriff für den Gerätenamen. ANNAHME: Der Name enthält
# "Crusher" (so heißt das Produkt). Das ist nicht verifiziert – wenn der
# Scan einen anderen Namen zeigt, mit --name oder --address überschreiben.
DEFAULT_NAME_HINT = "Crusher"


async def find_device(address: str | None, name_hint: str, timeout: float = 10.0) -> BLEDevice | None:
    """Wie _find_device, fängt aber Adapter-Fehler ab (kein Bluetooth, BT aus)."""
    try:
        return await _find_device(address, name_hint, timeout)
    except (BleakError, OSError) as err:
        print(f"FEHLER: BLE nicht verfügbar: {type(err).__name__}: {err}")
        return None


async def _find_device(address: str | None, name_hint: str, timeout: float = 10.0) -> BLEDevice | None:
    """Sucht das Gerät per Adresse (bevorzugt) oder per Namensteil."""
    if address:
        print(f"Suche Gerät mit Adresse {address} ({timeout:.0f} s) ...")
        return await BleakScanner.find_device_by_address(address, timeout=timeout)

    print(f"Suche Gerät, dessen Name '{name_hint}' enthält ({timeout:.0f} s) ...")
    needle = name_hint.lower()
    return await BleakScanner.find_device_by_filter(
        lambda dev, adv: needle in ((adv.local_name or dev.name or "").lower()),
        timeout=timeout,
    )


def fmt_bytes(data: bytes | bytearray) -> str:
    """Bytes als Hex plus (falls druckbar) als Text, z. B. '48 69 | "Hi"'."""
    hex_part = data.hex(" ") if data else "(leer)"
    try:
        text = bytes(data).decode("utf-8")
        if text and text.isprintable():
            return f'{hex_part} | "{text}"'
    except UnicodeDecodeError:
        pass
    return hex_part
