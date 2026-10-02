"""FastAPI-App: JARVIS-Oberfläche, Tool-Endpunkte, Chat."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import APIRouter, Body, Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app import fallback
from app.auth import TokenAuth, load_or_create_token
from app.config import PROJECT_DIR, AppConfig, ConfigError, config_warnings, load_config
from app.llm import OllamaAgent
from app.netguard import NetGuardMiddleware
from app.tools import register_all
from app.tools.registry import Registry, ToolArgumentError, ToolContext, ToolError, ToolNotFound

log = logging.getLogger("jarvis")

WEB_INDEX = PROJECT_DIR / "web" / "index.html"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)


def create_app(
    config: AppConfig,
    *,
    secrets_path: Path,
    state_dir: Path,
    http_client: httpx.AsyncClient | None = None,
    llm_client: httpx.AsyncClient | None = None,
    announce: Callable[[str], None] = print,
) -> FastAPI:
    token = load_or_create_token(secrets_path, announce)
    auth = TokenAuth(token)
    state_dir.mkdir(parents=True, exist_ok=True)

    # trust_env=False: Geräte im LAN nie über einen System-Proxy ansprechen.
    http = http_client or httpx.AsyncClient(timeout=5, trust_env=False)
    llm_http = llm_client or httpx.AsyncClient(timeout=config.llm.timeout, trust_env=False)
    ctx = ToolContext(config=config, http=http, state_dir=state_dir)
    registry = Registry(ctx)
    register_all(registry)
    agent = OllamaAgent(config.llm, registry, llm_http)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await http.aclose()
        if llm_http is not http:
            await llm_http.aclose()

    app = FastAPI(title="JARVIS", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(NetGuardMiddleware, networks=config.server.allowed_networks)
    app.state.registry = registry
    app.state.agent = agent
    app.state.auth = auth

    @app.exception_handler(ToolNotFound)
    async def _not_found(_req: Request, exc: ToolNotFound):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @app.exception_handler(ToolArgumentError)
    async def _bad_args(_req: Request, exc: ToolArgumentError):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    @app.exception_handler(ToolError)
    async def _tool_error(_req: Request, exc: ToolError):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB_INDEX, media_type="text/html; charset=utf-8")

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    api = APIRouter(prefix="/api", dependencies=[Depends(auth)])

    @api.get("/status")
    async def status():
        return {"server": "ok", "llm": await agent.status()}

    @api.get("/tools")
    async def list_tools():
        return {"tools": registry.describe()}

    @api.post("/tools/{name}")
    async def run_tool(name: str, args: dict[str, Any] | None = Body(default=None)):
        result = await registry.execute(name, args or {}, source="ui")
        return {"ok": True, "tool": name, "result": result}

    @api.post("/confirm/{confirm_id}")
    async def confirm(confirm_id: str):
        from app.tools import pc

        result = await pc.confirm(ctx, confirm_id)
        return {"ok": True, "result": result}

    @api.post("/chat")
    async def chat(req: ChatRequest):
        return await answer_chat(req.message, agent, registry, config)

    app.include_router(api)
    return app


async def answer_chat(message: str, agent: OllamaAgent, registry: Registry, config: AppConfig) -> dict:
    """LLM fragen; wenn das nicht geht (und noch nichts ausgeführt wurde), Regel-Parser nutzen."""
    llm_error = None
    if config.llm.usable:
        outcome = await agent.chat(message)
        if outcome.ok or outcome.tool_calls:
            return outcome.as_response()
        llm_error = outcome.error
    elif config.llm.force_fallback or not config.llm.enabled:
        llm_error = "LLM deaktiviert (Konfiguration)."
    else:
        llm_error = "Kein Modell konfiguriert (llm.model ist TODO)."
    response = await fallback.handle(message, registry)
    response["llm_error"] = llm_error
    return response


def _default_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"\nJARVIS kann nicht starten:\n{exc}\n", file=sys.stderr)
        raise SystemExit(2) from None
    for warning in config_warnings(config):
        log.warning(warning)
    return create_app(
        config,
        secrets_path=PROJECT_DIR / "secrets.yaml",
        state_dir=PROJECT_DIR / "state",
    )


_app: FastAPI | None = None


def __getattr__(name: str):
    # `uvicorn app.main:app` lädt die App erst beim Zugriff – Tests können create_app()
    # importieren, ohne dass eine config.yaml gelesen oder ein Token erzeugt wird.
    global _app
    if name == "app":
        if _app is None:
            _app = _default_app()
        return _app
    raise AttributeError(name)
