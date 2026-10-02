from __future__ import annotations

from pathlib import Path
from typing import Callable

import httpx
import pytest
import yaml

from app.config import PROJECT_DIR, parse_config
from app.main import create_app

LAN_IP = "192.168.1.50"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def example_config_dict() -> dict:
    return yaml.safe_load((PROJECT_DIR / "config.example.yaml").read_text(encoding="utf-8"))


class AppFactory:
    """Baut eine App mit gemockten HTTP-Clients für Geräte und Ollama."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.device_handler: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(
            599, json={"error": "kein Mock"}
        )
        self.llm_handler: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(
            599, json={"error": "kein Mock"}
        )
        self.device_requests: list[httpx.Request] = []
        self.llm_requests: list[httpx.Request] = []

    def _device(self, request: httpx.Request) -> httpx.Response:
        self.device_requests.append(request)
        return self.device_handler(request)

    def _llm(self, request: httpx.Request) -> httpx.Response:
        self.llm_requests.append(request)
        return self.llm_handler(request)

    def __call__(self, overrides: dict | None = None):
        data = example_config_dict()
        for section, values in (overrides or {}).items():
            if isinstance(values, dict) and isinstance(data.get(section), dict):
                data[section].update(values)
            else:
                data[section] = values
        config = parse_config(data)
        self.config = config
        app = create_app(
            config,
            secrets_path=self.tmp_path / "secrets.yaml",
            state_dir=self.tmp_path / "state",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._device)),
            llm_client=httpx.AsyncClient(transport=httpx.MockTransport(self._llm)),
            announce=lambda msg: None,
        )
        token = yaml.safe_load((self.tmp_path / "secrets.yaml").read_text())["api_token"]
        return app, token


@pytest.fixture
def factory(tmp_path) -> AppFactory:
    return AppFactory(tmp_path)


def client_for(app, ip: str = LAN_IP, token: str | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 51234)),
        base_url="http://jarvis.test",
        headers=headers,
    )
