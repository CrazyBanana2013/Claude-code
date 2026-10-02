"""Platzhalter – wird in Meilenstein M3 implementiert."""

from dataclasses import dataclass, field


@dataclass
class ChatOutcome:
    ok: bool
    reply: str = ""
    tool_calls: list = field(default_factory=list)
    error: str | None = None

    def as_response(self) -> dict:
        return {"reply": self.reply, "tool_calls": self.tool_calls, "source": "llm"}


class OllamaAgent:
    def __init__(self, cfg, registry, http) -> None:
        self.cfg = cfg

    async def status(self) -> dict:
        return {"reachable": False, "model": self.cfg.model}

    async def chat(self, message: str) -> ChatOutcome:
        return ChatOutcome(ok=False, error="noch nicht implementiert")
