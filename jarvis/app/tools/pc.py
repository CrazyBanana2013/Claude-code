"""PC herunterfahren – nur mit Bestätigung über /api/confirm/{id}."""

from __future__ import annotations

import os
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

import anyio

from app.tools.registry import Registry, ToolContext, ToolError

CONFIRM_TTL = 30
SHUTDOWN_CMD = ["shutdown", "/s", "/t", "15", "/c", "JARVIS"]
CANCEL_CMD = ["shutdown", "/a"]


def is_windows() -> bool:
    return os.name == "nt"


def run_command(cmd: list[str]) -> subprocess.CompletedProcess:
    if not is_windows():
        raise ToolError("Herunterfahren ist nur auf dem Windows-PC verfügbar.")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10, creationflags=flags)


@dataclass
class Pending:
    action: str
    expires_at: float


class ConfirmStore:
    """Einmal verwendbare, kurzlebige Bestätigungs-IDs."""

    def __init__(self, ttl: float = CONFIRM_TTL, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self.clock = clock
        self._pending: dict[str, Pending] = {}
        self._lock = threading.Lock()

    def create(self, action: str) -> str:
        with self._lock:
            now = self.clock()
            self._pending = {k: v for k, v in self._pending.items() if v.expires_at > now}
            confirm_id = secrets.token_urlsafe(16)
            self._pending[confirm_id] = Pending(action, now + self.ttl)
            return confirm_id

    def consume(self, confirm_id: str) -> str | None:
        with self._lock:
            pending = self._pending.pop(confirm_id, None)
            if pending is None or pending.expires_at <= self.clock():
                return None
            return pending.action


ACTIONS: dict[str, list[str]] = {"pc_shutdown": SHUTDOWN_CMD}


def _store(ctx: ToolContext) -> ConfirmStore:
    return ctx.extras.setdefault("confirm", ConfirmStore())


def pc_shutdown(ctx: ToolContext, _params) -> dict:
    confirm_id = _store(ctx).create("pc_shutdown")
    return {
        "status": "confirm_required",
        "confirm_id": confirm_id,
        "expires_in": int(_store(ctx).ttl),
        "message": "PC herunterfahren? Bitte in der Oberfläche bestätigen.",
    }


def pc_shutdown_cancel(_ctx: ToolContext, _params) -> dict:
    proc = run_command(CANCEL_CMD)
    if proc.returncode != 0:
        # shutdown /a meldet einen Fehler, wenn gar kein Herunterfahren geplant ist.
        return {"status": "nothing_to_cancel", "message": "Es war kein Herunterfahren geplant."}
    return {"status": "cancelled", "message": "Herunterfahren abgebrochen."}


async def confirm(ctx: ToolContext, confirm_id: str) -> dict:
    """Wird ausschließlich vom HTTP-Endpunkt /api/confirm/{id} aufgerufen, nie vom LLM."""
    action = _store(ctx).consume(confirm_id)
    if action is None:
        raise ToolError("Unbekannte oder abgelaufene Bestätigung.")
    proc = await anyio.to_thread.run_sync(run_command, ACTIONS[action])
    if proc.returncode != 0:
        raise ToolError(f"Befehl fehlgeschlagen: {(proc.stderr or proc.stdout).strip()}")
    return {"status": "shutdown_scheduled", "message": "PC fährt in 15 Sekunden herunter."}


def register(registry: Registry) -> None:
    registry.tool(
        "pc_shutdown",
        "Fährt den PC herunter. Fordert nur eine Bestätigung an; der User muss in der "
        "Oberfläche auf 'Bestätigen' tippen.",
    )(pc_shutdown)
    registry.tool("pc_shutdown_cancel", "Bricht ein geplantes Herunterfahren ab.")(pc_shutdown_cancel)
