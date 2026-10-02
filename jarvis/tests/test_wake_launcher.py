"""Führt den Browser-Test für launcher/wake.html aus (nur wenn Node + Playwright vorhanden)."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import PROJECT_DIR

WAKE = PROJECT_DIR / "launcher" / "wake.html"


def _playwright_module() -> str | None:
    if os.environ.get("PLAYWRIGHT_MODULE"):
        return os.environ["PLAYWRIGHT_MODULE"]
    npm = shutil.which("npm")
    if not npm:
        return None
    root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True).stdout.strip()
    candidate = Path(root) / "playwright"
    return str(candidate.resolve()) if candidate.exists() else None


def test_wake_contains_no_personal_data():
    html = WAKE.read_text(encoding="utf-8")
    assert not re.search(r"\b[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}\b", html.replace("AA:BB:CC:DD:EE:FF", ""))
    assert "localStorage" in html
    assert "https://www.depicus.com/wake-on-lan/woli" in html


def test_wake_html_in_browser():
    node = shutil.which("node")
    module = _playwright_module()
    if not node or not module:
        pytest.skip("Node/Playwright nicht installiert")
    env = {**os.environ, "PLAYWRIGHT_MODULE": module}
    proc = subprocess.run([node, str(PROJECT_DIR / "tests" / "web" / "wake.test.mjs")],
                          capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
