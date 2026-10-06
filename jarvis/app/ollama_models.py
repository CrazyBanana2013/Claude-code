"""Installierte Ollama-Modelle nur lesend abfragen (für pick_model.py und den Einrichtungsassistenten).

Genutzt werden ausschließlich lesende Endpunkte:
  GET  /api/tags  – installierte Modelle mit Größe
  POST /api/show  – Details eines Modells, Feld "capabilities": "tools" = kann Tool-Aufrufe (Chat-Modell
                    llm.model), "vision" = versteht Bilder (Bildschirm beschreiben, vision.model). Laut
                    Ollama-Doku docs/api.md, Beispielantwort für llava: capabilities ["completion", "vision"].
Es wird nie ein Modell gezogen, gelöscht oder geladen.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class OllamaUnavailable(Exception):
    """Ollama ist nicht erreichbar oder antwortet unerwartet. Nachricht ist für Menschen gedacht."""


@dataclass(frozen=True)
class ModelInfo:
    name: str
    size: int
    capabilities: tuple[str, ...]

    @property
    def supports_tools(self) -> bool:
        return "tools" in self.capabilities

    @property
    def supports_vision(self) -> bool:
        return "vision" in self.capabilities

    @property
    def size_gb(self) -> float:
        return self.size / 1e9


def _int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def list_models(client: httpx.Client, base: str, *, timeout: float | None = None) -> list[ModelInfo]:
    """Alle installierten Modelle mit ihren capabilities, in der Reihenfolge von /api/tags.

    Wirft OllamaUnavailable, wenn /api/tags nicht klappt. Schlägt nur /api/show für ein einzelnes
    Modell fehl, bekommt dieses Modell leere capabilities (zählt also nicht als Tool-Modell).
    """
    base = base.rstrip("/")
    kwargs = {} if timeout is None else {"timeout": timeout}
    try:
        resp = client.get(f"{base}/api/tags", **kwargs)
    except httpx.TimeoutException:
        raise OllamaUnavailable(f"Ollama antwortet nicht ({base}, Zeitüberschreitung)") from None
    except httpx.ConnectError:
        raise OllamaUnavailable(f"Ollama nicht erreichbar ({base}, Verbindung abgelehnt – läuft Ollama?)") from None
    except httpx.HTTPError as exc:
        raise OllamaUnavailable(f"Ollama nicht erreichbar ({base}, {type(exc).__name__})") from None
    except (httpx.InvalidURL, ValueError, UnicodeError):  # z. B. von Hand eingetragene http://127.0.0.256:11434
        raise OllamaUnavailable(f"Ungültige Ollama-Adresse ({base}) – llm.base_url in config.yaml prüfen") from None
    if resp.status_code != 200:
        raise OllamaUnavailable(f"Ollama antwortet mit HTTP {resp.status_code} ({base}/api/tags)")
    try:
        payload = resp.json()
    except ValueError:
        raise OllamaUnavailable(f"Keine gültige Antwort von {base}/api/tags – ist das Ollama?") from None
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise OllamaUnavailable(f"Unerwartete Antwort von {base}/api/tags – ist das Ollama?")

    models: list[ModelInfo] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("model")
        if not isinstance(name, str) or not name:
            continue
        caps = _capabilities(client, base, name, kwargs)
        models.append(ModelInfo(name=name, size=_int(entry.get("size")), capabilities=caps))
    return models


def _capabilities(client: httpx.Client, base: str, name: str, kwargs: dict) -> tuple[str, ...]:
    try:
        show = client.post(f"{base}/api/show", json={"model": name}, **kwargs)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, UnicodeError):
        return ()
    if show.status_code != 200:
        return ()
    try:
        caps = show.json().get("capabilities")
    except (ValueError, AttributeError):
        return ()
    if not isinstance(caps, list):
        return ()
    return tuple(c for c in caps if isinstance(c, str))


def tool_models(models: list[ModelInfo]) -> list[ModelInfo]:
    """Nur Modelle mit capability "tools", kleinstes zuerst (bei gleicher Größe nach Name)."""
    return sorted((m for m in models if m.supports_tools), key=lambda m: (m.size, m.name))


def vision_models(models: list[ModelInfo]) -> list[ModelInfo]:
    """Nur Modelle mit capability "vision" (verstehen Bilder), kleinstes zuerst (bei gleicher Größe nach Name)."""
    return sorted((m for m in models if m.supports_vision), key=lambda m: (m.size, m.name))
