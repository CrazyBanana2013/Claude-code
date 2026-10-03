import pytest

from app.config import PROJECT_DIR, ConfigError, config_warnings, is_todo, load_config, parse_config
from tests.conftest import example_config_dict


def test_example_config_is_valid():
    cfg = parse_config(example_config_dict())
    assert cfg.server.port == 8765
    assert cfg.llm.keep_alive == "2m"
    assert cfg.script("cs2") is not None
    assert not cfg.script("cs2").is_configured
    assert not cfg.llm.usable  # Modell ist TODO


def test_example_config_produces_warnings():
    warnings = config_warnings(parse_config(example_config_dict()))
    assert any("llm.model" in w for w in warnings)
    assert any("cs2" in w for w in warnings)


def test_missing_file_has_clear_message(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "config.yaml")
    assert "config.example.yaml" in str(exc.value)


def test_invalid_yaml(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("server: [unclosed\n")
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert "kein gültiges YAML" in str(exc.value)


def test_typo_and_wrong_types_are_reported():
    data = example_config_dict()
    data["server"]["prot"] = 1
    data["server"]["port"] = "abc"
    del data["scripts"][0]["command"]
    with pytest.raises(ConfigError) as exc:
        parse_config(data, "config.yaml")
    msg = str(exc.value)
    assert "server.prot: unbekannter Schlüssel" in msg
    assert "server.port" in msg
    assert "scripts[0].command: Pflichtfeld fehlt" in msg


def test_shell_string_command_rejected():
    data = example_config_dict()
    data["scripts"][0]["command"] = "python skript.py"
    with pytest.raises(ConfigError) as exc:
        parse_config(data)
    assert "scripts[0].command" in str(exc.value)


def test_duplicate_script_ids_rejected():
    data = example_config_dict()
    data["scripts"].append(dict(data["scripts"][0]))
    with pytest.raises(ConfigError) as exc:
        parse_config(data)
    assert "doppelt" in str(exc.value)


def test_bad_network_rejected():
    data = example_config_dict()
    data["server"]["allowed_networks"] = ["192.168.0.0/33"]
    with pytest.raises(ConfigError):
        parse_config(data)


def test_unknown_sensor_type_rejected():
    data = example_config_dict()
    data["sensors"][0]["type"] = "mqtt"
    with pytest.raises(ConfigError) as exc:
        parse_config(data)
    assert "sensors[0].type" in str(exc.value)


@pytest.mark.parametrize("value,expected", [("TODO", True), ("TODO_X", True), ("", True), (None, True),
                                            (["a", "TODO"], True), ("C:\\scripts", False), (["py", "a.py"], False)])
def test_is_todo(value, expected):
    assert is_todo(value) is expected


def test_ansi_encoded_config_has_clear_message(tmp_path):
    """Im Editor als 'ANSI' (cp1252) gespeichert: Klartext statt UnicodeDecodeError."""
    p = tmp_path / "config.yaml"
    text = (PROJECT_DIR / "config.example.yaml").read_text(encoding="utf-8")
    p.write_bytes(text.encode("cp1252", errors="replace"))
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert "nicht UTF-8-kodiert" in str(exc.value)


def test_config_path_is_a_directory(tmp_path):
    (tmp_path / "config.yaml").mkdir()
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "config.yaml")
    assert "Ordner" in str(exc.value) or "nicht gelesen" in str(exc.value)
