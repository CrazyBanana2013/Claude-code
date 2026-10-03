"""Strg+C in install.ps1/uninstall.ps1 (echtes Terminal per Pseudo-TTY, PowerShell 7 im Testmodus).

Strg+C hält die laufende PowerShell-Pipeline an: catch-Blöcke und das abschließende "exit" laufen
dann nicht mehr, nur finally. Die Skripte müssen trotzdem einen Hinweis zeigen und mit Exitcode 1
("vom Benutzer abgebrochen") enden – früher war es still Exitcode 0.
Braucht pwsh (JARVIS_PWSH oder PATH); der Installer-Test zusätzlich uv mit gefülltem Cache.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import sys
import time
from pathlib import Path

import pytest

from app.config import PROJECT_DIR

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Pseudo-TTY nur unter Linux/macOS")


def _pwsh() -> str | None:
    configured = os.environ.get("JARVIS_PWSH")
    if configured:
        return configured if Path(configured).exists() else None
    return shutil.which("pwsh")


class Terminal:
    """Startet einen Prozess in einem Pseudo-Terminal (wie ein Konsolenfenster) und liest die Ausgabe."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path) -> None:
        import pty

        self.output = b""
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # Kindprozess
            os.chdir(cwd)
            os.execve(argv[0], argv, env)

    def _pump(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.fd], [], [], 0.1)
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            self.output += chunk
            if b"\x1b[6n" in chunk:  # Cursorposition erfragt -> wie ein Terminal antworten
                os.write(self.fd, b"\x1b[1;1R")

    def wait_for(self, text: str, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if text.encode("utf-8") in self.output:
                return True
            self._pump(0.3)
        return text.encode("utf-8") in self.output

    def ctrl_c(self) -> None:
        os.write(self.fd, b"\x03")

    def finish(self, seconds: float = 60) -> int:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._pump(0.3)
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self._pump(0.5)
                return os.waitstatus_to_exitcode(status)
        os.kill(self.pid, 9)
        os.waitpid(self.pid, 0)
        raise AssertionError("Prozess endet nicht:\n" + self.text)

    @property
    def text(self) -> str:
        return self.output.decode("utf-8", "replace")


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("JARVIS_") and k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")}
    env.update({"UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never", "UV_NO_PROGRESS": "1", "TERM": "xterm"})
    return env


def test_ctrl_c_at_uninstall_prompt(tmp_path: Path) -> None:
    pwsh = _pwsh()
    if not pwsh:
        pytest.skip("pwsh nicht gefunden (JARVIS_PWSH setzen)")
    target = tmp_path / "JARVIS"
    (target / "app").mkdir(parents=True)
    (target / "app" / "__main__.py").write_text("x", encoding="utf-8")
    (target / "config.yaml").write_text("MEINE CONFIG", encoding="utf-8")
    manifest = {"version": "0.2.0", "source": str(tmp_path), "files": ["app/__main__.py"], "shortcuts": [],
                "autostart": None, "venv": ".venv", "python_env": "uv", "preexisting": []}
    (target / ".jarvis-install.json").write_text(json.dumps(manifest), encoding="utf-8")
    term = Terminal([pwsh, "-NoProfile", "-File", str(PROJECT_DIR / "scripts" / "uninstall.ps1"),
                     "-Target", str(target), "-AllowNonWindows"], _env(), tmp_path)
    assert term.wait_for("JARVIS jetzt entfernen?", 60), term.text
    term.ctrl_c()
    code = term.finish()
    assert code == 1, term.text
    assert "Abgebrochen (Strg+C)" in term.text
    assert (target / "app" / "__main__.py").exists() and (target / "config.yaml").read_text() == "MEINE CONFIG"


@pytest.mark.slow
def test_ctrl_c_in_setup_wizard(tmp_path: Path) -> None:
    """Strg+C im Einrichtungsassistenten: Python bricht ab UND PowerShell hält an – Hinweis, Exitcode 1."""
    pwsh = _pwsh()
    if not pwsh:
        pytest.skip("pwsh nicht gefunden (JARVIS_PWSH setzen)")
    if not shutil.which("uv"):
        pytest.skip("uv nicht im PATH")
    target = tmp_path / "JARVIS"
    args = [pwsh, "-NoProfile", "-File", str(PROJECT_DIR / "scripts" / "install.ps1"), "-Target", str(target),
            "-NoStart", "-NoAutostart", "-NoShortcuts", "-AllowNonWindows"]
    term = Terminal(args, _env(), tmp_path)
    try:
        if not term.wait_for("Geräte während der Einrichtung kurz testen", 300):
            if "uv sync ist fehlgeschlagen" in term.text or "FEHLER" in term.text:
                term.finish()
                pytest.skip("Python-Umgebung nicht offline einrichtbar (uv-Cache leer?)")
            raise AssertionError("Assistent fragt nicht:\n" + term.text)
        term.ctrl_c()
        code = term.finish()
    finally:
        try:
            os.kill(term.pid, 9)
            os.waitpid(term.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
    assert code == 1, term.text
    assert "Abgebrochen – config.yaml wurde nicht verändert." in term.text  # Meldung des Assistenten
    assert "Abgebrochen (Strg+C) - Install.cmd einfach erneut starten" in term.text  # Meldung des Installers
    assert not (target / "config.yaml").exists()
