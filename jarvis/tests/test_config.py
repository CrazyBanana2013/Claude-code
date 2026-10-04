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


# --- desktop / vision / voice ------------------------------------------------------------------


def test_old_config_without_new_sections_still_loads():
    data = example_config_dict()
    for key in ("desktop", "vision", "voice"):
        data.pop(key)
    cfg = parse_config(data)
    assert [a.id for a in cfg.desktop.apps] == ["editor", "rechner"]
    assert cfg.desktop.enabled and cfg.desktop.allow_open_url and cfg.desktop.allowed_domains == []
    assert cfg.vision.model == "TODO_VISIONMODELL" and not cfg.vision.configured
    assert cfg.vision.max_side == 1568 and cfg.vision.jpeg_quality == 80 and cfg.vision.monitor == 1
    assert cfg.voice.tts.enabled and cfg.voice.tts.speak_replies and cfg.voice.tts.voice == ""
    assert cfg.voice.stt.enabled is False and cfg.voice.stt.model == "small"
    assert cfg.voice.stt.compute_type == "int8" and cfg.voice.stt.language == "de"


def test_example_matches_defaults_for_new_sections():
    from app.config import AppConfig

    example = parse_config(example_config_dict()).model_dump()
    defaults = AppConfig().model_dump()
    for key in ("desktop", "vision", "voice"):
        assert example[key] == defaults[key], key


def _with_app(**app):
    data = example_config_dict()
    base = {"id": "spotify", "label": "Spotify", "command": ["spotify.exe"], "process_name": "Spotify.exe"}
    data["desktop"]["apps"].append({**base, **app})
    return data


@pytest.mark.parametrize("app,where", [
    ({"id": "Spotify!"}, "desktop.apps[2].id"),
    ({"command": "spotify.exe --minimized"}, "desktop.apps[2].command"),
    ({"command": []}, "desktop.apps[2].command"),
    ({"process_name": "C:\\Programme\\Spotify.exe"}, "desktop.apps[2].process_name"),
    ({"process_name": "explorer.exe"}, "desktop.apps[2].process_name"),
    ({"process_name": "PYTHONW.EXE"}, "desktop.apps[2].process_name"),
    ({"label": ""}, "desktop.apps[2].label"),
    ({"shell": True}, "desktop.apps[2].shell"),
    ({"id": "editor"}, "desktop.apps"),
])
def test_invalid_desktop_apps_rejected(app, where):
    with pytest.raises(ConfigError) as exc:
        parse_config(_with_app(**app))
    assert where in str(exc.value)


def test_allowed_domains_are_normalized():
    data = example_config_dict()
    data["desktop"]["allowed_domains"] = ["YouTube.com", "*.wikipedia.org", "bücher.de.", "youtube.com"]
    assert parse_config(data).desktop.allowed_domains == ["youtube.com", "wikipedia.org", "xn--bcher-kva.de"]


@pytest.mark.parametrize("domain", ["https://youtube.com", "youtube.com/watch", "a b.de", "", "user@x.de", "-x.de"])
def test_bad_allowed_domains_rejected(domain):
    data = example_config_dict()
    data["desktop"]["allowed_domains"] = [domain]
    with pytest.raises(ConfigError) as exc:
        parse_config(data)
    assert "desktop.allowed_domains" in str(exc.value)


@pytest.mark.parametrize("section,key,value", [
    ("vision", "max_side", 100), ("vision", "jpeg_quality", 100), ("vision", "monitor", -1),
    ("vision", "timeout", 0), ("vision", "modell", "x"),
])
def test_vision_limits(section, key, value):
    data = example_config_dict()
    data[section][key] = value
    with pytest.raises(ConfigError) as exc:
        parse_config(data)
    assert f"{section}.{key}" in str(exc.value)


@pytest.mark.parametrize("sub,key,value", [
    ("tts", "rate", 11), ("tts", "rate", -11), ("tts", "volume", 101), ("stt", "device", "rocm"),
    ("stt", "compute_type", "int4"), ("stt", "language", "Deutsch"), ("stt", "max_seconds", 0),
    ("stt", "max_seconds", 600), ("stt", "model", ""), ("tts", "stimme", "x"),
])
def test_voice_limits(sub, key, value):
    data = example_config_dict()
    data["voice"][sub][key] = value
    with pytest.raises(ConfigError) as exc:
        parse_config(data)
    assert f"voice.{sub}.{key}" in str(exc.value)


def test_todo_app_produces_warning():
    cfg = parse_config(_with_app(command=["TODO_PFAD\\Spotify.exe"]))
    assert any("spotify" in w for w in config_warnings(cfg))
    assert not any("editor" in w for w in config_warnings(cfg))
