"""Nur lesende Probe (GET) gegen WLED und ESPHome – schaltet nichts.

Aufruf im Ordner jarvis:
    .venv\\Scripts\\python.exe scripts\\probe.py                 (nutzt config.yaml)
    .venv\\Scripts\\python.exe scripts\\probe.py --esphome http://<IP>   (nur ESPHome prüfen)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import quote

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ConfigError, is_todo, load_config  # noqa: E402

TIMEOUT = 3.0


def get(client: httpx.Client, url: str) -> httpx.Response | None:
    try:
        r = client.get(url)
        print(f"  GET {url} → HTTP {r.status_code}")
        return r
    except httpx.HTTPError as exc:
        print(f"  GET {url} → nicht erreichbar ({type(exc).__name__})")
        return None


def probe_esphome(client: httpx.Client, base: str, entities: list[str]) -> None:
    base = base.rstrip("/")
    print(f"\nESPHome {base}")
    root = get(client, base + "/")
    if root is None:
        print("  → Gerät nicht erreichbar (IP, WLAN, Firewall prüfen).")
        return
    if root.status_code == 401:
        print("  → web_server mit Passwort (auth:) aktiv – JARVIS unterstützt das noch nicht.")
    elif root.status_code >= 400:
        print("  → Kein ESPHome-Webserver? In der ESPHome-YAML fehlt vermutlich 'web_server:'.")
    for entity in entities:
        r = get(client, f"{base}/sensor/{quote(entity, safe='')}")
        if r is not None and r.status_code == 200:
            print(f"    Antwort: {r.text[:200]}")
    if not entities:
        print("  Tipp: Sensor-Namen stehen auf der Webseite des Geräts (im Browser öffnen).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--esphome", help="nur diese ESPHome-Basis-URL prüfen")
    parser.add_argument("--entity", action="append", default=[], help="Sensorname (mehrfach möglich)")
    args = parser.parse_args()

    with httpx.Client(timeout=TIMEOUT, trust_env=False) as client:
        if args.esphome:
            probe_esphome(client, args.esphome, args.entity)
            return 0
        try:
            cfg = load_config()
        except ConfigError as exc:
            print(exc)
            return 2
        if is_todo(cfg.wled.base_url):
            print("WLED: wled.base_url ist noch TODO.")
        else:
            print(f"\nWLED {cfg.wled.base_url}")
            r = get(client, cfg.wled.base_url.rstrip("/") + "/json/info")
            if r is not None and r.status_code == 200:
                info = r.json()
                print(f"    Name: {info.get('name')}  Version: {info.get('ver')}  LEDs: {info.get('leds', {}).get('count')}")
        by_base: dict[str, list[str]] = {}
        for s in cfg.sensors:
            if is_todo(s.base_url):
                print(f"Sensor '{s.name}': base_url ist noch TODO.")
                continue
            by_base.setdefault(s.base_url, []).extend([] if is_todo(s.entity_id) else [s.entity_id])
        for base, entities in by_base.items():
            probe_esphome(client, base, entities)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
