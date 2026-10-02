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
