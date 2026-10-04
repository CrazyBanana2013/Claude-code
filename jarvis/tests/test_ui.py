import os
import re
import shutil
import socket
import subprocess
import sys
import time

import httpx
import pytest

from app.config import PROJECT_DIR
from tests.conftest import client_for

pytestmark = pytest.mark.anyio

INDEX = (PROJECT_DIR / "web" / "index.html").read_text(encoding="utf-8")


async def test_index_served(factory):
    app, _ = factory()
    async with client_for(app) as c:
        r = await c.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>JARVIS</title>" in r.text


def test_index_has_no_external_resources():
    assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", INDEX, re.I)
    assert "@import" not in INDEX
    assert "fetch(\"http" not in INDEX


async def test_ui_uses_only_registered_tools(factory):
    used = set(re.findall(r'data-tool="([a-z_]+)"', INDEX))
    used |= set(re.findall(r'tool\("([a-z_]+)"', INDEX))
    used |= set(re.findall(r'"(scripts_(?:start|stop))"', INDEX))
    app, token = factory()
    async with client_for(app, token=token) as c:
        names = {t["name"] for t in (await c.get("/api/tools")).json()["tools"]}
    assert used and used <= names, used - names


async def test_ui_api_paths_exist(factory):
    """Jeder feste /api/...-Pfad, den die Oberfläche aufruft, ist eine Route des Servers (kein 404/405 vom Router)."""
    used = set(re.findall(r'api\("(/api/[a-z_/]+)"', INDEX)) | set(re.findall(r'fetch\("(/api/[a-z_/]+)"', INDEX))
    assert {"/api/voice/status", "/api/voice/stop", "/api/voice/stt", "/api/chat", "/api/confirm/"} <= used
    get_paths = {"/api/health", "/api/status", "/api/voice/status"}  # in der Oberfläche per GET
    app, token = factory()
    missing = []
    async with client_for(app, token=token) as c:
        for path in sorted(used):
            url = path + ("scripts_list" if path == "/api/tools/" else "x" if path.endswith("/") else "")
            r = await (c.get(url) if path in get_paths else c.post(url))
            if r.status_code == 405 or (r.status_code == 404 and r.json() == {"detail": "Not Found"}):
                missing.append(f"{path} → {r.status_code}")
    assert not missing, missing


def test_ui_never_captures_screen_or_sends_images():
    # Bildschirmfotos macht nur der PC (screen_describe → lokales Ollama); die Oberfläche bekommt nur Text.
    assert "getDisplayMedia" not in INDEX
    assert "toDataURL" not in INDEX and "toBlob" not in INDEX  # kein Bild wird im Browser kodiert/hochgeladen
    # Bildschirmbeschreibung wird nie als HTML eingesetzt
    assert "description + " not in INDEX and "innerHTML = r.description" not in INDEX


async def test_ui_endpoints_respond(factory):
    app, token = factory()
    async with client_for(app, token=token) as c:
        assert (await c.get("/api/status")).json()["server"] == "ok"
        assert (await c.post("/api/tools/scripts_list")).status_code == 200
        assert (await c.post("/api/tools/sensors_read")).status_code == 200
        r = await c.post("/api/tools/led_status")  # WLED noch TODO → verständlicher Fehler
        assert r.status_code == 400 and "nicht eingerichtet" in r.json()["error"]
        assert (await c.post("/api/tools/gibtsnicht")).status_code == 404


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_server_starts_with_example_config(tmp_path):
    shutil.copy(PROJECT_DIR / "config.example.yaml", tmp_path / "config.yaml")
    port = _free_port()
    env = {**os.environ, "JARVIS_CONFIG": str(tmp_path / "config.yaml"),
           "JARVIS_SECRETS": str(tmp_path / "secrets.yaml"), "JARVIS_STATE": str(tmp_path / "state")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 15
        while True:
            try:
                r = httpx.get(base + "/api/health", timeout=1, trust_env=False)
                break
            except httpx.HTTPError:
                if time.time() > deadline or proc.poll() is not None:
                    proc.kill()
                    pytest.fail("Server startet nicht:\n" + proc.communicate()[0])
                time.sleep(0.2)
        assert r.status_code == 200
        assert httpx.get(base + "/", trust_env=False).status_code == 200
        assert httpx.get(base + "/api/tools", trust_env=False).status_code == 401
        token = (tmp_path / "secrets.yaml").read_text().split("api_token:")[1].strip()
        r = httpx.post(base + "/api/chat", json={"message": "wie warm ist es"},
                       headers={"Authorization": f"Bearer {token}"}, trust_env=False, timeout=10)
        assert r.status_code == 200 and r.json()["source"] == "fallback"
    finally:
        proc.terminate()
        out = proc.communicate(timeout=10)[0]
    assert "Traceback" not in out, out
    assert "API-Token" in out  # Token wurde beim ersten Start einmalig ausgegeben
