"""Alle Tools registrieren."""

from app.tools.registry import Registry


def register_all(registry: Registry) -> None:
    from app.tools import led, pc, scripts, sensors

    for module in (pc, led, sensors, scripts):
        module.register(registry)
