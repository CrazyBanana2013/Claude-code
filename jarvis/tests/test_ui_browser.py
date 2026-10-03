"""Browser-Test der HUD-Oberfläche web/index.html gegen einen echten JARVIS-Server.

Ablauf: Fake-WLED/ESPHome (HTTP-Server im Thread, freier Port) + JARVIS per uvicorn (freier Port,
Config/Secrets/State in tmp_path), danach tests/web/hud.test.mjs mit Playwright/Chromium.
Wird übersprungen, wenn Node oder Playwright fehlen (wie der wake.html-Test).
Optional: HUD_SHOTS=<ordner> legt Screenshots der wichtigsten Zustände ab.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import httpx
import psutil
import pytest
import yaml

from app.config import PROJECT_DIR
from tests.test_wake_launcher import _playwright_module

HUD_TEST = PROJECT_DIR / "tests" / "web" / "hud.test.mjs"
TOKEN = "hud-browser-test-" + "a1b2c3d4" * 5  # ≥ 32 Zeichen, sonst erzeugt JARVIS einen neuen

# Wie ein echtes WLED: Index in der Liste = Effekt-ID, Presets aus presets.json
EFFECTS = ["Solid", "Blink", "Breathe", "Wipe", "Rainbow", "Fire 2012", "Colorloop"]
PRESETS = {"0": {}, "1": {"n": "Abend"}, "2": {"n": "Gaming"}, "3": {"n": "Nacht"}}
SENSOR_UNITS = {"Temp": "°C", "Hum": "%"}


class FakeDevices:
    """Fake-WLED (JSON-API) + Fake-ESPHome (REST) mit Test-Steuerung unter /_test/."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.state = {"on": False, "bri": 128, "ps": -1, "seg": [{"col": [[0, 200, 255]], "fx": 0}]}
            self.sensors: dict[str, float | None] = {"Temp": 21.43, "Hum": 47.2}
            self.posts: list[dict] = []
            # WLED-Störung: None = normal, "drop" = ohne Antwort auflegen, Zahl = HTTP-Fehlercode
            self.wled_fail: str | int | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps({"state": self.state, "sensors": self.sensors, "posts": self.posts}))

    def apply(self, body: dict) -> None:
        """Teil-Zustand wie WLED übernehmen ("on": "t" schaltet um, "seg" als Objekt)."""
        with self.lock:
            if "on" in body:
                self.state["on"] = (not self.state["on"]) if body["on"] == "t" else bool(body["on"])
            for key in ("bri", "ps"):
                if key in body:
                    self.state[key] = body[key]
            seg = body.get("seg") or {}
            if isinstance(seg, list):
                seg = seg[0] if seg else {}
            for key in ("col", "fx"):
                if key in seg:
                    self.state["seg"][0][key] = seg[key]

    def handler(self):
        devices = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, obj, code: int = 200) -> None:
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}") if length else {}

            def _wled_broken(self, path: str) -> bool:
                """Gestörtes WLED nachbilden; True = Anfrage ist damit erledigt."""
                if not (path.startswith("/json/") or path == "/presets.json"):
                    return False
                with devices.lock:
                    fail = devices.wled_fail
                if fail is None:
                    return False
                if fail == "drop":
                    self.close_connection = True  # ohne Antwort auflegen → httpx: RemoteProtocolError
                else:
                    self._json({"error": "gestört"}, int(fail))
                return True

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                if self._wled_broken(path):
                    return None
                if path == "/json/state":
                    return self._json(devices.snapshot()["state"])
                if path == "/json/eff":
                    return self._json(EFFECTS)
                if path == "/presets.json":
                    return self._json(PRESETS)
                if path.startswith("/sensor/"):
                    name = unquote(path[len("/sensor/"):])
                    with devices.lock:
                        known = name in devices.sensors
                        value = devices.sensors.get(name)
                    if not known:
                        return self._json({}, 404)
                    if value is None:  # Sensor defekt → HTTP 500
                        return self._json({"error": "defekt"}, 500)
                    return self._json({"id": f"sensor/{name}", "state": f"{value} {SENSOR_UNITS[name]}",
                                       "value": value})
                if path == "/_test/state":
                    return self._json(devices.snapshot())
                return self._json({"error": "unbekannt"}, 404)

            def do_POST(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                body = self._body()
                if self._wled_broken(path):
                    return None
                if path == "/json/state":
                    with devices.lock:
                        devices.posts.append(body)
                    devices.apply(body)
                    return self._json(devices.snapshot()["state"])
                if path == "/_test/state":  # Gerätezustand direkt setzen (ohne Protokoll)
                    devices.apply(body)
                    return self._json(devices.snapshot())
                if path == "/_test/sensors":
                    with devices.lock:
                        devices.sensors.update({k: v for k, v in body.items() if k in SENSOR_UNITS})
                    return self._json(devices.snapshot())
                if path == "/_test/fail":
                    with devices.lock:
                        devices.wled_fail = body.get("wled")
                    return self._json(devices.snapshot())
                if path == "/_test/reset":
                    devices.reset()
                    return self._json(devices.snapshot())
                return self._json({"error": "unbekannt"}, 404)

            def log_message(self, *args):
                pass

        return Handler


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_config(tmp_path: Path, port: int, fake_url: str) -> dict[str, str]:
    dummy = [sys.executable, str(PROJECT_DIR / "tests" / "dummy_script.py"), str(tmp_path / "dummy.pid")]
    config = {
        "server": {"bind": "127.0.0.1", "port": port},
        # Modell TODO → Regel-Parser; base_url zeigt auf den Fake (kein echtes Ollama nötig)
        "llm": {"model": "TODO_MODELLNAME", "base_url": fake_url},
        "wled": {"base_url": fake_url, "timeout": 3},
        "sensors": [
            {"name": "Temperatur", "type": "esphome_rest", "base_url": fake_url, "entity_id": "Temp",
             "unit": "°C"},
            {"name": "Luftfeuchte", "type": "esphome_rest", "base_url": fake_url, "entity_id": "Hum",
             "unit": "%"},
        ],
        "scripts": [
            # hide_window: unter Windows ohne Konsolenfenster (Ausgabe in state/logs)
            {"id": "dummy", "label": "Dummy-Skript", "cwd": str(tmp_path), "command": dummy,
             "hide_window": True},
            {"id": "cs2", "label": "CS2-Skript", "cwd": "TODO_ORDNER_DES_CS2_SKRIPTS",
             "command": ["TODO_PROGRAMM", "TODO_ARGUMENT"]},
        ],
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    (tmp_path / "secrets.yaml").write_text(yaml.safe_dump({"api_token": TOKEN}), encoding="utf-8")
    return {
        "JARVIS_CONFIG": str(tmp_path / "config.yaml"),
        "JARVIS_SECRETS": str(tmp_path / "secrets.yaml"),
        "JARVIS_STATE": str(tmp_path / "state"),
    }


def _wait_healthy(base: str, proc: subprocess.Popen, log_path: Path) -> None:
    deadline = time.time() + 20
    while True:
        try:
            if httpx.get(base + "/api/health", timeout=1, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.time() > deadline or proc.poll() is not None:
            pytest.fail("JARVIS startet nicht:\n" + log_path.read_text(encoding="utf-8", errors="replace"))
        time.sleep(0.2)


def _stop_process_tree(proc: subprocess.Popen) -> None:
    """Server und alles, was er gestartet hat (Dummy-Skript), sicher beenden."""
    try:
        children = psutil.Process(proc.pid).children(recursive=True)
    except psutil.Error:
        children = []
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    for child in children:
        try:
            child.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(children, timeout=5)


@pytest.mark.slow  # ca. 3 min: echter Server + Chromium; abwählen mit -m "not slow"
def test_hud_in_browser(tmp_path):
    node = shutil.which("node")
    module = _playwright_module()
    if not node or not module:
        pytest.skip("Node/Playwright nicht installiert")

    devices = FakeDevices()
    fake = ThreadingHTTPServer(("127.0.0.1", 0), devices.handler())
    fake.daemon_threads = True
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    fake_url = f"http://127.0.0.1:{fake.server_address[1]}"

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, **_write_config(tmp_path, port, fake_url)}
    log_path = tmp_path / "server.log"
    proc = None
    try:
        with open(log_path, "wb") as log:  # Datei statt PIPE: viele Zugriffslogs dürfen nie blockieren
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=PROJECT_DIR, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
        _wait_healthy(base, proc, log_path)
        node_env = {**os.environ, "PLAYWRIGHT_MODULE": module, "JARVIS_URL": base, "JARVIS_TOKEN": TOKEN,
                    "FAKE_URL": fake_url}
        run = subprocess.run([node, str(HUD_TEST)], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", env=node_env, timeout=900)
        server_log = log_path.read_text(encoding="utf-8", errors="replace")
        tail = "\n--- Server-Log (Ende) ---\n" + server_log[-4000:]
        assert run.returncode == 0, run.stdout + run.stderr + tail
        assert "Traceback" not in server_log, server_log
        # Der Test darf nie eine Bestätigung an den Server schicken (auf Windows = echtes Herunterfahren)
        assert "/api/confirm" not in server_log, server_log
    finally:
        if proc is not None:
            _stop_process_tree(proc)
        fake.shutdown()
        fake.server_close()
