"""Tests für die Windows-Installer-Skripte (Install.cmd, Uninstall.cmd, scripts/*.ps1).

Die eigentlichen Tests liegen als pwsh-Skripte unter tests/ps/*.tests.ps1 und laufen im
Testmodus (-AllowNonWindows) unter PowerShell 7 auf Linux. pwsh wird über die
Umgebungsvariable JARVIS_PWSH oder den PATH gefunden; fehlt es, werden sie übersprungen.
Exitcode eines pwsh-Testskripts: 0 = alles grün, 77 = übersprungen, sonst Zahl der Fehlschläge.

Die reinen Python-Prüfungen (ASCII, .gitattributes, .cmd-Aufrufe, keine PS7-Operatoren)
laufen immer, auch ohne pwsh.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import PROJECT_DIR

PS_DIR = PROJECT_DIR / "tests" / "ps"
PS_TESTS = sorted(PS_DIR.glob("*.tests.ps1"))
SKIP_EXIT = 77
SKIP_DIRS = {".venv", "node_modules", ".git", "dist", "__pycache__", ".pytest_cache", "state"}


def _pwsh() -> str | None:
    configured = os.environ.get("JARVIS_PWSH")
    if configured:
        return configured if Path(configured).exists() else None
    return shutil.which("pwsh")


def _script_files(*suffixes: str) -> list[Path]:
    found = []
    for path in sorted(PROJECT_DIR.rglob("*")):
        rel = path.relative_to(PROJECT_DIR)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.is_file() and path.suffix.lower() in suffixes:
            found.append(path)
    return found


def test_pwsh_test_scripts_present():
    names = {p.name for p in PS_TESTS}
    expected = {"lib.tests.ps1", "syntax.tests.ps1", "stop.tests.ps1", "uninstall.tests.ps1", "install-flow.tests.ps1",
                "windows-sim.tests.ps1"}
    assert expected <= names, expected - names


@pytest.mark.parametrize("script", PS_TESTS, ids=lambda p: p.name)
def test_pwsh(script: Path):
    pwsh = _pwsh()
    if not pwsh:
        pytest.skip("pwsh nicht gefunden (Umgebungsvariable JARVIS_PWSH setzen oder pwsh in den PATH)")
    env = {k: v for k, v in os.environ.items() if not k.startswith("JARVIS_")}
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=900,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == SKIP_EXIT:
        reason = next((line for line in output.splitlines() if line.startswith("SKIP:")), "übersprungen")
        pytest.skip(reason)
    assert proc.returncode == 0, output


@pytest.mark.parametrize("path", _script_files(".ps1", ".cmd"), ids=lambda p: str(p.relative_to(PROJECT_DIR)))
def test_windows_scripts_are_pure_ascii(path: Path):
    """Windows PowerShell 5.1 liest UTF-8 ohne BOM als ANSI – Umlaute würden verstümmelt."""
    data = path.read_bytes()
    bad = [i for i, b in enumerate(data) if b > 127]
    if bad:
        line = data[: bad[0]].count(b"\n") + 1
        pytest.fail(f"{path.name}: Nicht-ASCII-Zeichen in Zeile {line}")
    assert b"\x00" not in data


def test_windows_scripts_found():
    rel = {str(p.relative_to(PROJECT_DIR)).replace("\\", "/") for p in _script_files(".ps1", ".cmd")}
    for name in ("Install.cmd", "Uninstall.cmd", "scripts/install.ps1", "scripts/uninstall.ps1",
                 "scripts/installer-lib.ps1", "scripts/stop.ps1", "scripts/install_autostart.ps1"):
        assert name in rel, name


_PS7_OPERATORS = re.compile(r"&&|\|\||\?\?|\$\{[^}]*\}\?[.\[]")


@pytest.mark.parametrize("path", _script_files(".ps1"), ids=lambda p: str(p.relative_to(PROJECT_DIR)))
def test_no_ps7_pipeline_or_null_operators(path: Path):
    """Grobe Prüfung ohne pwsh (die genaue Token-Prüfung macht tests/ps/syntax.tests.ps1)."""
    for number, line in enumerate(path.read_text(encoding="ascii").splitlines(), start=1):
        code = line.split("#", 1)[0] if line.lstrip().startswith("#") else line
        assert not _PS7_OPERATORS.search(code), f"{path.name}:{number}: PS7-Operator: {line.strip()}"


def test_gitattributes_forces_crlf():
    text = (PROJECT_DIR / ".gitattributes").read_text(encoding="utf-8")
    rules = {line.split()[0]: line.split()[1:] for line in text.splitlines() if line.strip() and not line.startswith("#")}
    for pattern in ("*.ps1", "*.cmd"):
        assert pattern in rules, pattern
        assert "eol=crlf" in rules[pattern], rules[pattern]


def test_cmd_wrappers_call_powershell_correctly():
    install = (PROJECT_DIR / "Install.cmd").read_text(encoding="ascii")
    assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\\install.ps1" %*' in install
    uninstall = (PROJECT_DIR / "Uninstall.cmd").read_text(encoding="ascii")
    assert 'set "JARVIS_UNINSTALL_PS1=%~dp0scripts\\uninstall.ps1"' in uninstall
    assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "!JARVIS_UNINSTALL_PS1!" %*' in uninstall
    for text in (install, uninstall):
        assert text.lstrip().lower().startswith("@echo off")
        assert '"%JARVIS_NOPAUSE%"=="1"' in text or '"!JARVIS_NOPAUSE!"=="1"' in text
        assert "exit /b" in text
        # Sprungmarken (:label) sind bei LF-Zeilenenden in cmd.exe unzuverlässig.
        assert not re.search(r"(?m)^\s*:", text)
    # Uninstall.cmd verlässt den Installationsordner und endet mit dem Block (die Datei wird gelöscht).
    assert 'cd /d "%TEMP%"' in uninstall
    assert uninstall.rstrip().endswith(")")


def test_installer_never_touches_system_settings():
    """Firewall, Aufgabenplanung, Registry & Co. werden nie verändert (nur Befehle angezeigt)."""
    forbidden = re.compile(
        r"^\s*(?!#).*\b(Register-ScheduledTask|schtasks|Set-NetFirewall\w*|netsh|Set-ItemProperty|"
        r"New-ItemProperty|reg(\.exe)?\s+add|Start-Process\b.*-Verb\s+RunAs|ollama\s+pull|Set-ExecutionPolicy)\b",
        re.I | re.M,
    )
    for path in _script_files(".ps1", ".cmd"):
        if path.parent.name == "ps":
            continue
        text = path.read_text(encoding="ascii")
        match = forbidden.search(text)
        assert not match, f"{path.name}: {match.group(0).strip()}"
        # New-NetFirewallRule darf nur als Text (zum Anzeigen) vorkommen, nie als Aufruf.
        for line in text.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("New-NetFirewallRule"), f"{path.name}: {stripped}"


def test_uv_download_only_with_documented_command():
    lib = (PROJECT_DIR / "scripts" / "installer-lib.ps1").read_text(encoding="ascii")
    assert 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"' in lib
    assert "UV_NO_MODIFY_PATH" in lib
    # Der Download passiert nur in Install-JarvisUv, und das wird nur nach Zustimmung aufgerufen.
    calls = [line for line in lib.splitlines() if "Install-JarvisUv" in line and not line.lstrip().startswith("#")]
    assert any(line.strip().startswith("function Install-JarvisUv") for line in calls)
    assert sum("= Install-JarvisUv" in line for line in calls) == 1
