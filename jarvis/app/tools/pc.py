"""Platzhalter – wird in Meilenstein M2 implementiert."""

from app.tools.registry import ToolError


def register(registry) -> None:
    pass


async def confirm(ctx, confirm_id: str) -> dict:
    raise ToolError("Unbekannte oder abgelaufene Bestätigung.")
