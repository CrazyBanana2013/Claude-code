"""Tests für scripts/build_installer.py (Release-ZIP) und requirements.txt."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_DIR / "scripts" / "build_installer.py"

spec = importlib.util.spec_from_file_location("build_installer", SCRIPT)
bi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bi)

VERSION = "1.2.3"
TOP = f"JARVIS-Setup-{VERSION}"

# Inhalt des Fake-Quellbaums: relativer Pfad -> Bytes
FAKE_PACKAGED = {
    "Install.cmd": b"@echo off\nrem Installer\npowershell.exe -File \"%~dp0scripts\\install.ps1\" %*\n",
    "Uninstall.cmd": b"@echo off\r\nrem schon CRLF\r\nexit /b 0\r\n",
    "README.md": b"# JARVIS\n\nUmlaute erlaubt: \xc3\xa4\xc3\xb6\xc3\xbc\n",
    "config.example.yaml": b"server:\n  port: 8765\napi_token: TODO\n",
    "pyproject.toml": f'[project]\nname = "jarvis"\nversion = "{VERSION}"\n'.encode(),
    "uv.lock": b"version = 1\n",
    "requirements.txt": b"fastapi==1.0 \\\n    --hash=sha256:00\n",
    "app/__init__.py": b"",
    "app/__main__.py": b"print('start')\n",
    "app/setup_wizard.py": b"# Wizard\r\nprint('crlf bleibt')\r\n",
    "app/tools/__init__.py": b"",
    "app/tools/led.py": b"LED = 1\n",
    "web/index.html": b"<!doctype html>\n<title>JARVIS</title>\n",
    "web/sub/style.css": b"body{}\n",
    "launcher/wake.html": b"<!doctype html>\n",
    "scripts/install.ps1": b"param()\r\nWrite-Host 'a'\nWrite-Host 'b'\r\n",
    "scripts/uninstall.ps1": b"param()\nWrite-Host 'weg'",
    "scripts/installer-lib.ps1": b"function Test-X { return 1 }\n",
    "scripts/start.ps1": b"Write-Host 'start'\n",
    "scripts/probe.py": b"print('probe')\n",
}

# Darf nie ins Paket, liegt aber in einem normalen Entwicklungs-Checkout herum.
FAKE_IGNORED = {
    "config.yaml": b"server:\n  port: 1\n",
    "secrets.yaml": b"api_token: " + b"x" * 43 + b"\n",
    "state/server.pid": b'{"pid": 1}\n',
    "state/logs/server.log": b"log\n",
    "tests/test_x.py": b"def test(): pass\n",
    "tests/ps/Helper.ps1": b"Write-Host 'test'\n",
    ".venv/bin/python": b"#!/bin/sh\n",
    ".venv/Lib/site-packages/x.py": b"x = 1\n",
    "dist/JARVIS-Setup-0.0.1.zip": b"PK\x05\x06" + b"\x00" * 18,
    ".pytest_cache/v/cache/lastfailed": b"{}\n",
    ".gitignore": b"config.yaml\n",
    ".gitattributes": b"*.ps1 text eol=crlf\n",
    ".jarvis-install.json": b"{}\n",
    "notes.txt": b"nicht in der Include-Liste\n",
    "app/__pycache__/main.cpython-311.pyc": b"\x00\x01",
    "app/old.pyc": b"\x00\x01",
    "scripts/__pycache__/probe.cpython-311.pyc": b"\x00\x01",
    "app/.pytest_cache/x": b"x\n",
    "web/.DS_Store": b"\x00",
}


def write_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


@pytest.fixture
def src(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    write_tree(root, FAKE_PACKAGED)
    write_tree(root, FAKE_IGNORED)
    return root


@pytest.fixture(autouse=True)
def _no_source_date_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)


def run_build(src: Path, out: Path) -> int:
    return bi.main(["--src", str(src), "--out", str(out)])


def zip_path(out: Path, version: str = VERSION) -> Path:
    return out / f"JARVIS-Setup-{version}.zip"


def names(zp: Path) -> list[str]:
    with zipfile.ZipFile(zp) as archive:
        return archive.namelist()


def read(zp: Path, rel: str, top: str = TOP) -> bytes:
    with zipfile.ZipFile(zp) as archive:
        return archive.read(f"{top}/{rel}")


# ---------------------------------------------------------------------------------------------
# Inhalt: Include/Exclude
# ---------------------------------------------------------------------------------------------

def test_include_exclude_exact(src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert run_build(src, out) == 0
    expected = sorted(f"{TOP}/{rel}" for rel in FAKE_PACKAGED)
    assert names(zip_path(out)) == expected


def test_entries_sorted_single_top_folder_and_version(src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert run_build(src, out) == 0
    zp = zip_path(out)
    assert zp.name == "JARVIS-Setup-1.2.3.zip"
    entries = names(zp)
    assert entries == sorted(entries)
    assert {e.split("/", 1)[0] for e in entries} == {TOP}
    assert not any(e.endswith("/") for e in entries)


def test_default_out_is_src_dist(src: Path) -> None:
    assert bi.main(["--src", str(src)]) == 0
    assert zip_path(src / "dist").is_file()
    # Das alte ZIP in dist/ wurde nicht mitverpackt.
    assert not any("dist/" in n for n in names(zip_path(src / "dist")))


def test_entry_attributes_are_fixed(src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert run_build(src, out) == 0
    with zipfile.ZipFile(zip_path(out)) as archive:
        assert archive.testzip() is None
        for info in archive.infolist():
            assert info.compress_type == zipfile.ZIP_DEFLATED
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.create_system == 3
            assert info.external_attr == 0o100644 << 16


# ---------------------------------------------------------------------------------------------
# Verbotene und fehlende Dateien
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "app/config.yaml",
    "app/CONFIG.yaml.bak",
    "scripts/secrets.yaml",
    "launcher/Secrets.YAML",
    "web/state/scripts.json",
    "app/tools/state/server.pid",
    "scripts/.jarvis-install.json",
    # Kopien/Sicherungen der Benutzerdaten und typische Reste (oft mit echten Adressen oder Token):
    "scripts/config.yaml.orig",
    "app/config.yaml.bak2",
    "scripts/secrets.yml.txt",
    "web/.config.yaml.x1y2.tmp",
    "launcher/settings.json.bak",
    "web/notes.old",
    "app/tools/led.py~",
    "scripts/.jarvis-install.json.tmp",
])
def test_forbidden_file_refused(src: Path, tmp_path: Path, rel: str, capsys: pytest.CaptureFixture[str]) -> None:
    write_tree(src, {rel: b"geheim\n"})
    out = tmp_path / "out"
    assert run_build(src, out) == 2
    err = capsys.readouterr().err
    assert "Verboten" in err
    assert not zip_path(out).exists()


def test_token_in_file_refused(src: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_tree(src, {"app/leak.py": b'EXAMPLE = """\napi_token: "' + b"Ab3_-" * 8 + b'"\n"""\n'})
    out = tmp_path / "out"
    assert run_build(src, out) == 2
    err = capsys.readouterr().err
    assert "app/leak.py" in err and "Token" in err
    assert not zip_path(out).exists()


TOKEN_43 = b"EYdA3b7kLCkE9mpeI4fNVpH-Nee63J_jjbXZcJu5i_E"  # Form wie secrets.token_urlsafe(32), kein echter Token


@pytest.mark.parametrize("content", [
    b'{"api_token": "' + TOKEN_43 + b'"}\n',            # JSON (z. B. gespeicherte Einstellungen)
    b"api_token = '" + TOKEN_43 + b"'\n",               # Python/INI mit '='
    b"API_TOKEN: " + TOKEN_43 + b"\n",                   # andere Schreibweise
    b'const t = {apiToken: "' + TOKEN_43 + b'"};\n',     # JavaScript
    b"api-token=" + TOKEN_43 + b"\n",
])
def test_token_in_other_notations_refused(src: Path, tmp_path: Path, content: bytes,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    write_tree(src, {"launcher/settings.json": content})
    out = tmp_path / "out"
    assert run_build(src, out) == 2
    assert "launcher/settings.json" in capsys.readouterr().err
    assert not zip_path(out).exists()


def test_short_token_placeholder_allowed(src: Path, tmp_path: Path) -> None:
    write_tree(src, {"app/doc.py": b"# api_token: <dein Token>\n# api_token: TODO\n"
                                   b'yaml.safe_dump({"api_token": token})\n'
                                   b'API_TOKEN = "TODO"\n'})
    assert run_build(src, tmp_path / "out") == 0


@pytest.mark.parametrize("rel", bi.REQUIRED_FILES)
def test_missing_required_file(src: Path, tmp_path: Path, rel: str, capsys: pytest.CaptureFixture[str]) -> None:
    (src / rel).unlink()
    out = tmp_path / "out"
    assert run_build(src, out) == 2
    err = capsys.readouterr().err
    assert "Pflichtdateien fehlen" in err and rel in err
    assert not out.exists() or not any(out.iterdir())


def test_required_list_matches_contract() -> None:
    assert set(bi.REQUIRED_FILES) == {
        "Install.cmd", "Uninstall.cmd", "scripts/install.ps1", "scripts/uninstall.ps1",
        "scripts/installer-lib.ps1", "app/setup_wizard.py", "requirements.txt", "uv.lock",
        "pyproject.toml", "config.example.yaml", "README.md", "web/index.html", "launcher/wake.html",
    }


def test_non_ascii_powershell_refused(src: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_tree(src, {"scripts/start.ps1": "Write-Host 'Grüße'\n".encode("utf-8")})
    assert run_build(src, tmp_path / "out") == 2
    assert "scripts/start.ps1" in capsys.readouterr().err


def test_bom_in_cmd_refused(src: Path, tmp_path: Path) -> None:
    write_tree(src, {"Install.cmd": b"\xef\xbb\xbf@echo off\n"})
    assert run_build(src, tmp_path / "out") == 2


@pytest.mark.parametrize("version", ["", "1.0/../x", "1 0"])
def test_invalid_version_refused(src: Path, tmp_path: Path, version: str) -> None:
    write_tree(src, {"pyproject.toml": f'[project]\nname = "jarvis"\nversion = "{version}"\n'.encode()})
    assert run_build(src, tmp_path / "out") == 2


def test_missing_version_refused(src: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_tree(src, {"pyproject.toml": b'[project]\nname = "jarvis"\n'})
    assert run_build(src, tmp_path / "out") == 2
    assert "version" in capsys.readouterr().err


def test_out_dir_inside_package_refused(src: Path) -> None:
    assert run_build(src, src / "app" / "dist") == 2
    assert not (src / "app" / "dist").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Symlinks brauchen unter Windows Sonderrechte")
def test_symlink_refused(src: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (src / "app" / "link.yaml").symlink_to(src / "config.yaml")
    assert run_build(src, tmp_path / "out") == 2
    assert "Symbolischer Link" in capsys.readouterr().err


def test_case_collision_refused(src: Path, tmp_path: Path) -> None:
    write_tree(src, {"web/Index.html": b"<!doctype html>\n"})
    if not (src / "web" / "index.html").read_bytes().startswith(b"<!doctype html>\n<title>"):
        pytest.skip("Dateisystem unterscheidet keine Groß-/Kleinschreibung")
    assert run_build(src, tmp_path / "out") == 2


# ---------------------------------------------------------------------------------------------
# Zeilenenden
# ---------------------------------------------------------------------------------------------

def test_crlf_for_windows_scripts(src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert run_build(src, out) == 0
    zp = zip_path(out)
    for rel in ("Install.cmd", "Uninstall.cmd", "scripts/install.ps1", "scripts/uninstall.ps1",
                "scripts/installer-lib.ps1", "scripts/start.ps1"):
        data = read(zp, rel)
        assert b"\r\r" not in data, rel
        assert data.count(b"\n") == data.count(b"\r\n"), rel
        assert data.replace(b"\r\n", b"\n") == FAKE_PACKAGED[rel].replace(b"\r\n", b"\n"), rel
    assert read(zp, "Install.cmd").endswith(b"%*\r\n")
    assert read(zp, "Uninstall.cmd") == FAKE_PACKAGED["Uninstall.cmd"]  # schon CRLF, nicht verdoppelt
    assert read(zp, "scripts/install.ps1") == b"param()\r\nWrite-Host 'a'\r\nWrite-Host 'b'\r\n"
    assert read(zp, "scripts/uninstall.ps1") == b"param()\r\nWrite-Host 'weg'"


def test_other_text_files_get_lf(src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert run_build(src, out) == 0
    zp = zip_path(out)
    for rel, data in FAKE_PACKAGED.items():
        if not rel.endswith((".ps1", ".cmd")):
            assert read(zp, rel) == data.replace(b"\r\n", b"\n"), rel
    assert read(zp, "app/setup_wizard.py") == b"# Wizard\nprint('crlf bleibt')\n"  # war CRLF im Checkout


def test_binary_files_unchanged(src: Path, tmp_path: Path) -> None:
    icon = b"\x89PNG\r\n\x1a\n\x00\x00\r\n"
    write_tree(src, {"web/icon.png": icon})
    out = tmp_path / "out"
    assert run_build(src, out) == 0
    assert read(zip_path(out), "web/icon.png") == icon


def test_same_zip_from_lf_and_crlf_checkout(tmp_path: Path) -> None:
    """Git für Windows checkt mit core.autocrlf=true CRLF aus – das ZIP muss trotzdem gleich sein."""
    lf, crlf = tmp_path / "lf", tmp_path / "crlf"
    write_tree(lf, {rel: data.replace(b"\r\n", b"\n") for rel, data in FAKE_PACKAGED.items()})
    write_tree(crlf, {rel: data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n") for rel, data in FAKE_PACKAGED.items()})
    assert run_build(lf, tmp_path / "out-lf") == 0
    assert run_build(crlf, tmp_path / "out-crlf") == 0
    assert zip_path(tmp_path / "out-lf").read_bytes() == zip_path(tmp_path / "out-crlf").read_bytes()


# ---------------------------------------------------------------------------------------------
# Reproduzierbarkeit, Prüfsumme
# ---------------------------------------------------------------------------------------------

def test_deterministic(src: Path, tmp_path: Path) -> None:
    out1, out2 = tmp_path / "out1", tmp_path / "out2"
    assert run_build(src, out1) == 0
    for path in src.rglob("*"):
        if path.is_file():
            os.utime(path, (1_700_000_000, 1_700_000_000))
    assert run_build(src, out2) == 0
    assert zip_path(out1).read_bytes() == zip_path(out2).read_bytes()
    # Neubau in denselben Ordner ersetzt das ZIP mit identischem Inhalt.
    first = zip_path(out1).read_bytes()
    assert run_build(src, out1) == 0
    assert zip_path(out1).read_bytes() == first
    assert sorted(p.name for p in out1.iterdir()) == [f"{TOP}.zip", f"{TOP}.zip.sha256"]


def test_source_date_epoch(src: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1767225600")  # 2026-01-01 00:00:00 UTC
    out = tmp_path / "out"
    assert run_build(src, out) == 0
    with zipfile.ZipFile(zip_path(out)) as archive:
        assert {i.date_time for i in archive.infolist()} == {(2026, 1, 1, 0, 0, 0)}
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "100")  # vor 1980 -> 1980
    assert bi.zip_date_time() == (1980, 1, 1, 0, 0, 0)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "gestern")
    assert run_build(src, tmp_path / "out2") == 2
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "4354819199")  # 2107-12-31 23:59:59 = letzter ZIP-Zeitpunkt
    assert bi.zip_date_time()[0] == 2107


@pytest.mark.parametrize("value", ["4354819200", "1790000000000", str(10**17), str(10**30)])
def test_source_date_epoch_out_of_range(src: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str], value: str) -> None:
    """Zu große Werte (z. B. Millisekunden) -> Meldung statt Stacktrace (OSError/OverflowError in gmtime)."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", value)
    assert run_build(src, tmp_path / "out") == 2
    assert "SOURCE_DATE_EPOCH" in capsys.readouterr().err


def test_sha256_file_and_output(src: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    assert run_build(src, out) == 0
    zp = zip_path(out)
    digest = hashlib.sha256(zp.read_bytes()).hexdigest()
    sha_file = out / f"{TOP}.zip.sha256"
    assert sha_file.read_bytes() == f"{digest}  {TOP}.zip\n".encode("ascii")
    stdout = capsys.readouterr().out
    assert str(zp) in stdout
    assert digest in stdout


def test_cli_subprocess(src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    ok = subprocess.run([sys.executable, str(SCRIPT), "--src", str(src), "--out", str(out)],
                        capture_output=True, text=True, timeout=120)
    assert ok.returncode == 0, ok.stderr
    assert "SHA-256:" in ok.stdout
    (src / "README.md").unlink()
    bad = subprocess.run([sys.executable, str(SCRIPT), "--src", str(src), "--out", str(out)],
                         capture_output=True, text=True, timeout=120)
    assert bad.returncode == 2
    assert "README.md" in bad.stderr
    assert "Traceback" not in bad.stderr


# ---------------------------------------------------------------------------------------------
# Echter Projektbaum
# ---------------------------------------------------------------------------------------------

def _expected_real_files() -> list[str]:
    expected = [rel for rel in bi.INCLUDE_FILES if (PROJECT_DIR / rel).is_file()]
    for top in bi.INCLUDE_DIRS:
        for path in (PROJECT_DIR / top).rglob("*"):
            rel = path.relative_to(PROJECT_DIR).as_posix()
            if not path.is_file() or "__pycache__" in rel.split("/") or rel.endswith((".pyc", ".pyo")):
                continue
            expected.append(rel)
    return sorted(expected)


def test_real_tree(tmp_path: Path) -> None:
    missing = [rel for rel in bi.REQUIRED_FILES if not (PROJECT_DIR / rel).is_file()]
    if missing:
        pytest.skip("Pflichtdateien im echten Projekt fehlen noch: " + ", ".join(missing))
    out = tmp_path / "out"
    assert bi.main(["--out", str(out)]) == 0
    version = bi.read_version(PROJECT_DIR)
    top = f"JARVIS-Setup-{version}"
    zp = out / f"{top}.zip"
    assert zp.is_file()
    entries = names(zp)
    rels = [e.split("/", 1)[1] for e in entries]
    assert all(e.startswith(top + "/") for e in entries)
    assert rels == _expected_real_files()
    for rel in bi.REQUIRED_FILES:
        assert rel in rels
    for rel in rels:
        parts = rel.split("/")
        assert parts[0] not in {"tests", ".venv", "state", "dist", ".pytest_cache"}, rel
        assert parts[-1] not in {"config.yaml", "config.yaml.bak", "secrets.yaml", ".jarvis-install.json"}, rel
        assert "__pycache__" not in parts and not rel.endswith(".pyc"), rel
    with zipfile.ZipFile(zp) as archive:
        for rel in rels:
            data = archive.read(f"{top}/{rel}")
            source = (PROJECT_DIR / rel).read_bytes()
            if rel.endswith((".ps1", ".cmd")):
                assert data.isascii(), rel
                assert b"\r\r" not in data and data.count(b"\n") == data.count(b"\r\n"), rel
                assert data.replace(b"\r\n", b"\n") == source.replace(b"\r\n", b"\n"), rel
            elif rel.lower().endswith(bi.TEXT_SUFFIXES):
                assert data == source.replace(b"\r\n", b"\n"), rel
            else:
                assert data == source, rel
    assert (out / f"{top}.zip.sha256").read_text(encoding="ascii").split() == [
        hashlib.sha256(zp.read_bytes()).hexdigest(), zp.name]


PARSE_CHECK_PS1 = r"""
$failed = 0
foreach ($file in $args) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$errors)
    foreach ($e in $errors) {
        Write-Output ('{0}:{1}: {2}' -f $file, $e.Extent.StartLineNumber, $e.Message)
        $failed = 1
    }
}
exit $failed
"""


def test_packaged_powershell_parses(tmp_path: Path) -> None:
    """Die CRLF-Fassungen aus dem ZIP müssen für PowerShell fehlerfrei parsebar sein."""
    pwsh = os.environ.get("JARVIS_PWSH") or shutil.which("pwsh")
    if not pwsh or not Path(pwsh).is_file():
        pytest.skip("pwsh nicht gefunden (JARVIS_PWSH setzen)")
    missing = [rel for rel in bi.REQUIRED_FILES if not (PROJECT_DIR / rel).is_file()]
    if missing:
        pytest.skip("Pflichtdateien im echten Projekt fehlen noch: " + ", ".join(missing))
    out = tmp_path / "out"
    assert bi.main(["--out", str(out)]) == 0
    extracted = tmp_path / "x"
    zp = next(out.glob("*.zip"))
    with zipfile.ZipFile(zp) as archive:
        archive.extractall(extracted)
    scripts = sorted(str(p) for p in extracted.rglob("*.ps1"))
    assert scripts
    checker = tmp_path / "check.ps1"
    checker.write_text(PARSE_CHECK_PS1, encoding="ascii")
    result = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(checker), *scripts],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------------------------
# requirements.txt
# ---------------------------------------------------------------------------------------------

UV_EXPORT = ["export", "--frozen", "--no-dev", "--format", "requirements-txt", "-o", "requirements.txt"]


def test_requirements_in_sync_with_lock(tmp_path: Path) -> None:
    uv = os.environ.get("JARVIS_UV") or shutil.which("uv")
    if not uv:
        pytest.skip("uv nicht installiert")
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(PROJECT_DIR / name, tmp_path / name)
    result = subprocess.run([uv, *UV_EXPORT], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    fresh = (tmp_path / "requirements.txt").read_text(encoding="utf-8")
    committed = (PROJECT_DIR / "requirements.txt").read_text(encoding="utf-8")
    assert committed == fresh, (
        "requirements.txt passt nicht zu uv.lock. Neu erzeugen mit: "
        "uv export --frozen --no-dev --format requirements-txt -o requirements.txt"
    )


def test_requirements_runtime_only_with_hashes() -> None:
    text = (PROJECT_DIR / "requirements.txt").read_text(encoding="utf-8")
    assert text.startswith("# This file was autogenerated by uv")
    pins = {}
    current = None
    for line in text.splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            spec_part = line.split(";", 1)[0].rstrip(" \\")
            name, _, version = spec_part.partition("==")
            assert version, f"nicht exakt gepinnt: {line}"
            current = name.lower()
            pins[current] = []
        elif line.strip().startswith("--hash=sha256:"):
            assert current is not None
            pins[current].append(line.strip())
    for direct in ("fastapi", "uvicorn", "httpx", "pydantic", "pyyaml", "psutil"):
        assert direct in pins, direct
    for dev_only in ("pytest", "pluggy", "iniconfig", "pygments", "packaging"):
        assert dev_only not in pins, dev_only
    assert all(hashes for hashes in pins.values()), "jede Abhängigkeit braucht --hash (pip --require-hashes)"
