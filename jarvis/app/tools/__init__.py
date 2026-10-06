"""Alle Tools registrieren."""

from app.tools.registry import Registry


def register_all(registry: Registry) -> None:
    from app.tools import desktop, led, pc, screen, scripts, sensors

    for module in (pc, led, sensors, scripts, desktop, screen):
        module.register(registry)
