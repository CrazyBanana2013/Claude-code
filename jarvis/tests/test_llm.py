import asyncio
import json
import subprocess

import httpx
import pytest

from app.tools import pc
from tests.conftest import client_for
from tests.test_led import wled_mock

pytestmark = pytest.mark.anyio

LLM = {"llm": {"model": "test-modell:1b"}, "wled": {"base_url": "http://wled.test"}}


def tool_call(name, arguments):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": name, "arguments": arguments}}]}, "done": True}


def text(content):
    return {"message": {"role": "assistant", "content": content}, "done": True}


def scripted(*responses):
    """Ollama-Mock, der der Reihe nach die gegebenen Antworten liefert (letzte wiederholt)."""
    queue = list(responses)

    def handler(request: httpx.Request):
        assert request.url.path == "/api/chat"
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)

    return handler


async def chat(factory, message, overrides=LLM):
    handler, posted = wled_mock()
    factory.device_handler = handler
    app, token = factory(overrides)
    async with client_for(app, token=token) as c:
        r = await c.post("/api/chat", json={"message": message})
    assert r.status_code == 200, r.text
    return r.json(), posted


async def test_normal_tool_call(factory):
    factory.llm_handler = scripted(tool_call("led_power", {"state": "on"}), text("Licht ist an."))
    body, posted = await chat(factory, "Mach bitte das Licht an")
    assert body["source"] == "llm"
    assert body["reply"] == "Licht ist an."
    assert body["tool_calls"][0]["tool"] == "led_power" and body["tool_calls"][0]["ok"]
    assert posted == [{"on": True, "v": True}]

    first = json.loads(factory.llm_requests[0].content)
    assert first["model"] == "test-modell:1b"
    assert first["keep_alive"] == "2m"
    assert first["stream"] is False
    assert first["options"] == {"temperature": 0.2}
    assert first["messages"][0]["role"] == "system"
    tool_names = {t["function"]["name"] for t in first["tools"]}
    assert {"led_power", "pc_shutdown", "sensors_read", "scripts_start"} <= tool_names
    second = json.loads(factory.llm_requests[1].content)
    assert second["messages"][-1]["role"] == "tool"
    assert second["messages"][-1]["tool_name"] == "led_power"


async def test_string_arguments_are_parsed(factory):
    factory.llm_handler = scripted(tool_call("led_brightness", '{"percent": 30}'), text("ok"))
    body, posted = await chat(factory, "dimm auf 30")
    assert posted == [{"on": True, "bri": 76, "v": True}]


async def test_unknown_tool_is_rejected(factory):
    factory.llm_handler = scripted(tool_call("run_shell", {"cmd": "format c:"}), text("Das kann ich nicht."))
    body, posted = await chat(factory, "lösche alles")
    rec = body["tool_calls"][0]
    assert rec["tool"] == "run_shell" and rec["ok"] is False and rec["rejected"] is True
    assert posted == [] and factory.device_requests == []
    tool_msg = json.loads(factory.llm_requests[1].content)["messages"][-1]
    assert "Unbekanntes Tool" in tool_msg["content"]


async def test_invalid_arguments_reported_to_model(factory):
    factory.llm_handler = scripted(tool_call("led_brightness", {"percent": "viel"}), text("Fehler."))
    body, posted = await chat(factory, "hell")
    assert body["tool_calls"][0]["ok"] is False and posted == []


async def test_loop_limit(factory):
    factory.llm_handler = scripted(tool_call("led_status", {}))
    body, _ = await chat(factory, "status?")
    assert body["limit_reached"] is True
    assert len(factory.llm_requests) == 4
    assert len(body["tool_calls"]) == 4


async def test_llm_cannot_confirm_shutdown(factory, monkeypatch):
    started = []
    monkeypatch.setattr(pc.subprocess, "run", lambda cmd, **k: started.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(pc, "is_windows", lambda: True)
    factory.llm_handler = scripted(
        tool_call("pc_shutdown", {}),
        {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "confirm", "arguments": {"confirm_id": "x"}}},
            {"function": {"name": "pc_confirm", "arguments": {}}}]}},
        text("Bitte auf Bestätigen tippen."),
    )
    body, _ = await chat(factory, "PC aus")
    assert body["tool_calls"][0]["result"]["status"] == "confirm_required"
    assert all(r.get("rejected") for r in body["tool_calls"][1:])
    assert started == []


@pytest.mark.parametrize("make_response,expected", [
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), "nicht erreichbar"),
    (lambda r: httpx.Response(404, json={"error": "model 'test-modell:1b' not found"}), "nicht installiert"),
    (lambda r: httpx.Response(400, json={"error": "registry.ollama.ai/library/x does not support tools"}),
     "unterstützt keine Tools"),
    (lambda r: httpx.Response(500, json={"error": "boom"}), "Ollama-Fehler"),
])
async def test_fallback_on_ollama_errors(factory, make_response, expected):
    factory.llm_handler = make_response
    body, posted = await chat(factory, "Licht an")
    assert body["source"] == "fallback"
    assert expected in body["llm_error"]
    assert posted == [{"on": True, "v": True}]
    assert body["reply"] == "Licht an."


async def test_timeout_falls_back(factory):
    async def slow(request):
        await asyncio.sleep(2)
        return httpx.Response(200, json=text("zu spät"))

    factory.llm_handler = slow
    body, posted = await chat(factory, "Licht aus", {**LLM, "llm": {"model": "m", "timeout": 0.2}})
    assert body["source"] == "fallback" and "Zeitüberschreitung" in body["llm_error"]
    assert posted == [{"on": False, "v": True}]


async def test_forced_fallback_never_calls_ollama(factory):
    body, posted = await chat(factory, "licht an", {**LLM, "llm": {"model": "m", "force_fallback": True}})
    assert body["source"] == "fallback"
    assert factory.llm_requests == []


async def test_todo_model_uses_fallback(factory):
    body, _ = await chat(factory, "licht an", {"wled": {"base_url": "http://wled.test"}})
    assert body["source"] == "fallback" and "TODO" in body["llm_error"]
    assert factory.llm_requests == []


async def test_status_reports_model_and_tools(factory):
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "test-modell:1b", "size": 1}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "tools"]})
        return httpx.Response(404)

    factory.llm_handler = handler
    app, token = factory(LLM)
    async with client_for(app, token=token) as c:
        llm = (await c.get("/api/status")).json()["llm"]
    assert llm["reachable"] and llm["model_installed"] and llm["tools_supported"]


async def test_status_ollama_down(factory):
    factory.llm_handler = lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))
    app, token = factory(LLM)
    async with client_for(app, token=token) as c:
        llm = (await c.get("/api/status")).json()["llm"]
    assert llm["reachable"] is False


async def test_invalid_ollama_address_falls_back(factory):
    """Von Hand eingetragene, ungültige llm.base_url: Regel-Parser statt Absturz."""
    overrides = {**LLM, "llm": {"model": "m", "base_url": "http://127.0.0.256:11434"}}
    body, posted = await chat(factory, "Licht an", overrides)
    assert body["source"] == "fallback"
    assert "Ungültige Ollama-Adresse" in body["llm_error"]
    assert posted == [{"on": True, "v": True}]
    app, token = factory(overrides)
    async with client_for(app, token=token) as c:
        r = await c.get("/api/status")
    assert r.status_code == 200 and r.json()["llm"]["reachable"] is False
