"""README stimmt mit dem Verhalten überein (Meldungen, Grenzen, Bedienung) – Funde aus dem Review der PC-Steuerung."""

import re

from app import llm
from app.config import PROJECT_DIR
from app.tools import desktop

README = (PROJECT_DIR / "README.md").read_text(encoding="utf-8")


def section(number: int) -> str:
    match = re.search(rf"^## {number}\. .*?(?=^## )", README, re.M | re.S)
    assert match, f"Abschnitt {number} fehlt"
    return match.group(0)


def troubleshooting_row(text: str) -> str:
    rows = [line for line in README.splitlines() if line.startswith("|") and text in line]
    assert rows, text
    return rows[0]


def test_quoted_messages_match_the_code():
    assert desktop.MEDIA_LOCKED in README
    assert llm.BLOCKED_MESSAGE.split(" – ")[0] in README
    assert llm.URL_NOT_NAMED_MESSAGE.split(". ")[0] in README
    assert "wartet in der Taskleiste (blinkt)" in README
    assert "hatte kein Fenster zum Schließen" in README


def test_worst_case_names_everything_an_injected_model_could_do():
    s9, s10 = section(9), section(10)
    for part in ("Skripte", "Licht", "100 %", "Bestätigen"):
        assert part in s9, part
    assert "Musik pausiert, ein Programm aus deiner Liste startet, der PC sperrt" not in s9  # alte, zu knappe Liste
    flat10 = " ".join(s10.split())
    assert "jede Aktion" in flat10 and "erscheint also kein Bestätigen-Dialog" in flat10


def test_allowlist_side_effect_is_documented_where_fritzbox_is_suggested():
    assert "NUR noch die dort eingetragenen Domains" in section(9)
    assert "NUR noch die eingetragenen Domains" in troubleshooting_row("fritz.box")


def test_focus_limits_are_described_honestly():
    s9 = section(9)
    assert "fast immer" in s9 and "Taskleiste" in s9


def test_tailscale_serve_section_matches_lockout_and_setup():
    s12 = section(12)
    assert "auch die mit dem richtigen Token" in s12 and "verlängern" in s12
    assert "kommt immer durch" not in s12  # die Sperre gilt auch für den richtigen Token
    assert "server.port" in s12 and "JARVIS-Adresse" in s12 and "wake" in s12
    assert "X-Forwarded-For" in s12


def test_voice_hints_need_no_uv_in_path_and_name_the_folder():
    s11 = section(11)
    assert "-InstallVoice" in s11 and 'cd "$env:LOCALAPPDATA\\JARVIS"' in s11
    assert not re.search(r"^uv sync", s11, re.M), "nacktes uv sync – uv liegt nicht im PATH"
    assert "uv sync" not in troubleshooting_row("Paket faster-whisper fehlt")
    assert "Visual C++" in README


def test_mic_discard_and_vision_off_are_documented():
    assert "„Verwerfen“" in section(11) and "Hintergrund" in section(11)
    assert "Ausschalten" in section(10) and "`aus`" in section(10)
    assert "`aus` ab" in section(1)
