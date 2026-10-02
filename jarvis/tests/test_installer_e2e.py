"""Ende-zu-Ende-Test des Installers: ZIP bauen → entpacken → installieren → Update → deinstallieren.

LANGSAM (ca. 20–60 s, startet zweimal einen echten JARVIS-Server). Abwählen mit: pytest -m "not slow"

Läuft wie beim Benutzer, nur unter PowerShell 7 auf Linux im Testmodus (-AllowNonWindows), daher
ohne Startmenü/Autostart/Admin-Prüfung. Benötigt pwsh (Umgebungsvariable JARVIS_PWSH oder PATH)
und uv im PATH, sonst wird der Test übersprungen. `uv sync` darf fehlende Pakete von pypi.org laden.

Geprüft wird das Zusammenspiel aller drei Teile: build_installer.py (Vertrag 4), install.ps1/
uninstall.ps1 (Vertrag 3), setup_wizard (Vertrag 1) und die PID-Datei von python -m app (Vertrag 2).
Eine vorhandene config.yaml (mit freiem Port) muss dabei Byte für Byte erhalten bleiben.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import psutil
import pytest
import yaml

from app.config import PROJECT_DIR

pytestmark = pytest.mark.slow

STEP_TIMEOUT = 600  # Sekunden pro Skriptaufruf (Paket-Download bei leerem uv-Cache eingerechnet)


def _pwsh() -> str | None:
    configured = os.environ.get("JARVIS_PWSH")
    if configured:
        return configured if Path(configured).exists() else None
    return shutil.which("pwsh")


def _free_port() -> int:
    # Der Server lauscht wie in config.example.yaml auf 0.0.0.0 – dort muss der Port frei sein.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def _env(uv: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("JARVIS_") and k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")}
    env["JARVIS_NOPAUSE"] = "1"
    env["UV_NO_PROGRESS"] = "1"
    env["UV_PYTHON_DOWNLOADS"] = "never"  # vorhandenes Python >= 3.11 nehmen, nichts von GitHub laden
    env["PATH"] = str(Path(uv).parent) + os.pathsep + env.get("PATH", "")
    return env


def _run(pwsh: str, script: Path, args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [pwsh, "-NoProfile", "-File", str(script), *args],
        cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=STEP_TIMEOUT,
    )


def _output(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


def _http() -> httpx.Client:
    return httpx.Client(timeout=3, trust_env=False)  # nie über einen Proxy aus der Umgebung


def _health(port: int) -> int | None:
    try:
        with _http() as client:
            return client.get(f"http://127.0.0.1:{port}/api/health").status_code
    except httpx.HTTPError:
        return None


def _wait_health(port: int, seconds: float = 20) -> int | None:
    deadline = time.monotonic() + seconds
    status = _health(port)
    while status != 200 and time.monotonic() < deadline:
        time.sleep(0.3)
        status = _health(port)
    return status


def _alive(pid: int) -> bool:
    """Läuft der Prozess noch? Zombies (beendet, nur noch nicht abgeholt) zählen als beendet."""
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _wait_gone(pid: int, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    return not _alive(pid)


def _read_pid(target: Path) -> dict:
    data = json.loads((target / "state" / "server.pid").read_text(encoding="utf-8"))
    assert isinstance(data["pid"], int), data
    return data


def _is_our_server(pid: int, target: Path) -> bool:
    try:
        proc = psutil.Process(pid)
        cmdline = proc.cmdline()
        return "app" in cmdline and "-m" in cmdline and str(target) in " ".join([proc.cwd(), *cmdline])
    except psutil.Error:
        return False


def _kill_leftovers(target: Path, pids: set[int]) -> None:
    """Aufräumen: jeden noch laufenden Server dieser Testinstallation beenden (PID-Datei + Suche)."""
    pid_file = target / "state" / "server.pid"
    if pid_file.is_file():
        try:
            pids.add(int(json.loads(pid_file.read_text(encoding="utf-8"))["pid"]))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    for proc in psutil.process_iter(["pid"]):
        if _is_our_server(proc.info["pid"], target):
            pids.add(proc.info["pid"])
    for pid in pids:
        if _alive(pid) and _is_our_server(pid, target):
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass


def _build_and_extract(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    build = subprocess.run(
        [sys.executable, str(PROJECT_DIR / "scripts" / "build_installer.py"), "--out", str(dist)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    zips = sorted(dist.glob("JARVIS-Setup-*.zip"))
    assert len(zips) == 1, zips
    with zipfile.ZipFile(zips[0]) as archive:
        tops = {name.split("/", 1)[0] for name in archive.namelist()}
        assert tops == {zips[0].stem}, tops
        archive.extractall(tmp_path / "paket")
    return tmp_path / "paket" / zips[0].stem


def _package_files(package: Path) -> list[str]:
    return sorted(p.relative_to(package).as_posix() for p in package.rglob("*") if p.is_file())


def test_install_update_uninstall_end_to_end(tmp_path: Path) -> None:
    pwsh = _pwsh()
    if not pwsh:
        pytest.skip("pwsh nicht gefunden (Umgebungsvariable JARVIS_PWSH setzen oder pwsh in den PATH)")
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv nicht im PATH")
    env = _env(uv)

    # a) Release-ZIP bauen und entpacken (CRLF-Skripte wie unter Windows).
    package = _build_and_extract(tmp_path)
    packaged = _package_files(package)
    assert "scripts/install.ps1" in packaged and not any(f.startswith("tests/") for f in packaged)
    assert (package / "scripts" / "install.ps1").read_bytes().count(b"\r\n") > 10

    # b) Zielordner mit vorhandener config.yaml (freier Port): muss unverändert bleiben.
    target = tmp_path / "JARVIS"
    target.mkdir()
    port = _free_port()
    example = (package / "config.example.yaml").read_text(encoding="utf-8")
    config_text, n = re.subn(r"(?m)^(\s+port:\s*)8765\b", rf"\g<1>{port}", example)
    assert n == 1, "server.port nicht in config.example.yaml gefunden"
    (target / "config.yaml").write_text(config_text, encoding="utf-8", newline="\n")
    config_bytes = (target / "config.yaml").read_bytes()
    install_args = ["-Target", str(target), "-Yes", "-NoAutostart", "-NoShortcuts", "-AllowNonWindows"]
    seen_pids: set[int] = set()

    try:
        proc = _run(pwsh, package / "scripts" / "install.ps1", install_args, env, tmp_path)
        out = _output(proc)

        # c) Installation prüfen.
        assert proc.returncode == 0, out
        assert "[10/10]" in out, out
        manifest = json.loads((target / ".jarvis-install.json").read_text(encoding="utf-8"))
        installed = sorted(manifest["files"])
        assert installed == packaged, "Manifest listet genau die Dateien des Pakets"
        for rel in packaged:  # Programmdateien 1:1 kopiert (CRLF bleibt)
            assert (target / rel).read_bytes() == (package / rel).read_bytes(), rel
        assert not (target / "tests").exists()
        assert manifest["version"] == package.name.removeprefix("JARVIS-Setup-")
        assert Path(manifest["source"]) == package
        assert manifest["shortcuts"] == [] and manifest["autostart"] is None
        assert manifest["python_env"] == "uv"
        assert (target / "config.yaml").read_bytes() == config_bytes, "vorhandene config.yaml verändert"
        assert not (target / "config.yaml.bak").exists()
        secrets = yaml.safe_load((target / "secrets.yaml").read_text(encoding="utf-8"))
        token = secrets["api_token"]
        assert isinstance(token, str) and len(token) >= 32
        assert out.count(token) == 1, "neuer Token wird genau einmal angezeigt"
        secrets_bytes = (target / "secrets.yaml").read_bytes()
        assert (target / ".venv" / "bin" / "python").exists()
        assert "New-NetFirewallRule" in out and f"Niemals Port {port}" in out
        assert _wait_health(port) == 200, out
        with _http() as client:
            assert client.get(f"http://127.0.0.1:{port}/api/tools").status_code == 401
            ok = client.get(f"http://127.0.0.1:{port}/api/tools", headers={"Authorization": f"Bearer {token}"})
            assert ok.status_code == 200
        pid_info = _read_pid(target)
        first_pid = pid_info["pid"]
        seen_pids.add(first_pid)
        assert _alive(first_pid) and _is_our_server(first_pid, target)
        assert Path(pid_info["executable"]) == target / ".venv" / "bin" / "python"
        assert psutil.Process(first_pid).cwd() == str(target), "Server läuft aus der Installation"

        # d) Update (dasselbe Paket erneut): Server wird beendet und neu gestartet, Daten bleiben.
        proc = _run(pwsh, package / "scripts" / "install.ps1", install_args, env, tmp_path)
        out = _output(proc)
        assert proc.returncode == 0, out
        assert "wird aktualisiert" in out and "Laufender JARVIS-Server wurde beendet" in out, out
        assert "Token vorhanden" in out and token not in out
        assert (target / "config.yaml").read_bytes() == config_bytes
        assert (target / "secrets.yaml").read_bytes() == secrets_bytes
        assert _wait_gone(first_pid), "alter Server läuft noch"
        assert _wait_health(port) == 200, out
        second_pid = _read_pid(target)["pid"]
        seen_pids.add(second_pid)
        assert second_pid != first_pid and _is_our_server(second_pid, target)

        # e) Deinstallieren (Skript aus der Installation, wie Uninstall.cmd): Einstellungen bleiben.
        proc = _run(pwsh, target / "scripts" / "uninstall.ps1",
                    ["-Target", str(target), "-Yes", "-AllowNonWindows"], env, tmp_path)
        out = _output(proc)
        assert proc.returncode == 0, out
        assert _wait_gone(second_pid), "Server läuft nach der Deinstallation noch"
        assert _health(port) is None
        for rel in packaged:
            assert not (target / rel).exists(), rel
        for name in ("app", "web", "launcher", "scripts", ".venv"):
            assert not (target / name).exists(), name
        assert (target / "config.yaml").read_bytes() == config_bytes
        assert (target / "secrets.yaml").read_bytes() == secrets_bytes
        remnant = json.loads((target / ".jarvis-install.json").read_text(encoding="utf-8"))
        assert remnant["files"] == [] and remnant.get("uninstalled_at"), remnant
        left = sorted(p.name for p in target.iterdir())
        assert set(left) <= {".jarvis-install.json", "config.yaml", "secrets.yaml", "state"}, left

        # ... und später mit -Purge (aus dem entpackten Paket, die Installation hat keins mehr).
        proc = _run(pwsh, package / "scripts" / "uninstall.ps1",
                    ["-Target", str(target), "-Yes", "-Purge", "-AllowNonWindows"], env, tmp_path)
        out = _output(proc)
        assert proc.returncode == 0, out
        assert not target.exists() or not any(target.iterdir()), sorted(p.name for p in target.iterdir())
    finally:
        # f) Nie einen Server zurücklassen.
        _kill_leftovers(target, seen_pids)
