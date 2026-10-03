import importlib.util
import json
from pathlib import Path

import httpx

spec = importlib.util.spec_from_file_location("pick_model", Path(__file__).parent.parent / "scripts" / "pick_model.py")
pick_model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pick_model)


def test_smallest_tool_model_first():
    caps = {"big": ["completion", "tools"], "tiny-no-tools": ["completion"], "small": ["completion", "tools"]}

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": "big", "size": 9_000_000_000},
                {"name": "tiny-no-tools", "size": 500_000_000},
                {"name": "small", "size": 2_000_000_000}]})
        name = json.loads(request.content)["model"]
        return httpx.Response(200, json={"capabilities": caps[name]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        found = pick_model.find_tool_models(client, "http://ollama.test")
    assert [n for _, n in found] == ["small", "big"]


def test_invalid_url_gives_message_not_traceback(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["pick_model.py", "--url", "http://127.0.0.256:11434"])
    assert pick_model.main() == 1
    out = capsys.readouterr().out
    assert "Ungültige Ollama-Adresse" in out


def test_connection_refused_message_without_class_name():
    def handler(request):
        raise httpx.ConnectError("weg", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        try:
            pick_model.find_tool_models(client, "http://ollama.test")
        except pick_model.OllamaUnavailable as exc:
            message = str(exc)
        else:
            raise AssertionError("OllamaUnavailable erwartet")
    assert "ConnectError" not in message and "Verbindung abgelehnt" in message
