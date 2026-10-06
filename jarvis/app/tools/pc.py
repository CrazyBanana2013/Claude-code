"""PC herunterfahren und allgemeine Bestätigungen – ausgeführt nur über /api/confirm/{id}.

Gefährliche Aktionen (Herunterfahren, Programm schließen) erzeugen nur eine einmalige,
kurzlebige Bestätigungs-ID. Ausgeführt wird ausschließlich in ``confirm()``, das nur die Route
``POST /api/confirm/{id}`` aufruft (also der Button in der Oberfläche) – es gibt kein Tool, über das
das LLM bestätigen könnte. Eine ausstehende Aktion trägt den Namen einer hier registrierten
Aktion plus ihre (schon geprüften) Parameter; beliebiger Code kann nicht hinterlegt werden.
"""

from __future__ import annotations

import copy
import inspect
import os
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import anyio

from app.tools.registry import Registry, ToolContext, ToolError

CONFIRM_TTL = 30
MAX_PENDING = 50
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
    params: dict[str, Any] = field(default_factory=dict)


class ConfirmStore:
    """Einmal verwendbare, kurzlebige Bestätigungs-IDs (mit Aktion und Parametern)."""

    def __init__(self, ttl: float = CONFIRM_TTL, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self.clock = clock
        self._pending: dict[str, Pending] = {}
        self._lock = threading.Lock()

    def create(self, action: str, params: dict[str, Any] | None = None) -> str:
        with self._lock:
            now = self.clock()
            self._pending = {k: v for k, v in self._pending.items() if v.expires_at > now}
            while len(self._pending) >= MAX_PENDING:  # älteste zuerst verwerfen
                self._pending.pop(next(iter(self._pending)))
            confirm_id = secrets.token_urlsafe(16)
            self._pending[confirm_id] = Pending(action, now + self.ttl, copy.deepcopy(params or {}))
            return confirm_id

    def consume(self, confirm_id: str) -> Pending | None:
        """Gibt die ausstehende Aktion genau einmal zurück (None, wenn unbekannt oder abgelaufen)."""
        with self._lock:
            pending = self._pending.pop(confirm_id, None)
            if pending is None or pending.expires_at <= self.clock():
                return None
            return pending


ConfirmAction = Callable[[ToolContext, dict[str, Any]], "Awaitable[dict] | dict"]
# Aktionsname -> Ausführung. Nur Module registrieren hier (beim Import), nie das LLM oder die UI.
CONFIRM_ACTIONS: dict[str, ConfirmAction] = {}


def _qualname(func: Any) -> tuple[str, str]:
    return getattr(func, "__module__", ""), getattr(func, "__qualname__", "")


def register_confirm_action(name: str, func: ConfirmAction) -> None:
    existing = CONFIRM_ACTIONS.get(name)
    # Erneutes Registrieren derselben Funktion (z. B. nach importlib.reload) ist erlaubt.
    if existing is not None and _qualname(existing) != _qualname(func):
        raise ValueError(f"Bestätigungs-Aktion {name} doppelt registriert")
    CONFIRM_ACTIONS[name] = func


def _store(ctx: ToolContext) -> ConfirmStore:
    return ctx.extras.setdefault("confirm", ConfirmStore())


def request_confirmation(
    ctx: ToolContext, action: str, params: dict[str, Any] | None, *, prompt: str, message: str
) -> dict:
    """Legt eine Bestätigung an und liefert das einheitliche ``confirm_required``-Ergebnis."""
    if action not in CONFIRM_ACTIONS:
        raise ToolError(f"Interner Fehler: unbekannte Bestätigungs-Aktion {action!r}.")
    store = _store(ctx)
    confirm_id = store.create(action, params)
    return {
        "status": "confirm_required",
        "action": action,
        "confirm_id": confirm_id,
        "expires_in": int(store.ttl),
        "prompt": prompt,
        "message": message,
    }


def pc_shutdown(ctx: ToolContext, _params) -> dict:
    return request_confirmation(
        ctx,
        "pc_shutdown",
        None,
        prompt="PC herunterfahren? Er fährt 15 s nach der Bestätigung herunter.",
        message="PC herunterfahren? Bitte in der Oberfläche bestätigen.",
    )


def pc_shutdown_cancel(_ctx: ToolContext, _params) -> dict:
    proc = run_command(CANCEL_CMD)
    if proc.returncode != 0:
        # shutdown /a meldet einen Fehler, wenn gar kein Herunterfahren geplant ist.
        return {"status": "nothing_to_cancel", "message": "Es war kein Herunterfahren geplant."}
    return {"status": "cancelled", "message": "Herunterfahren abgebrochen."}


async def _do_shutdown(_ctx: ToolContext, _params: dict[str, Any]) -> dict:
    proc = await anyio.to_thread.run_sync(run_command, SHUTDOWN_CMD)
    if proc.returncode != 0:
        raise ToolError(f"Befehl fehlgeschlagen: {(proc.stderr or proc.stdout).strip()}")
    return {"status": "shutdown_scheduled", "message": "PC fährt in 15 Sekunden herunter."}


register_confirm_action("pc_shutdown", _do_shutdown)


async def confirm(ctx: ToolContext, confirm_id: str) -> dict:
    """Wird ausschließlich vom HTTP-Endpunkt /api/confirm/{id} aufgerufen, nie vom LLM."""
    pending = _store(ctx).consume(confirm_id)
    if pending is None:
        raise ToolError("Unbekannte oder abgelaufene Bestätigung.")
    func = CONFIRM_ACTIONS.get(pending.action)
    if func is None:  # pragma: no cover - nur bei Programmierfehlern
        raise ToolError("Unbekannte Bestätigungs-Aktion.")
    if inspect.iscoroutinefunction(func):
        result = await func(ctx, pending.params)
    else:
        result = await anyio.to_thread.run_sync(func, ctx, pending.params)
    return {"action": pending.action, **result}


def register(registry: Registry) -> None:
    registry.tool(
        "pc_shutdown",
        "Fährt den PC herunter. Fordert nur eine Bestätigung an; der User muss in der "
        "Oberfläche auf 'Bestätigen' tippen.",
    )(pc_shutdown)
    registry.tool("pc_shutdown_cancel", "Bricht ein geplantes Herunterfahren ab.")(pc_shutdown_cancel)
