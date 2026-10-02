"""Baut das Release-Paket dist/JARVIS-Setup-<version>.zip für den Windows-Installer.

Aufruf (im Ordner jarvis):
    python scripts/build_installer.py [--src ORDNER] [--out ORDNER]

Das ZIP enthält genau einen Ordner JARVIS-Setup-<version>/ mit Install.cmd, Uninstall.cmd,
README.md, config.example.yaml, pyproject.toml, uv.lock, requirements.txt sowie app/, web/,
launcher/ und scripts/. Nie enthalten: tests/, .venv, state/, dist/, config.yaml, config.yaml.bak,
secrets.yaml, __pycache__, *.pyc, .pytest_cache.

- .ps1/.cmd/.bat bekommen im ZIP CRLF-Zeilenenden und müssen reines ASCII sein
  (Windows PowerShell 5.1 liest UTF-8 ohne BOM als ANSI).
- Reproduzierbar: sortierte Einträge, feste Zeitstempel (1980-01-01 bzw. SOURCE_DATE_EPOCH),
  feste Dateiattribute, ZIP_DEFLATED.
- Neben dem ZIP entsteht <zip>.sha256 (Format wie sha256sum).

Exit-Codes: 0 = gebaut, 2 = Fehler (Meldung auf stderr, kein Stacktrace).
Nur Standardbibliothek.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
import tomllib
import zipfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent

# Was ins Paket kommt (relativ zum Quellordner, immer mit "/").
INCLUDE_FILES = (
    "Install.cmd",
    "Uninstall.cmd",
    "README.md",
    "config.example.yaml",
    "pyproject.toml",
    "uv.lock",
    "requirements.txt",
)
INCLUDE_DIRS = ("app", "web", "launcher", "scripts")

# Ohne diese Dateien ist das Paket unbrauchbar.
REQUIRED_FILES = (
    "Install.cmd",
    "Uninstall.cmd",
    "scripts/install.ps1",
    "scripts/uninstall.ps1",
    "scripts/installer-lib.ps1",
    "app/setup_wizard.py",
    "requirements.txt",
    "uv.lock",
    "pyproject.toml",
    "config.example.yaml",
    "README.md",
    "web/index.html",
    "launcher/wake.html",
)

# Werden innerhalb der Include-Ordner stillschweigend übersprungen (Build-/Editor-Reste).
SKIP_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".venv", ".git", "node_modules",
                            ".mypy_cache", ".ruff_cache"})
SKIP_FILE_SUFFIXES = (".pyc", ".pyo")
SKIP_FILE_NAMES = frozenset({".ds_store", "thumbs.db"})

# Dürfen nie ins Paket. Taucht so etwas in einem Include-Ordner auf, bricht der Build ab.
# config.yaml.bak = Sicherung des Einrichtungsassistenten (enthält die Einstellungen des Benutzers).
FORBIDDEN_FILE_NAMES = frozenset({"config.yaml", "config.yaml.bak", "secrets.yaml", ".jarvis-install.json"})
FORBIDDEN_DIR_NAMES = frozenset({"state"})

WINDOWS_SCRIPT_SUFFIXES = (".ps1", ".cmd", ".bat")

# api_token: <32+ Zeichen> – sieht nach einem echten Token aus (Platzhalter sind kürzer).
TOKEN_PATTERN = re.compile(rb"""api_token\s*:\s*["']?[A-Za-z0-9_\-+/=.]{32,}""")

VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]*$")

DEFAULT_DATE_TIME = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o100644  # reguläre Datei, rw-r--r--


class BuildError(Exception):
    """Fehler mit verständlicher Meldung für den Benutzer."""


def read_version(src: Path) -> str:
    pyproject = src / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BuildError(f"pyproject.toml fehlt in {src}.") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise BuildError(f"pyproject.toml ist nicht lesbar: {exc}") from None
    project = data.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not VERSION_PATTERN.match(version):
        raise BuildError("In pyproject.toml fehlt unter [project] eine gültige 'version' (z. B. \"0.2.0\").")
    return version


def zip_date_time() -> tuple[int, int, int, int, int, int]:
    raw = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if not raw:
        return DEFAULT_DATE_TIME
    try:
        epoch = int(raw)
    except ValueError:
        raise BuildError(
            f"SOURCE_DATE_EPOCH muss eine ganze Zahl (Sekunden seit 1970) sein, nicht {raw!r}."
        ) from None
    stamp = time.gmtime(max(epoch, 0))
    if stamp.tm_year < 1980:
        return DEFAULT_DATE_TIME
    if stamp.tm_year > 2107:
        raise BuildError("SOURCE_DATE_EPOCH liegt nach 2107 – das kann ZIP nicht speichern.")
    return (stamp.tm_year, stamp.tm_mon, stamp.tm_mday, stamp.tm_hour, stamp.tm_min, stamp.tm_sec)


def _check_entry(path: Path, rel: str) -> None:
    if path.is_symlink():
        raise BuildError(
            f"Symbolischer Link wird nicht verpackt: {rel} – bitte durch eine echte Datei ersetzen."
        )
    parts = rel.lower().split("/")
    if parts[-1] in FORBIDDEN_FILE_NAMES or any(p in FORBIDDEN_DIR_NAMES for p in parts[:-1]):
        raise BuildError(
            f"Verbotene Datei im Paket: {rel} – config.yaml(.bak), secrets.yaml und state/ gehören dem Benutzer "
            "und dürfen nie ausgeliefert werden. Bitte entfernen."
        )


def _walk(src: Path, top: str, out: list[str]) -> None:
    base = src / top
    if base.is_symlink():
        raise BuildError(
            f"Symbolischer Link wird nicht verpackt: {top}/ – bitte durch einen echten Ordner ersetzen."
        )
    if not base.is_dir():
        return
    stack = [base]
    while stack:
        current = stack.pop()
        for entry in sorted(current.iterdir(), key=lambda p: p.name):
            rel = entry.relative_to(src).as_posix()
            name = entry.name.lower()
            if entry.is_dir() and not entry.is_symlink():
                if entry.name in SKIP_DIR_NAMES:
                    continue
                if name in FORBIDDEN_DIR_NAMES:
                    raise BuildError(
                        f"Verbotener Ordner im Paket: {rel}/ – state/ enthält Laufzeitdaten des Benutzers "
                        "und darf nie ausgeliefert werden. Bitte entfernen."
                    )
                stack.append(entry)
                continue
            if name in SKIP_FILE_NAMES or name.endswith(SKIP_FILE_SUFFIXES):
                continue
            _check_entry(entry, rel)
            if not entry.is_file():
                raise BuildError(f"Keine normale Datei, wird nicht verpackt: {rel}")
            out.append(rel)


def collect_files(src: Path) -> list[str]:
    """Liste aller zu verpackenden Dateien (relativ, mit "/", sortiert). Prüft Pflicht- und Verbotsliste."""
    missing = [rel for rel in REQUIRED_FILES if not (src / rel).is_file()]
    if missing:
        raise BuildError(
            "Pflichtdateien fehlen im Quellordner " + str(src) + ":\n  " + "\n  ".join(missing)
        )
    files: list[str] = []
    for rel in INCLUDE_FILES:
        path = src / rel
        if path.exists() or path.is_symlink():
            _check_entry(path, rel)
            if not path.is_file():
                raise BuildError(f"Keine normale Datei, wird nicht verpackt: {rel}")
            files.append(rel)
    for top in INCLUDE_DIRS:
        _walk(src, top, files)
    files.sort()
    seen: dict[str, str] = {}
    for rel in files:
        other = seen.setdefault(rel.lower(), rel)
        if other != rel:
            raise BuildError(
                f"Dateinamen unterscheiden sich nur in Groß-/Kleinschreibung (Windows!): {other} / {rel}"
            )
    return files


def prepare_content(rel: str, data: bytes) -> bytes:
    """Prüft den Inhalt und wandelt Windows-Skripte nach CRLF."""
    if TOKEN_PATTERN.search(data):
        raise BuildError(f"{rel} enthält anscheinend ein echtes API-Token (api_token: ...). Bitte entfernen.")
    if rel.lower().endswith(WINDOWS_SCRIPT_SUFFIXES):
        if any(byte > 0x7F for byte in data):
            raise BuildError(
                f"{rel} enthält Nicht-ASCII-Zeichen (Umlaute/BOM). PowerShell- und .cmd-Dateien müssen "
                "reines ASCII sein (ae/oe/ue/ss oder [char]0x00E4 verwenden)."
            )
        data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return data


def build(src: Path, out_dir: Path) -> tuple[Path, str, int]:
    src = src.resolve()
    if not src.is_dir():
        raise BuildError(f"Quellordner nicht gefunden: {src}")
    out_dir = out_dir.resolve()
    for top in INCLUDE_DIRS:
        if out_dir == (src / top) or (src / top) in out_dir.parents:
            raise BuildError(
                f"Ausgabeordner {out_dir} liegt in {top}/ und würde mitverpackt – bitte einen anderen wählen."
            )

    files = collect_files(src)
    version = read_version(src)
    date_time = zip_date_time()
    top = f"JARVIS-Setup-{version}"

    entries: list[tuple[str, bytes]] = []
    for rel in files:
        try:
            data = (src / rel).read_bytes()
        except OSError as exc:
            raise BuildError(f"{rel} ist nicht lesbar: {exc}") from None
        entries.append((f"{top}/{rel}", prepare_content(rel, data)))

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BuildError(f"Ausgabeordner {out_dir} kann nicht angelegt werden: {exc}") from None
    zip_path = out_dir / f"{top}.zip"
    tmp_path = out_dir / f".{top}.zip.tmp"
    try:
        with zipfile.ZipFile(tmp_path, "w") as archive:
            for name, data in entries:
                info = zipfile.ZipInfo(name, date_time=date_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3  # Unix – feste Attribute unabhängig vom Build-Rechner
                info.external_attr = FILE_MODE << 16
                archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        digest = hashlib.sha256(tmp_path.read_bytes()).hexdigest()
        os.replace(tmp_path, zip_path)
        zip_path.with_name(zip_path.name + ".sha256").write_text(
            f"{digest}  {zip_path.name}\n", encoding="ascii", newline="\n"
        )
    except OSError as exc:
        raise BuildError(f"ZIP konnte nicht geschrieben werden: {exc}") from None
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return zip_path, digest, len(entries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Baut dist/JARVIS-Setup-<version>.zip für den Windows-Installer."
    )
    parser.add_argument("--src", type=Path, default=PROJECT_DIR,
                        help="Projektordner (Standard: Ordner über scripts/)")
    parser.add_argument("--out", type=Path, default=None, help="Ausgabeordner (Standard: <src>/dist)")
    args = parser.parse_args(argv)
    out_dir = args.out if args.out is not None else args.src / "dist"
    try:
        zip_path, digest, count = build(args.src, out_dir)
    except BuildError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 2
    print(f"Paket gebaut: {zip_path} ({count} Dateien)")
    print(f"SHA-256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
