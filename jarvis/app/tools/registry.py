"""Tool-Registry: eine Quelle der Wahrheit für LLM, Regel-Parser und UI."""

from __future__ import annotations

import copy
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import anyio
import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.config import AppConfig


class ToolError(Exception):
    """Fehler, dessen Nachricht direkt dem User gezeigt werden darf."""


class ToolNotFound(ToolError):
    pass


class ToolArgumentError(ToolError):
    pass


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class ToolContext:
    config: AppConfig
    http: httpx.AsyncClient
    state_dir: Path
    extras: dict[str, Any] = field(default_factory=dict)


ToolFunc = Callable[[ToolContext, Any], Awaitable[dict] | dict]


def _clean_schema(node: Any) -> Any:
    """Pydantic-Schema für kleine LLMs vereinfachen (keine titles, kein anyOf mit null)."""
    if isinstance(node, dict):
        node = {k: _clean_schema(v) for k, v in node.items() if k != "title"}
        any_of = node.get("anyOf")
        if isinstance(any_of, list):
            non_null = [s for s in any_of if s != {"type": "null"}]
            if len(non_null) == 1 and len(non_null) != len(any_of):
                node.pop("anyOf")
                node = {**non_null[0], **node}
        if node.get("default", ...) is None:
            node.pop("default")
        return node
    if isinstance(node, list):
        return [_clean_schema(v) for v in node]
    return node


@dataclass
class Tool:
    name: str
    description: str
    params: type[BaseModel]
    func: ToolFunc
    llm_allowed: bool = True

    def json_schema(self) -> dict:
        schema = _clean_schema(self.params.model_json_schema())
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema.pop("additionalProperties", None)
        return schema

    def describe(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.json_schema(),
            "llm_allowed": self.llm_allowed,
        }

    def ollama_definition(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.json_schema(),
            },
        }


class Registry:
    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name} doppelt registriert")
        self._tools[tool.name] = tool
        return tool

    def tool(self, name: str, description: str, params: type[BaseModel] = NoParams, llm_allowed: bool = True):
        """Decorator zum Registrieren einer Funktion als Tool."""

        def deco(func: ToolFunc) -> ToolFunc:
            self.register(Tool(name, description, params, func, llm_allowed))
            return func

        return deco

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def describe(self) -> list[dict]:
        return [t.describe() for t in self._tools.values()]

    def ollama_tools(self) -> list[dict]:
        return [t.ollama_definition() for t in self._tools.values() if t.llm_allowed]

    async def execute(self, name: str, args: dict | None = None, *, source: str = "ui") -> dict:
        tool = self._tools.get(name)
        if tool is None or (source == "llm" and not tool.llm_allowed):
            raise ToolNotFound(f"Unbekanntes Tool: {name!r}")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ToolArgumentError("Parameter müssen ein JSON-Objekt sein.")
        try:
            params = tool.params.model_validate(copy.deepcopy(args))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'Parameter'}: {e['msg']}" for e in exc.errors()
            )
            raise ToolArgumentError(f"Ungültige Parameter für {name}: {problems}") from None
        if inspect.iscoroutinefunction(tool.func):
            result = await tool.func(self.ctx, params)
        else:
            result = await anyio.to_thread.run_sync(tool.func, self.ctx, params)
        return result if isinstance(result, dict) else {"result": result}
