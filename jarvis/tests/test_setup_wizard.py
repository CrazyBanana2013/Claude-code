"""Tests für den Einrichtungsassistenten (app/setup_wizard.py) – ohne Terminal und ohne echtes Netz."""

from __future__ import annotations

import builtins
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

from app import setup_wizard as sw
from app.config import PROJECT_DIR, parse_config
from app.ollama_models import OllamaUnavailable, list_models, tool_models, vision_models
from tests.conftest import example_config_dict

EXAMPLE = PROJECT_DIR / "config.example.yaml"
# Schritte 6–8 mit Enter (ohne installiertes Vision-Modell fragt Schritt 6 nichts): PC-Steuerung einschalten,
# kein weiteres Programm, Sprachausgabe, Spracheingabe.
NEW_STEPS = ["", "", "", ""]
OLLAMA_HOST = "127.0.0.1"
WIN_PS1 = "C:\\Users\\Max Müller\\CS2 Skript\\run.ps1"


def example_dump() -> dict:
    return parse_config(example_config_dict()).model_dump()


def read_config(path: Path) -> dict:
    return parse_config(yaml.safe_load(path.read_text(encoding="utf-8"))).model_dump()


class ScriptedIO:
    """Beantwortet Fragen aus einer Liste. Ist die Liste leer, kommt EOFError (wie bei Strg+Z/Dateiende)."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.lines: list[str] = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException) or (isinstance(answer, type) and issubclass(answer, BaseException)):
            raise answer
        return answer

    def say(self, text: str = "") -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class FakeNet:
    """Ersetzt WLED, ESPHome und Ollama. Unbekannte Ziele sind 'nicht erreichbar'."""

    def __init__(self, routes: dict | None = None, ollama_models: list[tuple[str, int, list[str]]] | None = None,
                 host_defaults: dict[str, int] | None = None):
        self.routes = dict(routes or {})
        self.ollama_models = ollama_models
        self.host_defaults = dict(host_defaults or {})
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == OLLAMA_HOST and self.ollama_models is not None:
            if request.method == "GET" and path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": n, "size": s} for n, s, _ in self.ollama_models]})
            if request.method == "POST" and path == "/api/show":
                name = json.loads(request.content)["model"]
                caps = next(c for n, _, c in self.ollama_models if n == name)
                return httpx.Response(200, json={"capabilities": caps})
        route = self.routes.get((host, path))
        if route is None and host in self.host_defaults:
            return httpx.Response(self.host_defaults[host])
        if route is None:
            raise httpx.ConnectError("nicht erreichbar", request=request)
        return route(request) if callable(route) else route

    def assert_read_only(self) -> None:
        for r in self.requests:
            if r.url.host == OLLAMA_HOST:
                assert (r.method, r.url.path) in {("GET", "/api/tags"), ("POST", "/api/show")}, r.url
            else:
                assert r.method == "GET", f"{r.method} {r.url}"
        assert not any("pull" in r.url.path for r in self.requests)


def device_routes() -> dict:
    return {
        ("192.0.2.10", "/json/info"): httpx.Response(200, json={"name": "Schreibtisch", "ver": "0.15.0",
                                                               "leds": {"count": 120}}),
        ("esp.test", "/"): httpx.Response(200, text="<html>ESPHome</html>"),
        ("esp.test", "/sensor/BME280 Temperature"): httpx.Response(
            200, json={"id": "sensor-bme280_temperature", "value": 21.4, "state": "21.4 °C"}),
        ("esp.test", "/sensor/bme280 humidity"): httpx.Response(404),
        ("esp.test", "/sensor/bme280_humidity"): httpx.Response(200, json={"value": 48.0, "state": "48.0 %"}),
    }


def device_net(**kwargs) -> FakeNet:
    return FakeNet(device_routes(), host_defaults={"esp.test": 404}, **kwargs)


def run_wizard(tmp_path, answers, net=None, *, is_file=None, is_dir=None, which=None, env=None, example=EXAMPLE):
    net = net or FakeNet()
    scripted = ScriptedIO(answers)
    with httpx.Client(transport=httpx.MockTransport(net)) as client:
        code = sw.configure(
            tmp_path / "config.yaml",
            example,
            io=scripted,
            http=client,
            is_file=is_file or os.path.isfile,
            is_dir=is_dir or os.path.isdir,
            which=which or (lambda name: None),
            env=env if env is not None else {},
        )
    net.assert_read_only()
    return code, scripted, net


def write_yaml(path: Path, data: dict, comment: str = "") -> bytes:
    raw = (comment + yaml.safe_dump(data, allow_unicode=True, sort_keys=False)).encode("utf-8")
    path.write_bytes(raw)
    return raw


# --------------------------------------------------------------------------------------------
# Interaktiver Durchlauf
# --------------------------------------------------------------------------------------------


def test_full_interactive_run(tmp_path):
    script_dir = tmp_path / "CS2 Skript"
    script_dir.mkdir()
    script = script_dir / "run.ps1"
    script.write_text("Write-Host hallo\n")
    net = device_net(ollama_models=[
        ("gross:8b", 5_000_000_000, ["completion", "tools"]),
        ("klein:1b", 1_000_000_000, ["completion", "tools"]),
        ("ohne-tools", 500_000_000, ["completion"]),
    ])
    answers = [
        "",                     # Geräte testen: ja
        "192.0.2.10",           # WLED
        "esp.test",             # ESPHome-Gerät
        "BME280 Temperature",   # Temperatur
        "bme280 humidity",      # Luftfeuchte (404 → object_id-Form gefunden)
        str(script),            # CS2-Skript
        "",                     # Vorschlag übernehmen
        "",                     # hide_window: nein
        "",                     # Modell: Vorgabe = kleinstes
        "8800",                 # Port
        *NEW_STEPS,             # Bildschirm (kein Vision-Modell), PC-Steuerung, Stimme
        "",                     # Speichern
    ]
    code, scripted, net = run_wizard(tmp_path, answers, net)
    assert code == 0, scripted.text
    assert scripted.answers == []

    cfg = read_config(tmp_path / "config.yaml")
    assert cfg["wled"]["base_url"] == "http://192.0.2.10"
    assert [s["base_url"] for s in cfg["sensors"]] == ["http://esp.test", "http://esp.test"]
    assert [s["entity_id"] for s in cfg["sensors"]] == ["BME280 Temperature", "bme280 humidity"]
    cs2 = cfg["scripts"][0]
    assert cs2["cwd"] == str(script_dir)
    assert cs2["command"] == ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    assert cs2["hide_window"] is False
    assert cfg["llm"]["model"] == "klein:1b"
    assert cfg["server"]["port"] == 8800
    # Alles andere wie im Beispiel
    expected = example_dump()
    for section in ("llm", "server", "wled"):
        for key, value in expected[section].items():
            if (section, key) not in {("llm", "model"), ("server", "port"), ("wled", "base_url")}:
                assert cfg[section][key] == value, (section, key)

    out = scripted.text
    assert "Schreibtisch" in out and "0.15.0" in out and "120 LEDs" in out
    assert "21.4" in out and "object_id 'bme280_humidity'" in out
    assert any("1: klein:1b" in p for p in scripted.prompts)
    assert out.index("klein:1b") < out.index("gross:8b")  # kleinstes zuerst
    assert not (tmp_path / "config.yaml.bak").exists()
    paths = [(r.url.host, r.url.path) for r in net.requests]
    assert ("192.0.2.10", "/json/info") in paths
    # Nach dem Speichern: Hinweis, dass Änderungen erst nach einem Neustart wirken.
    assert out.index("Gespeichert:") < out.index("Damit Änderungen wirken: JARVIS neu starten")


@pytest.mark.parametrize("skip", ["", "-"])
def test_skip_everything_keeps_todo(tmp_path, skip):
    answers = ["n", skip, skip, skip, skip, *NEW_STEPS, ""]  # kein Test, WLED, ESPHome, CS2, Port, ..., Speichern
    code, scripted, net = run_wizard(tmp_path, answers)
    assert code == 0, scripted.text
    assert scripted.answers == []
    assert read_config(tmp_path / "config.yaml") == example_dump()
    # Ollama nicht erreichbar → Hinweis auf Regel-Parser und ollama pull, kein Modell-Prompt
    assert "Regel-Parser" in scripted.text and "ollama pull" in scripted.text
    assert "Install.cmd erneut starten" in scripted.text  # ausführbarer Weg statt "scripts\\pick_model.py"
    # Nichts eingerichtet: '-' und Enter bedeuten dasselbe (bleibt TODO).
    assert "Enter bzw. '-' = überspringen (bleibt TODO)." in scripted.text
    assert "WLED bleibt nicht eingerichtet." in scripted.text
    assert "jetzt nicht eingerichtet" not in scripted.text
    assert all(r.url.host == OLLAMA_HOST for r in net.requests)  # keine Geräteabfrage ohne Erlaubnis


def test_windows_path_with_spaces_and_umlauts_round_trips(tmp_path):
    answers = ["n", "", "", f'"{WIN_PS1}"', "", "j", "", *NEW_STEPS, ""]
    code, scripted, _ = run_wizard(tmp_path, answers, is_file=lambda p: p == WIN_PS1)
    assert code == 0, scripted.text
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert '"C:\\\\Users\\\\Max Müller\\\\CS2 Skript"' in text  # lesbar: Umlaut roh, Backslash verdoppelt
    cs2 = read_config(tmp_path / "config.yaml")["scripts"][0]
    assert cs2["cwd"] == "C:\\Users\\Max Müller\\CS2 Skript"
    assert cs2["command"][-1] == WIN_PS1
    assert cs2["hide_window"] is True


def test_manual_command_with_quotes_round_trips(tmp_path):
    cmd_file = "C:\\Users\\Max Müller\\CS2 Skript\\start.cmd"
    program = "C:\\Program Files\\Tool\\tool.exe"
    tricky = '--titel=Max "Der Boss" Müller'
    answers = [
        "n", "", "", cmd_file,
        "n",                      # Vorschlag ablehnen
        program, tricky, "--pfad=C:\\Temp\\", "'quoted arg'", "",
        "D:\\Spiele\\CS2 Ordner",  # Arbeitsordner
        "",                       # hide_window
        "", *NEW_STEPS, "",       # Port, Schritte 6–8, Speichern
    ]
    code, scripted, _ = run_wizard(tmp_path, answers, is_file=lambda p: p == cmd_file, is_dir=lambda p: True)
    assert code == 0, scripted.text
    cs2 = read_config(tmp_path / "config.yaml")["scripts"][0]
    assert cs2["command"] == [program, tricky, "--pfad=C:\\Temp\\", "quoted arg"]
    assert cs2["cwd"] == "D:\\Spiele\\CS2 Ordner"


TRICKY_STRINGS = [
    "C:\\Users\\Max Müller\\CS2 Skript\\run.ps1", '"', "'", 'a "b" \'c\'', "tab\tund\nneue Zeile",
    "ümläüte ß € 😀", "\x7f\x85\u2028\u2029\ufeff", "# kein Kommentar: wirklich", "- kein Listenpunkt",
    "null", "true", "~", " führendes Leerzeichen", "Leerzeichen am Ende ", "{a: 1} [b]", "%USERPROFILE%",
    "*stern &anker !tag @at `back`", "\\\\server\\freigabe\\x.exe", "\\", "C:\\Temp\\",
]


def test_render_round_trips_tricky_strings():
    data = example_dump()
    data["scripts"][0]["cwd"] = TRICKY_STRINGS[0]
    data["scripts"][0]["command"] = list(TRICKY_STRINGS)
    data["scripts"][0]["label"] = "Ein \"Label\" mit\\Backslash"
    data["sensors"][0]["entity_id"] = "Temperatur \"innen\" ü"
    data["llm"]["keep_alive"] = "5m # nicht abschneiden"
    text = sw.checked_render(data)
    assert yaml.safe_load(text)["scripts"][0]["command"] == TRICKY_STRINGS
    assert parse_config(yaml.safe_load(text)).model_dump() == parse_config(data).model_dump()


def test_render_handles_numbers_and_empty_lists():
    data = example_dump()
    data["llm"]["timeout"] = 12.5
    data["llm"]["temperature"] = 0.0
    data["llm"]["think"] = False
    data["sensors"] = []
    data["scripts"] = []
    text = sw.checked_render(data)
    assert "sensors: []" in text and "scripts: []" in text
    assert read_config_text(text)["llm"]["timeout"] == 12.5


def read_config_text(text: str) -> dict:
    return parse_config(yaml.safe_load(text)).model_dump()


def custom_existing() -> dict:
    data = example_config_dict()
    data["server"].update({"bind": "127.0.0.1", "port": 9000, "allowed_networks": ["127.0.0.0/8", "10.0.0.0/8"]})
    data["llm"].update({"model": "qwen3:4b", "keep_alive": "10m", "timeout": 120, "temperature": 0.5,
                        "max_tool_rounds": 6, "think": False, "base_url": "http://127.0.0.1:11434"})
    data["wled"] = {"base_url": "http://wled.test", "timeout": 5}
    data["sensors"] = [
        {"name": "Temperatur", "type": "esphome_rest", "base_url": "http://esp.test",
         "entity_id": "BME280 Temperature", "unit": "°C"},
        {"name": "Luftfeuchte", "type": "esphome_rest", "base_url": "http://esp.test",
         "entity_id": "BME280 Humidity", "unit": "%"},
        {"name": "CO2", "type": "esphome_rest", "base_url": "http://co2.test",
         "entity_id": "SCD40 CO2", "unit": "ppm"},
    ]
    data["scripts"] = [
        {"id": "obs", "label": "OBS", "cwd": "C:\\OBS", "command": ["C:\\OBS\\obs64.exe", "--minimize-to-tray"],
         "single_instance": False, "hide_window": True},
        {"id": "cs2", "label": "Mein CS2", "cwd": "C:\\CS2", "command": ["py", "-3", "C:\\CS2\\a.py"],
         "single_instance": True, "hide_window": False},
    ]
    return data


def ollama_with_current():
    return [("klein:1b", 1_000_000_000, ["tools"]), ("qwen3:4b", 2_500_000_000, ["tools"])]


def test_existing_config_values_are_preserved_and_backed_up(tmp_path):
    original = write_yaml(tmp_path / "config.yaml", custom_existing(), "# meine Notiz\n")
    before = read_config(tmp_path / "config.yaml")
    answers = ["n", "http://WLED2.test:8080/json/info", "", "", "", "", "", "", *NEW_STEPS, ""]
    code, scripted, _ = run_wizard(tmp_path, answers, FakeNet(ollama_models=ollama_with_current()))
    assert code == 0, scripted.text
    assert scripted.answers == []
    after = read_config(tmp_path / "config.yaml")
    assert after["wled"]["base_url"] == "http://wled2.test:8080"
    after["wled"]["base_url"] = before["wled"]["base_url"]
    assert after == before  # nichts sonst verändert: LLM-Werte, weitere Sensoren/Skripte, Netze, bind
    assert any("2: qwen3:4b" in p for p in scripted.prompts)  # Vorgabe = bisheriges Modell
    assert (tmp_path / "config.yaml.bak").read_bytes() == original
    assert "Sicherung" in scripted.text


def test_unchanged_run_does_not_touch_file(tmp_path):
    original = write_yaml(tmp_path / "config.yaml", custom_existing(), "# meine Notiz\n")
    answers = ["n", "", "", "", "", "", "", "", *NEW_STEPS]  # Test, WLED, ESP, 2x Entität, CS2, Modell, Port, 6–8
    code, scripted, _ = run_wizard(tmp_path, answers, FakeNet(ollama_models=ollama_with_current()))
    assert code == 0, scripted.text
    assert scripted.answers == []
    assert "Keine Änderungen" in scripted.text
    assert (tmp_path / "config.yaml").read_bytes() == original
    assert not (tmp_path / "config.yaml.bak").exists()


def test_separate_esphome_devices_are_asked_separately(tmp_path):
    data = custom_existing()
    data["sensors"][1]["base_url"] = "http://esp2.test"
    write_yaml(tmp_path / "config.yaml", data)
    answers = ["n", "", "esp3.test", "", "", "", "", "", "", *NEW_STEPS, ""]  # WLED, Gerät 1, Gerät 2, 2x Entität, ...
    code, scripted, _ = run_wizard(tmp_path, answers, FakeNet(ollama_models=ollama_with_current()))
    assert code == 0, scripted.text
    cfg = read_config(tmp_path / "config.yaml")
    assert [s["base_url"] for s in cfg["sensors"]] == ["http://esp3.test", "http://esp2.test", "http://co2.test"]


def test_dash_resets_configured_values_to_todo(tmp_path):
    write_yaml(tmp_path / "config.yaml", custom_existing())
    answers = ["n", "-", "", "-", "", "-", "-", "", *NEW_STEPS, ""]  # WLED -, ESP, Temp -, Feuchte, CS2 -, Modell -
    code, scripted, _ = run_wizard(tmp_path, answers, FakeNet(ollama_models=ollama_with_current()))
    assert code == 0, scripted.text
    cfg = read_config(tmp_path / "config.yaml")
    assert cfg["wled"]["base_url"] == sw.TODO_WLED
    assert cfg["sensors"][0]["entity_id"] == "TODO_ENTITAET_TEMPERATUR"
    assert cfg["sensors"][1]["entity_id"] == "BME280 Humidity"
    cs2 = next(s for s in cfg["scripts"] if s["id"] == "cs2")
    assert cs2["cwd"] == sw.TODO_CWD and cs2["command"] == sw.TODO_COMMAND
    assert cs2["label"] == "Mein CS2"
    assert cfg["llm"]["model"] == "qwen3:4b"
    # Eingerichtete Werte: Der Hinweis sagt ehrlich, dass '-' zurücksetzt (nicht "überspringen").
    out = scripted.text
    assert out.count("Enter = unverändert lassen, '-' = auf TODO zurücksetzen") >= 2, out
    assert "WLED ist jetzt nicht eingerichtet (TODO)." in out
    assert "'Temperatur' ist jetzt nicht eingerichtet (TODO)." in out
    assert "bleibt TODO" not in out


def test_empty_sensor_and_script_lists_stay_empty_when_skipped(tmp_path):
    data = custom_existing()
    data["sensors"], data["scripts"] = [], []
    write_yaml(tmp_path / "config.yaml", data)
    answers = ["n", "", "", "", "", "", *NEW_STEPS]  # WLED, ESP (Vorschlag, übersprungen), CS2, Modell, Port
    code, scripted, _ = run_wizard(tmp_path, answers, FakeNet(ollama_models=ollama_with_current()))
    assert code == 0, scripted.text
    assert "Keine Änderungen" in scripted.text


def test_empty_sensor_list_gets_defaults_when_configured(tmp_path):
    data = custom_existing()
    data["sensors"] = []
    write_yaml(tmp_path / "config.yaml", data)
    answers = ["n", "", "esp.test", "Temp A", "-", "", "", "", *NEW_STEPS, ""]
    code, scripted, _ = run_wizard(tmp_path, answers, FakeNet(ollama_models=ollama_with_current()))
    assert code == 0, scripted.text
    sensors = read_config(tmp_path / "config.yaml")["sensors"]
    assert [(s["name"], s["base_url"], s["entity_id"], s["unit"]) for s in sensors] == [
        ("Temperatur", "http://esp.test", "Temp A", "°C"),
        ("Luftfeuchte", "http://esp.test", "TODO_ENTITAET_LUFTFEUCHTE", "%"),
    ]


# --------------------------------------------------------------------------------------------
# Abbruch und Fehler
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("stop", [EOFError, KeyboardInterrupt])
@pytest.mark.parametrize("after", [0, 3])
def test_abort_leaves_file_untouched(tmp_path, stop, after):
    original = write_yaml(tmp_path / "config.yaml", custom_existing())
    answers = ["n", "http://neu.test", "", ""][:after] + [stop]
    code, scripted, _ = run_wizard(tmp_path, answers)
    assert code == 1
    assert "Abgebrochen" in scripted.text
    assert (tmp_path / "config.yaml").read_bytes() == original
    assert not (tmp_path / "config.yaml.bak").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]


def test_declining_save_is_abort(tmp_path):
    answers = ["n", "192.0.2.10", "", "", "", *NEW_STEPS, "n"]
    code, scripted, _ = run_wizard(tmp_path, answers)
    assert code == 1
    assert not (tmp_path / "config.yaml").exists()


def test_abort_on_fresh_install_writes_nothing(tmp_path):
    code, _, _ = run_wizard(tmp_path, ["n", KeyboardInterrupt])
    assert code == 1
    assert list(tmp_path.iterdir()) == []


def test_invalid_existing_config_interactive(tmp_path):
    broken = b"server:\n  prot: 1\n"
    (tmp_path / "config.yaml").write_bytes(broken)
    code, scripted, _ = run_wizard(tmp_path, ["n"])
    assert code == 1
    assert "prot" in scripted.text
    assert (tmp_path / "config.yaml").read_bytes() == broken

    code, scripted, _ = run_wizard(tmp_path, ["j", "n", "", "", "", "", *NEW_STEPS, ""])
    assert code == 0, scripted.text
    assert read_config(tmp_path / "config.yaml") == example_dump()
    assert (tmp_path / "config.yaml.bak").read_bytes() == broken


def test_invalid_port_is_asked_again(tmp_path):
    answers = ["n", "", "", "", "0", "70000", "acht", "8766", *NEW_STEPS, ""]
    code, scripted, _ = run_wizard(tmp_path, answers)
    assert code == 0, scripted.text
    assert scripted.text.count("1 bis 65535") == 3
    assert read_config(tmp_path / "config.yaml")["server"]["port"] == 8766


def test_invalid_url_is_asked_again(tmp_path):
    answers = ["n", "ftp://wled.test", "wled test", "wled.test:99999", "wled.test", "", "", "", *NEW_STEPS, ""]
    code, scripted, _ = run_wizard(tmp_path, answers)
    assert code == 0, scripted.text
    assert read_config(tmp_path / "config.yaml")["wled"]["base_url"] == "http://wled.test"


def test_example_missing_uses_builtin_defaults(tmp_path):
    code, scripted, _ = run_wizard(tmp_path, ["n", "", "", "", "", *NEW_STEPS, ""], example=tmp_path / "fehlt.yaml")
    assert code == 0, scripted.text
    assert read_config(tmp_path / "config.yaml") == example_dump()


def test_builtin_defaults_match_example():
    assert sw.builtin_defaults() == example_dump()


# --------------------------------------------------------------------------------------------
# Proben (nur lesend) und Hinweise
# --------------------------------------------------------------------------------------------


def test_wled_probe_failure_asks_and_can_retry(tmp_path):
    answers = ["", "192.0.2.99", "n", "192.0.2.10", "", "", "", *NEW_STEPS, ""]
    code, scripted, net = run_wizard(tmp_path, answers, device_net())
    assert code == 0, scripted.text
    assert "Nicht erreichbar" in scripted.text
    assert any("trotzdem übernehmen" in p for p in scripted.prompts)
    assert read_config(tmp_path / "config.yaml")["wled"]["base_url"] == "http://192.0.2.10"


def test_wled_probe_failure_keep_anyway(tmp_path):
    answers = ["", "192.0.2.99", "j", "", "", "", *NEW_STEPS, ""]
    code, scripted, _ = run_wizard(tmp_path, answers, device_net())
    assert code == 0, scripted.text
    assert read_config(tmp_path / "config.yaml")["wled"]["base_url"] == "http://192.0.2.99"


def test_esphome_404_shows_web_server_hint_and_retries(tmp_path):
    answers = ["", "", "esp.test", "Falscher Name", "n", "BME280 Temperature", "-", "", "", *NEW_STEPS, ""]
    code, scripted, _ = run_wizard(tmp_path, answers, device_net())
    assert code == 0, scripted.text
    assert "404" in scripted.text and "web_server:" in scripted.text and "object_id" in scripted.text
    cfg = read_config(tmp_path / "config.yaml")
    assert cfg["sensors"][0]["entity_id"] == "BME280 Temperature"
    assert cfg["sensors"][1]["entity_id"] == "TODO_ENTITAET_LUFTFEUCHTE"


def test_unreachable_esphome_kept_anyway_skips_sensor_probes(tmp_path):
    answers = ["", "", "esp-aus.test", "j", "Temp", "Feuchte", "", "", *NEW_STEPS, ""]
    code, scripted, net = run_wizard(tmp_path, answers, device_net())
    assert code == 0, scripted.text
    assert not any(r.url.path.startswith("/sensor/") for r in net.requests)
    assert [s["entity_id"] for s in read_config(tmp_path / "config.yaml")["sensors"]] == ["Temp", "Feuchte"]


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_probe_wled_variants():
    def handler(request):
        host = request.url.host
        if host == "ok.test":
            return httpx.Response(200, json={"name": "WLED", "ver": "0.14.4", "leds": {"count": 60}})
        if host == "html.test":
            return httpx.Response(200, text="<html>")
        if host == "nf.test":
            return httpx.Response(404)
        raise httpx.ConnectTimeout("zu langsam", request=request)

    with mock_client(handler) as c:
        ok = sw.probe_wled(c, "http://ok.test")
        assert ok.ok and "WLED" in ok.message and "0.14.4" in ok.message and "60 LEDs" in ok.message
        assert not sw.probe_wled(c, "http://html.test").ok
        assert "404" in sw.probe_wled(c, "http://nf.test").message
        timeout = sw.probe_wled(c, "http://slow.test")
        assert not timeout.ok and not timeout.reachable and "3 s" in timeout.message


def test_probe_esphome_variants():
    def handler(request):
        host, path = request.url.host, request.url.path
        if host == "auth.test":
            return httpx.Response(401)
        if host == "plain.test":
            return httpx.Response(404)
        if path == "/sensor/Temp":
            return httpx.Response(200, json={"value": None, "state": "NA"})
        return httpx.Response(200, text="ok")

    with mock_client(handler) as c:
        assert "Passwort" in sw.probe_esphome_device(c, "http://auth.test").message
        assert "web_server:" in sw.probe_esphome_device(c, "http://plain.test").message
        assert sw.probe_esphome_device(c, "http://esp.test").ok
        no_value = sw.probe_esphome_sensor(c, "http://esp.test", "Temp")
        assert no_value.ok and "keinen Messwert" in no_value.message
        missing = sw.probe_esphome_sensor(c, "http://plain.test", "BME280 Temperature")
        assert not missing.ok and "bme280_temperature" in missing.message


def test_sensor_name_is_url_quoted():
    seen = []

    def handler(request):
        seen.append(request.url.raw_path)
        return httpx.Response(200, json={"value": 1.0})

    with mock_client(handler) as c:
        assert sw.probe_esphome_sensor(c, "http://esp.test/", "Temp/Innen ü").ok
    assert seen == [b"/sensor/Temp%2FInnen%20%C3%BC"]


@pytest.mark.parametrize("raw,expected", [
    ("192.0.2.10", "http://192.0.2.10"),
    ("  http://192.0.2.10/  ", "http://192.0.2.10"),
    ('"192.0.2.10"', "http://192.0.2.10"),
    ("wled.local:8080", "http://wled.local:8080"),
    ("HTTP://WLED.Local/json/info?x=1#y", "http://wled.local"),
    ("https://wled.test", "https://wled.test"),
    ("[fd00::1]:80", "http://[fd00::1]:80"),
    ("esp_home-1", "http://esp_home-1"),
])
def test_normalize_base_url(raw, expected):
    assert sw.normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "  ", "ftp://x.test", "http://", "a b", "x.test:99999", "x.test:abc",
                                 "x.test:0", "user:pw@x.test", "exa$mple", "[zz::1]"])
def test_normalize_base_url_rejects(raw):
    with pytest.raises(ValueError):
        sw.normalize_base_url(raw)


@pytest.mark.parametrize("raw", ["192.168.1.300", "10.0.0.1000", "192.168.001.050", "999.1.1.1", "http://192.0.2.256:80"])
def test_normalize_base_url_rejects_invalid_ipv4(raw):
    """Tippfehler in der IP-Adresse: klare Meldung statt Absturz in httpx (InvalidURL)."""
    with pytest.raises(ValueError, match="keine gültige IP-Adresse"):
        sw.normalize_base_url(raw)


@pytest.mark.parametrize("raw,expected", [
    ("fd00::5", "http://[fd00::5]"),
    ("fe80::1", "http://[fe80::1]"),
    ("2001:db8::1", "http://[2001:db8::1]"),
])
def test_normalize_base_url_bare_ipv6(raw, expected):
    assert sw.normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw,match", [
    ("http//192.0.2.1", "http://<adresse>"),
    ("https//192.0.2.1", "http://<adresse>"),
    ("http:/192.0.2.1", "http://<adresse>"),
    ("http:\\\\192.0.2.1", "statt"),
    ("fe80::1%eth0", "Zonen-ID"),
])
def test_normalize_base_url_rejects_scheme_typos(raw, match):
    with pytest.raises(ValueError, match=match):
        sw.normalize_base_url(raw)


@pytest.mark.parametrize("url", ["http://192.168.1.300", "http://10.0.0.1000", "http://192.168.001.050"])
def test_probe_with_invalid_address_does_not_crash(url):
    """Von Hand eingetragene, ungültige Adresse (mit Enter übernommen): Ergebnis statt Ausnahme."""
    with httpx.Client() as c:  # httpx prüft die URL schon vor jeder Verbindung
        for result in (sw.probe_wled(c, url), sw.probe_esphome_device(c, url),
                       sw.probe_esphome_sensor(c, url, "BME280 Temperature")):
            assert not result.ok and not result.reachable
            assert "Ungültige Adresse" in result.message


def test_wizard_with_invalid_existing_wled_url_does_not_abort(tmp_path):
    data = custom_existing()
    data["wled"]["base_url"] = "http://192.168.1.300"
    write_yaml(tmp_path / "config.yaml", data)
    # Mit Gerätetest, überall Enter: Die WLED-Probe meldet "Ungültige Adresse" statt abzustürzen.
    answers = ["j"] + [""] * 9 + NEW_STEPS
    code, scripted, _ = run_wizard(tmp_path, answers, FakeNet(ollama_models=ollama_with_current()))
    assert code == 0, scripted.text
    assert "Ungültige Adresse: http://192.168.1.300/json/info" in scripted.text
    assert "Fehler:" not in scripted.text


def test_connect_error_message_is_plain_german():
    def handler(request):
        raise httpx.ConnectError("weg", request=request)

    with mock_client(handler) as c:
        result = sw.probe_wled(c, "http://192.0.2.99")
    assert not result.ok and not result.reachable
    assert "ConnectError" not in result.message
    assert "Gerät aus oder Adresse falsch" in result.message


# --------------------------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------------------------


def ollama_handler(models, *, show_fails: set[str] = frozenset(), tags_status: int = 200):
    def handler(request):
        if request.url.path == "/api/tags":
            if tags_status != 200:
                return httpx.Response(tags_status)
            return httpx.Response(200, json={"models": [{"name": n, "size": s} for n, s, _ in models]})
        name = json.loads(request.content)["model"]
        if name in show_fails:
            return httpx.Response(500)
        return httpx.Response(200, json={"capabilities": next(c for n, _, c in models if n == name)})

    return handler


def test_ollama_models_sorted_smallest_first():
    models = [("big", 9_000_000_000, ["completion", "tools"]), ("tiny-no-tools", 500_000_000, ["completion"]),
              ("small", 2_000_000_000, ["completion", "tools"]), ("small-b", 2_000_000_000, ["tools"]),
              ("kaputt", 100, ["tools"])]
    with mock_client(ollama_handler(models, show_fails={"kaputt"})) as c:
        found = list_models(c, "http://ollama.test/")
    assert [m.name for m in found] == ["big", "tiny-no-tools", "small", "small-b", "kaputt"]
    assert [m.name for m in tool_models(found)] == ["small", "small-b", "big"]


@pytest.mark.parametrize("handler", [
    ollama_handler([], tags_status=500),
    lambda r: httpx.Response(200, text="kein json"),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError("weg", request=r)),
])
def test_ollama_unavailable(handler):
    with mock_client(handler) as c, pytest.raises(OllamaUnavailable):
        list_models(c, "http://ollama.test")


@pytest.mark.parametrize("answer,expected", [("", "klein"), ("2", "mittel"), ("gross", "gross"), ("-", None)])
def test_wizard_model_choice(tmp_path, answer, expected):
    models = [("gross", 9_000_000_000, ["tools"]), ("klein", 1_000_000_000, ["tools"]),
              ("mittel", 4_000_000_000, ["completion", "tools"]), ("winzig", 100_000_000, ["completion"])]
    answers = ["n", "", "", "", "x", answer, "", *NEW_STEPS, ""]  # "x" ist ungültig → erneute Frage
    code, scripted, _ = run_wizard(tmp_path, answers, FakeNet(ollama_models=models))
    assert code == 0, scripted.text
    assert "Nummer aus der Liste" in scripted.text
    assert "winzig" in scripted.text  # als 'ohne Tool-Unterstützung' aufgeführt
    model = read_config(tmp_path / "config.yaml")["llm"]["model"]
    assert model == (expected or "TODO_MODELLNAME")


def test_wizard_no_tool_models_keeps_todo(tmp_path):
    models = [("nur-text", 1_000_000, ["completion"])]
    code, scripted, net = run_wizard(tmp_path, ["n", "", "", "", "", *NEW_STEPS, ""], FakeNet(ollama_models=models))
    assert code == 0, scripted.text
    assert "keins unterstützt Tools" in scripted.text and "ollama pull" in scripted.text
    assert read_config(tmp_path / "config.yaml")["llm"]["model"] == "TODO_MODELLNAME"


# --------------------------------------------------------------------------------------------
# CS2-Skript: Vorschläge, nie öffnen
# --------------------------------------------------------------------------------------------

AHK_V2 = "C:\\Program Files\\AutoHotkey\\v2\\AutoHotkey64.exe"


@pytest.mark.parametrize("path,expected", [
    ("C:\\S\\a.py", ["py", "-3", "C:\\S\\a.py"]),
    ("C:\\S\\a.PS1", ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:\\S\\a.PS1"]),
    ("C:\\S\\a.ahk", [AHK_V2, "C:\\S\\a.ahk"]),
    ("C:\\S\\a.exe", ["C:\\S\\a.exe"]),
    ("C:\\S\\a.bat", ["cmd.exe", "/d", "/c", "call", "C:\\S\\a.bat"]),
    ("C:\\S\\a.cmd", ["cmd.exe", "/d", "/c", "call", "C:\\S\\a.cmd"]),
    ("C:\\S\\a.txt", None),
])
def test_suggest_command(path, expected):
    s = sw.suggest_command(path, is_file=lambda p: p == AHK_V2, which=lambda n: None,
                           env={"ProgramFiles": "C:\\Program Files"})
    assert s.cwd == "C:\\S"
    assert s.command == expected


def test_suggest_bat_in_program_files_x86_keeps_quotes():
    """cmd /c "C:\\Program Files (x86)\\...bat" würde die Anführungszeichen entfernen ("call" davor hilft)."""
    path = "C:\\Program Files (x86)\\Steam\\steamapps\\common\\CS2 Tools\\start.bat"
    s = sw.suggest_command(path, which=lambda n: None, env={})
    assert s.command == ["cmd.exe", "/d", "/c", "call", path]
    line = subprocess.list2cmdline(s.command)  # so baut Popen unter Windows die Kommandozeile
    rest = line.split(" /c ", 1)[1]
    assert not rest.startswith('"'), line  # sonst greift die alte Anführungszeichen-Regel von cmd /c
    assert f'"{path}"' in line
    assert not s.notes, s.notes  # ( ) mit Leerzeichen im Pfad sind kein Problem mehr


@pytest.mark.parametrize("path", ["C:\\S\\100%\\a.bat", "C:\\S\\a^b.cmd", "C:\\S\\a!.bat", "C:\\S\\a&b.bat"])
def test_suggest_bat_warns_about_cmd_special_characters(path):
    s = sw.suggest_command(path, which=lambda n: None, env={})
    assert any("Sonderzeichen" in n for n in s.notes), (path, s.notes)


def test_suggest_py_mentions_alternative_python():
    s = sw.suggest_command("C:\\S\\a.py", which=lambda n: "C:\\Windows\\py.exe", env={})
    assert any("python.exe" in n for n in s.notes)
    s = sw.suggest_command("C:\\S\\a.py", which=lambda n: None, env={})
    assert any("nicht gefunden" in n for n in s.notes)


def test_autohotkey_found_per_user_and_via_path():
    per_user = "C:\\Users\\X\\AppData\\Local\\Programs\\AutoHotkey\\v2\\AutoHotkey64.exe"
    env = {"LOCALAPPDATA": "C:\\Users\\X\\AppData\\Local"}
    assert sw.find_autohotkey(is_file=lambda p: p == per_user, which=lambda n: None, env=env) == per_user
    assert sw.find_autohotkey(is_file=lambda p: False, which=lambda n: "C:\\bin\\" + n, env={}) == \
        "C:\\bin\\AutoHotkey64.exe"


def test_ahk_not_found_asks_for_exe(tmp_path):
    ahk_script = "C:\\Skripte\\cs2.ahk"
    ahk_exe = "D:\\Tools\\AutoHotkey\\AutoHotkey64.exe"
    answers = ["n", "", "", ahk_script, "D:\\gibts\\nicht.exe", ahk_exe, "", "", "", *NEW_STEPS, ""]
    code, scripted, _ = run_wizard(tmp_path, answers, is_file=lambda p: p in (ahk_script, ahk_exe))
    assert code == 0, scripted.text
    cs2 = read_config(tmp_path / "config.yaml")["scripts"][0]
    assert cs2["command"] == [ahk_exe, ahk_script]
    assert cs2["cwd"] == "C:\\Skripte"


def test_missing_script_file_is_asked_again(tmp_path):
    answers = ["n", "", "", "C:\\gibts\\nicht.ps1", "", "", *NEW_STEPS, ""]
    code, scripted, _ = run_wizard(tmp_path, answers, is_file=lambda p: False)
    assert code == 0, scripted.text
    assert "Datei nicht gefunden: C:\\gibts\\nicht.ps1" in scripted.text
    assert read_config(tmp_path / "config.yaml") == example_dump()


def test_cs2_script_is_never_opened(tmp_path, monkeypatch):
    script = tmp_path / "geheim" / "cs2.py"
    script.parent.mkdir()
    script.write_text("GEHEIMER_INHALT = 1\n")
    guarded = os.path.abspath(script)

    def is_script(file) -> bool:
        return isinstance(file, (str, bytes, os.PathLike)) and os.path.abspath(os.fsdecode(file)) == guarded

    real_open, real_os_open = builtins.open, os.open

    def guard_open(file, *args, **kwargs):
        if is_script(file):
            raise AssertionError("CS2-Skript wurde geöffnet!")
        return real_open(file, *args, **kwargs)

    def guard_os_open(path, *args, **kwargs):
        if is_script(path):
            raise AssertionError("CS2-Skript wurde geöffnet (os.open)!")
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guard_open)
    monkeypatch.setattr(io, "open", guard_open)
    monkeypatch.setattr(os, "open", guard_os_open)
    for name in ("read_text", "read_bytes", "open"):
        real = getattr(Path, name)

        def guarded_method(self, *args, _real=real, **kwargs):
            if is_script(self):
                raise AssertionError("CS2-Skript wurde über pathlib gelesen!")
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, guarded_method)

    answers = ["n", "", "", str(script), "", "", "", *NEW_STEPS, ""]
    code, scripted, _ = run_wizard(tmp_path, answers, which=lambda n: "/usr/bin/py")
    assert code == 0, scripted.text
    assert "GEHEIMER_INHALT" not in scripted.text
    cs2 = read_config(tmp_path / "config.yaml")["scripts"][0]
    assert cs2["command"] == ["py", "-3", str(script)]
    assert cs2["cwd"] == str(script.parent)


# --------------------------------------------------------------------------------------------
# CLI: non-interactive, token, info
# --------------------------------------------------------------------------------------------


def test_non_interactive_creates_config(tmp_path, capsys):
    target = tmp_path / "neu" / "config.yaml"
    assert sw.main(["configure", "--config", str(target), "--example", str(EXAMPLE), "--non-interactive"]) == 0
    assert read_config(target) == example_dump()
    assert "angelegt" in capsys.readouterr().out


def test_non_interactive_validates_existing_without_touching(tmp_path, capsys):
    target = tmp_path / "config.yaml"
    original = write_yaml(target, custom_existing(), "# Notiz\n")
    assert sw.main(["configure", "--config", str(target), "--non-interactive"]) == 0
    assert target.read_bytes() == original
    assert "gültig" in capsys.readouterr().out


@pytest.mark.parametrize("content", [b"server:\n  prot: 1\n", b"server: [kaputt\n", "port: ü".encode("latin-1")])
def test_non_interactive_invalid_config(tmp_path, capsys, content):
    target = tmp_path / "config.yaml"
    target.write_bytes(content)
    assert sw.main(["configure", "--config", str(target), "--non-interactive"]) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() and "Traceback" not in captured.err
    assert target.read_bytes() == content
    assert not (tmp_path / "config.yaml.bak").exists()


def test_token_created_once(tmp_path, capsys):
    secrets = tmp_path / "secrets.yaml"
    assert sw.main(["token", "--secrets", str(secrets)]) == 0
    first = capsys.readouterr().out
    token = yaml.safe_load(secrets.read_text())["api_token"]
    assert token in first and "Neuer API-Token" in first
    before = secrets.read_bytes()

    assert sw.main(["token", "--secrets", str(secrets)]) == 0
    second = capsys.readouterr().out
    assert second.strip() == "Token vorhanden (steht in secrets.yaml)"
    assert token not in second
    assert secrets.read_bytes() == before


@pytest.mark.parametrize("content", [b"api_token: kurz\n", b"api_token: [kaputt\n", b"- liste\n"])
def test_token_never_overwrites_unusable_secrets(tmp_path, capsys, content):
    secrets = tmp_path / "secrets.yaml"
    secrets.write_bytes(content)
    assert sw.main(["token", "--secrets", str(secrets)]) == 2
    assert secrets.read_bytes() == content
    assert "Traceback" not in capsys.readouterr().err


def test_info_prints_one_json_line(tmp_path, capsys):
    target = tmp_path / "config.yaml"
    sw.main(["configure", "--config", str(target), "--non-interactive"])
    capsys.readouterr()
    assert sw.main(["info", "--config", str(target)]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and out.isascii()
    payload = json.loads(out)
    assert set(payload) == {"bind", "port", "local_url", "warnings", "desktop_enabled", "desktop_open_url",
                            "desktop_apps", "vision_model", "tts_enabled", "speak_replies", "stt_enabled", "stt_model",
                            "stt_model_size"}
    assert payload["bind"] == "0.0.0.0" and payload["port"] == 8765
    assert payload["local_url"] == "http://127.0.0.1:8765/"
    assert any("llm.model" in w for w in payload["warnings"])


def test_info_without_warnings_and_custom_bind(tmp_path, capsys):
    data = custom_existing()
    data["server"]["bind"] = "192.0.2.5"
    target = tmp_path / "config.yaml"
    write_yaml(target, data)
    assert sw.main(["info", "--config", str(target)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"bind": "192.0.2.5", "port": 9000, "local_url": "http://192.0.2.5:9000/", "warnings": [],
                       "desktop_enabled": True, "desktop_open_url": True, "desktop_apps": ["Editor", "Rechner"],
                       "vision_model": None, "tts_enabled": True, "speak_replies": True, "stt_enabled": False,
                       "stt_model": "small", "stt_model_size": "ca. 500 MB"}


@pytest.mark.parametrize("bind,url", [("0.0.0.0", "http://127.0.0.1:1/"), ("127.0.0.1", "http://127.0.0.1:1/"),
                                      ("::", "http://[::1]:1/"), ("fd00::5", "http://[fd00::5]:1/")])
def test_local_url(bind, url):
    assert sw.local_url(bind, 1) == url


@pytest.mark.parametrize("content", [None, b"server:\n  port: 0\n"])
def test_info_invalid_or_missing(tmp_path, capsys, content):
    target = tmp_path / "config.yaml"
    if content is not None:
        target.write_bytes(content)
    assert sw.main(["info", "--config", str(target)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.strip() and "Traceback" not in captured.err


def test_module_entry_point_runs(tmp_path):
    """`python -m app.setup_wizard` wie vom Installer aufgerufen (Arbeitsordner = Installationsordner)."""
    target = tmp_path / "config.yaml"
    run = lambda *args: subprocess.run(  # noqa: E731
        [sys.executable, "-m", "app.setup_wizard", *args], cwd=PROJECT_DIR, capture_output=True, text=True,
        encoding="utf-8", timeout=60,
    )
    created = run("configure", "--config", str(target), "--non-interactive")
    assert created.returncode == 0, created.stderr
    info = run("info", "--config", str(target))
    assert info.returncode == 0 and json.loads(info.stdout)["port"] == 8765
    aborted = subprocess.run(
        [sys.executable, "-m", "app.setup_wizard", "configure", "--config", str(target)], cwd=PROJECT_DIR,
        input="", capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert aborted.returncode == 1 and "Traceback" not in aborted.stderr
    target.write_text("server: [kaputt\n", encoding="utf-8")
    broken = run("info", "--config", str(target))
    assert broken.returncode == 2 and "Traceback" not in broken.stderr


def test_config_saved_by_notepad_with_bom_and_crlf(tmp_path, capsys):
    """Windows-Editor: 'UTF-8 mit BOM' und CRLF-Zeilenenden müssen genauso funktionieren."""
    target = tmp_path / "config.yaml"
    raw = "﻿" + yaml.safe_dump(custom_existing(), allow_unicode=True, sort_keys=False).replace("\n", "\r\n")
    target.write_bytes(raw.encode("utf-8"))
    assert sw.main(["info", "--config", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["port"] == 9000
    code, scripted, _ = run_wizard(tmp_path, ["n", "", "", "", "", "", "", "", *NEW_STEPS],
                                   FakeNet(ollama_models=ollama_with_current()))
    assert code == 0 and "Keine Änderungen" in scripted.text


def test_kept_value_with_failed_probe_defaults_to_keep(tmp_path):
    """Gerät gerade aus: Enter behält die bisherige Adresse (neue Eingaben brauchen ein 'j')."""
    data = custom_existing()
    data["wled"]["base_url"] = "http://wled-aus.test"
    data["sensors"][0]["entity_id"] = data["sensors"][1]["entity_id"] = "Weg"
    write_yaml(tmp_path / "config.yaml", data)
    # Test ja | WLED Enter, Probe scheitert, Enter = behalten | ESP Enter (ok) | 2x Entität Enter, 404, Enter
    # | CS2, Modell, Port
    answers = [""] * 11 + NEW_STEPS
    code, scripted, _ = run_wizard(tmp_path, answers, FakeNet(
        {("esp.test", "/"): httpx.Response(200)}, ollama_models=ollama_with_current(),
        host_defaults={"esp.test": 404}))
    assert code == 0, scripted.text
    assert scripted.answers == []
    assert any("trotzdem übernehmen? [J/n]" in p for p in scripted.prompts)
    assert "Keine Änderungen" in scripted.text


# --------------------------------------------------------------------------------------------
# Schritte 6–8: Vision-Modell, PC-Steuerung, Stimme
# --------------------------------------------------------------------------------------------

VISION_MODELS = [
    ("gross-vl", 9_000_000_000, ["completion", "vision"]),
    ("chat-tools", 2_000_000_000, ["completion", "tools"]),
    ("klein-vl", 3_000_000_000, ["completion", "vision", "tools"]),
]


def test_ollama_vision_models_sorted_smallest_first():
    with mock_client(ollama_handler(VISION_MODELS + [("kaputt", 1, ["vision"])], show_fails={"kaputt"})) as c:
        found = list_models(c, "http://ollama.test")
    assert [m.name for m in vision_models(found)] == ["klein-vl", "gross-vl"]
    assert [m.supports_vision for m in found] == [True, False, True, False]


@pytest.mark.parametrize("answer,expected", [("", "klein-vl"), ("2", "gross-vl"), ("gross-vl", "gross-vl"),
                                             ("-", "TODO_VISIONMODELL")])
def test_vision_model_choice(tmp_path, answer, expected):
    # Schritt 4 (Modell): Enter = chat-tools; Schritt 6: "x" ungültig, dann die Antwort.
    answers = ["n", "", "", "", "", "", "x", answer, *NEW_STEPS, ""]
    code, scripted, net = run_wizard(tmp_path, answers, FakeNet(ollama_models=VISION_MODELS))
    assert code == 0, scripted.text
    assert scripted.answers == []
    cfg = read_config(tmp_path / "config.yaml")
    assert cfg["llm"]["model"] == "chat-tools"
    assert cfg["vision"]["model"] == expected
    out = scripted.text
    assert out.index("1) klein-vl") < out.index("2) gross-vl")
    assert "klein-vl  (3.0 GB) – kann auch Tools" in out
    assert "chat-tools" not in out.split("Schritt 6/8")[1].split("Schritt 7/8")[0]  # kein Vision-Modell
    assert "Nummer aus der Liste" in out
    # Ollama wird nur einmal gefragt (Liste gilt für Schritt 4 und 6).
    assert [r.url.path for r in net.requests].count("/api/tags") == 1
    if expected != "TODO_VISIONMODELL":
        assert re.search(rf"Bildschirm:\s+{expected}\n", out)


def test_vision_default_is_chat_model_when_it_can_see(tmp_path):
    """Kann llm.model auch Bilder, ist es die Vorgabe (kein zusätzlicher VRAM)."""
    data = custom_existing()
    data["llm"]["model"] = "klein-vl"
    data["vision"] = {"model": "TODO_VISIONMODELL", "max_side": 1024, "jpeg_quality": 70, "monitor": 2, "timeout": 30}
    write_yaml(tmp_path / "config.yaml", data)
    answers = ["n", "", "", "", "", "", "", "", "", *NEW_STEPS, ""]  # ... Modell Enter, Port Enter, Vision Enter
    code, scripted, _ = run_wizard(tmp_path, answers, FakeNet(ollama_models=VISION_MODELS))
    assert code == 0, scripted.text
    assert "= llm.model, kein zusätzlicher VRAM" in scripted.text
    cfg = read_config(tmp_path / "config.yaml")
    assert cfg["vision"] == {"model": "klein-vl", "max_side": 1024, "jpeg_quality": 70, "monitor": 2, "timeout": 30}


def test_vision_without_ollama_or_vision_models_asks_nothing(tmp_path):
    code, scripted, _ = run_wizard(tmp_path, ["n", "", "", "", "", *NEW_STEPS, ""])
    assert code == 0, scripted.text
    assert "Ollama ist nicht erreichbar – vision.model bleibt: nicht eingerichtet" in scripted.text
    models = [("nur-text", 1_000_000, ["completion", "tools"])]
    code, scripted, _ = run_wizard(tmp_path / "b", ["n", "", "", "", "", "", *NEW_STEPS, ""],
                                   FakeNet(ollama_models=models))
    assert code == 0, scripted.text
    assert "keins versteht Bilder" in scripted.text and "ollama pull" in scripted.text
    assert read_config(tmp_path / "b" / "config.yaml")["vision"]["model"] == "TODO_VISIONMODELL"


def test_vision_warns_when_ollama_is_not_local(tmp_path):
    data = custom_existing()
    data["llm"]["base_url"] = "http://192.0.2.7:11434"
    write_yaml(tmp_path / "config.yaml", data)
    code, scripted, _ = run_wizard(tmp_path, ["n", "", "", "", "", "", "", *NEW_STEPS])
    assert code == 0, scripted.text
    assert "zeigt nicht auf diesen PC" in scripted.text and "gesperrt" in scripted.text


def test_desktop_can_be_switched_off(tmp_path):
    answers = ["n", "", "", "", "", "n", "", "", ""]  # ..., Port, PC-Steuerung = n, Sprachausgabe, Spracheingabe, Speichern
    code, scripted, _ = run_wizard(tmp_path, answers)
    assert code == 0, scripted.text
    assert scripted.answers == []
    cfg = read_config(tmp_path / "config.yaml")
    assert cfg["desktop"]["enabled"] is False
    assert cfg["desktop"]["apps"] == example_dump()["desktop"]["apps"]  # Liste bleibt erhalten
    assert re.search(r"PC-Steuerung:\s+aus\n", scripted.text)
    assert "keine freie Maus-/Tastatursteuerung" in scripted.text.replace("\n", " ")


WIN_ENV = {
    "APPDATA": "C:\\Users\\Max\\AppData\\Roaming",
    "LOCALAPPDATA": "C:\\Users\\Max\\AppData\\Local",
    "ProgramFiles": "C:\\Program Files",
    "ProgramFiles(x86)": "C:\\Program Files (x86)",
}
SPOTIFY = "C:\\Users\\Max\\AppData\\Roaming\\Spotify\\Spotify.exe"
DISCORD = "C:\\Users\\Max\\AppData\\Local\\Discord\\Update.exe"
STEAM = "C:\\Program Files (x86)\\Steam\\steam.exe"
GAME = "D:\\Spiele\\Mein Spiel (2024)\\Spiel-Ä.exe"


def test_desktop_found_and_custom_apps(tmp_path):
    files = {SPOTIFY, DISCORD, STEAM, GAME, "C:\\Windows\\explorer.exe"}
    answers = [
        "n", "", "", "", "",          # kein Test, WLED, ESP, CS2, Port
        "",                           # PC-Steuerung an
        "", "n", "",                  # Gefunden: Spotify ja, Discord nein, Steam ja
        "C:\\Programme\\notiz.txt",   # keine .exe
        "D:\\fehlt.exe",              # gibt es nicht
        "C:\\Windows\\explorer.exe",  # geschützter Prozess
        STEAM,                        # schon in der Liste
        f'"{GAME}"',                  # eigenes Programm (mit Anführungszeichen wie "Als Pfad kopieren")
        "Mein Spiel",                 # Name in der Oberfläche
        "",                           # fertig
        "", "",                       # Sprachausgabe, Spracheingabe
        "",                           # Speichern
    ]
    code, scripted, _ = run_wizard(tmp_path, answers, is_file=lambda p: p in files, env=WIN_ENV)
    assert code == 0, scripted.text
    assert scripted.answers == []
    apps = read_config(tmp_path / "config.yaml")["desktop"]["apps"]
    assert [a["id"] for a in apps] == ["editor", "rechner", "spotify", "steam", "spiel-ae"]
    assert apps[2] == {"id": "spotify", "label": "Spotify", "command": [SPOTIFY], "process_name": "Spotify.exe",
                       "window_title": "Spotify"}
    assert apps[3]["command"] == [STEAM] and apps[3]["process_name"] == "steam.exe"
    assert apps[4] == {"id": "spiel-ae", "label": "Mein Spiel", "command": [GAME], "process_name": "Spiel-Ä.exe",
                       "window_title": "Mein Spiel"}
    out = scripted.text
    assert "Gefunden: Discord" in "\n".join(scripted.prompts)
    assert ".exe-Datei" in out and "Datei nicht gefunden: D:\\fehlt.exe" in out
    assert "System-, Shell- oder JARVIS-Prozess" in out
    assert "steam.exe ist schon in der Liste" in out
    assert re.search(r"PC-Steuerung:\s+an – Editor, Rechner, Spotify, Steam, Mein Spiel\n", out)


def test_desktop_discord_uses_its_updater(tmp_path):
    answers = ["n", "", "", "", "", "", "", "", "", "", ""]
    code, scripted, _ = run_wizard(tmp_path, answers, is_file=lambda p: p == DISCORD, env=WIN_ENV)
    assert code == 0, scripted.text
    discord = read_config(tmp_path / "config.yaml")["desktop"]["apps"][-1]
    assert discord["command"] == [DISCORD, "--processStart", "Discord.exe"]
    assert discord["process_name"] == "Discord.exe"


@pytest.mark.parametrize("apps,answer,expected", [
    ([], "", ["editor", "rechner"]),                       # leere Liste: Vorschlag ja
    ("only-editor", "", ["editor"]),                        # einzelne fehlen: wohl absichtlich, Vorschlag nein
    ("only-editor", "j", ["editor", "rechner"]),
])
def test_desktop_offers_missing_default_apps(tmp_path, apps, answer, expected):
    data = custom_existing()
    if apps == "only-editor":
        apps = [a for a in example_config_dict()["desktop"]["apps"] if a["id"] == "editor"]
    data["desktop"]["apps"] = apps
    write_yaml(tmp_path / "config.yaml", data)
    answers = ["n", "", "", "", "", "", "", "", "", answer, "", "", "", ""]
    code, scripted, _ = run_wizard(tmp_path, answers, FakeNet(ollama_models=ollama_with_current()))
    assert code == 0, scripted.text
    assert any("Standard-Programme hinzufügen" in p for p in scripted.prompts)
    assert [a["id"] for a in read_config(tmp_path / "config.yaml")["desktop"]["apps"]] == expected


def test_voice_choices(tmp_path):
    answers = ["n", "", "", "", "", "",  # ..., PC-Steuerung
               "",                       # kein weiteres Programm
               "n",                      # Sprachausgabe aus
               "j",                      # Spracheingabe an
               "riesig", "base",         # Modell: ungültig, dann base
               ""]                       # Speichern
    code, scripted, _ = run_wizard(tmp_path, answers)
    assert code == 0, scripted.text
    assert scripted.answers == []
    voice = read_config(tmp_path / "config.yaml")["voice"]
    assert voice["tts"]["enabled"] is False and voice["tts"]["speak_replies"] is True
    assert voice["stt"]["enabled"] is True and voice["stt"]["model"] == "base"
    assert voice["stt"]["device"] == "cpu" and voice["stt"]["language"] == "de"
    out = scripted.text
    assert any("Spracheingabe einschalten (Modell-Download ca. 500 MB)?" in p for p in scripted.prompts)
    assert "Bitte einen dieser Namen eingeben" in out
    assert re.search(r"Sprachausgabe:\s+aus\n", out) and re.search(r"Spracheingabe:\s+an \(Whisper-Modell base\)", out)
    assert "python.exe -m app.voice.stt --download --config config.yaml" in out  # Hinweis nach dem Speichern
    # mit Ordnerwechsel (-m app… geht nur dort) und ohne nacktes „uv sync“ (uv liegt nicht im PATH)
    assert f'cd "{(tmp_path / "config.yaml").resolve().parent}"' in out
    assert "uv sync" not in out and "-InstallVoice" in out
    assert "HTTPS" in out


def test_voice_and_other_unasked_values_are_preserved(tmp_path):
    """Werte, die der Assistent nicht abfragt (Stimme, Lautstärke, Domains, Monitor ...), bleiben unverändert."""
    data = custom_existing()
    data["desktop"]["allowed_domains"] = ["youtube.com", "wikipedia.org"]
    data["desktop"]["allow_open_url"] = False
    data["vision"].update({"model": "klein-vl", "max_side": 800, "monitor": 0})
    data["voice"]["tts"].update({"voice": "Microsoft Hedda Desktop", "rate": -3, "volume": 55, "speak_replies": False})
    data["voice"]["stt"].update({"enabled": True, "model": "C:\\Modelle\\whisper-de", "language": "",
                                 "max_seconds": 12, "download_root": "D:\\Whisper", "compute_type": "float32"})
    write_yaml(tmp_path / "config.yaml", data)
    before = read_config(tmp_path / "config.yaml")
    # WLED ändern, sonst überall Enter (Spracheingabe an -> Modellfrage, Enter behält den eigenen Ordner)
    answers = ["n", "wled3.test", "", "", "", "", "", "", "", "", "", "", "", "", ""]
    code, scripted, _ = run_wizard(tmp_path, answers, FakeNet(ollama_models=ollama_with_current() + VISION_MODELS[:1]))
    assert code == 0, scripted.text
    assert scripted.answers == []
    after = read_config(tmp_path / "config.yaml")
    assert after["wled"]["base_url"] == "http://wled3.test"
    after["wled"]["base_url"] = before["wled"]["base_url"]
    # vision.model: 'klein-vl' steht hier nicht in Ollamas Liste – Enter behält den Eintrag trotzdem
    assert after == before
    assert "Enter behält den Eintrag" in scripted.text


@pytest.mark.parametrize("step,section,listed", [
    (4, "llm", [("llava:latest", 4_000_000_000, ["completion", "vision", "tools"])]),
    (6, "vision", [("llava:latest", 4_000_000_000, ["completion", "vision"])]),
])
def test_enter_keeps_hand_written_model_name(tmp_path, step, section, listed):
    """'llava' von Hand eingetragen, Ollama listet 'llava:latest': gleiches Modell – Enter ändert nichts."""
    data = custom_existing()
    data[section]["model"] = "llava"
    write_yaml(tmp_path / "config.yaml", data)
    models = listed + [("anderes-vl", 1_000_000_000, ["completion", "vision", "tools"])]
    code, scripted, _ = run_wizard(tmp_path, ["n"] + [""] * 14, FakeNet(ollama_models=ollama_with_current() + models))
    assert code == 0, scripted.text
    assert read_config(tmp_path / "config.yaml")[section]["model"] == "llava"
    assert "ist nicht installiert" not in scripted.text and "Enter behält" not in scripted.text


def test_vision_can_be_switched_off(tmp_path):
    data = custom_existing()
    data["vision"]["model"] = "klein-vl"
    write_yaml(tmp_path / "config.yaml", data)
    answers = ["n"] + [""] * 7 + ["aus"] + [""] * 6  # Test n | WLED … Port | Vision 'aus' | Schritte 7–8, Speichern
    code, scripted, _ = run_wizard(tmp_path, answers, FakeNet(ollama_models=ollama_with_current() + VISION_MODELS))
    assert code == 0, scripted.text
    assert read_config(tmp_path / "config.yaml")["vision"]["model"] == "TODO_VISIONMODELL"
    assert "Bildschirm beschreiben aus" in scripted.text


def test_old_config_without_new_sections_loads_and_stays_untouched(tmp_path):
    """config.yaml von Version 0.2.0 (ohne desktop/vision/voice): Enter überall ändert nichts an der Datei."""
    data = custom_existing()
    for section in ("desktop", "vision", "voice"):
        data.pop(section)
    original = write_yaml(tmp_path / "config.yaml", data)
    code, scripted, _ = run_wizard(tmp_path, ["n", "", "", "", "", "", "", "", *NEW_STEPS],
                                   FakeNet(ollama_models=ollama_with_current()))
    assert code == 0, scripted.text
    assert "Keine Änderungen" in scripted.text
    assert (tmp_path / "config.yaml").read_bytes() == original
    # Mit einer Änderung kommen die neuen Abschnitte (Standardwerte) dazu.
    code, scripted, _ = run_wizard(tmp_path, ["n", "", "", "", "", "", "", "", "", "", "", "j", "", ""],
                                   FakeNet(ollama_models=ollama_with_current()))
    assert code == 0, scripted.text
    cfg = read_config(tmp_path / "config.yaml")
    assert cfg["voice"]["stt"]["enabled"] is True
    assert cfg["desktop"] == example_dump()["desktop"]
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "KEINE freie\n# Maus-/Tastatursteuerung" in text and "Bildschirmfoto geht NUR an das lokale Ollama" in text


def test_render_round_trips_desktop_apps():
    data = example_dump()
    data["desktop"]["apps"].append({"id": "x-1", "label": 'Spiel "Ä" #1', "command": [GAME, "--x=\"y\"", "-"],
                                    "process_name": "Spiel-Ä.exe", "window_title": "# kein Kommentar"})
    data["desktop"]["allowed_domains"] = ["bücher.de", "youtube.com"]
    text = sw.checked_render(data)
    assert read_config_text(text) == parse_config(data).model_dump()
    assert "allowed_domains: [\"xn--bcher-kva.de\", \"youtube.com\"]" in sw.render_config(parse_config(data).model_dump())


@pytest.mark.parametrize("label,expected", [("Spotify", "spotify"), ("Mein Spiel (2024)", "mein-spiel-2024"),
                                            ("Größe", "groesse"), ("___", "programm"), ("x" * 50, "x" * 32),
                                            ("-Start", "start")])
def test_app_id_from(label, expected):
    assert sw.app_id_from(label, []) == expected


def test_app_id_is_unique():
    apps = [{"id": "spiel", "process_name": "a.exe"}, {"id": "spiel-2", "process_name": "b.exe"}]
    assert sw.app_id_from("Spiel", apps) == "spiel-3"
    entry = sw.app_entry_for_exe("C:\\Spiele\\Spiel.exe", apps)
    assert entry["id"] == "spiel-3" and entry["process_name"] == "Spiel.exe" and entry["command"] == ["C:\\Spiele\\Spiel.exe"]


def test_known_app_candidates_use_environment_only():
    spotify = next(k for k in sw.KNOWN_APPS if k.id == "spotify")
    assert spotify.candidates({}) == []
    assert spotify.candidates(WIN_ENV) == [SPOTIFY]
    for known in sw.KNOWN_APPS:  # jeder Vorschlag ist eine gültige Konfiguration
        entry = known.entry("C:\\X\\" + known.process_name, [])
        parse_config({"desktop": {"apps": [entry]}})


def test_info_reports_voice_and_vision(tmp_path, capsys):
    data = custom_existing()
    data["vision"]["model"] = "klein-vl"
    data["voice"]["stt"].update({"enabled": True, "model": "base"})
    data["desktop"]["enabled"] = False
    target = tmp_path / "config.yaml"
    write_yaml(target, data)
    assert sw.main(["info", "--config", str(target)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["vision_model"] == "klein-vl" and payload["stt_enabled"] is True and payload["stt_model"] == "base"
    assert payload["stt_model_size"] == "ca. 145 MB" and payload["vision_model"] == "klein-vl"
    assert payload["desktop_enabled"] is False and payload["tts_enabled"] is True


def test_wizard_never_downloads_or_starts_programs(tmp_path, monkeypatch):
    """Spracheingabe an: der Assistent lädt nichts (kein Whisper, kein pip) und startet kein Programm."""
    def forbidden(*args, **kwargs):
        raise AssertionError(f"verboten: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    import app.voice.stt as stt_module
    monkeypatch.setattr(stt_module, "download", forbidden)
    answers = ["n", "", "", "", "", "", "", "", "", "j", "", ""]
    code, scripted, _ = run_wizard(tmp_path, answers, is_file=lambda p: p == SPOTIFY, env=WIN_ENV)
    assert code == 0, scripted.text
    assert read_config(tmp_path / "config.yaml")["voice"]["stt"]["enabled"] is True


def test_readme_references_in_wizard_match_headings():
    """Hinweise des Assistenten auf die README ("Abschnitt 12 '…'", "Sicherheit", "Modell auswählen") stimmen."""
    readme = (PROJECT_DIR / "README.md").read_text(encoding="utf-8")
    numbered = dict(re.findall(r"(?m)^## (\d+)\. (.+)$", readme))
    plain = set(re.findall(r"(?m)^## (?:\d+\. )?(.+)$", readme))
    source = Path(sw.__file__).read_text(encoding="utf-8")
    refs = re.findall(r"Abschnitt (\d+) '([^']+)'", source)
    assert refs, "Assistent verweist auf nummerierte README-Abschnitte"
    for number, name in refs:
        assert numbered.get(number, "").startswith(name), (number, name, numbered.get(number))
    names = re.findall(r'README \(?\\"([^"\\]+)\\"', source)
    assert set(names) >= {"Sicherheit", "Modell auswählen"}, names
    for name in names:
        assert any(h == name or h.startswith(name) for h in plain), name
