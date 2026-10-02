"""Token-Authentifizierung mit Sperre nach Fehlversuchen."""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Callable

import yaml
from fastapi import HTTPException, Request

MAX_FAILURES = 5
LOCKOUT_SECONDS = 60


def load_or_create_token(path: Path, announce: Callable[[str], None] = print) -> str:
    """Liest den API-Token aus secrets.yaml oder erzeugt beim ersten Start einen neuen."""
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        token = data.get("api_token") if isinstance(data, dict) else None
        if isinstance(token, str) and len(token) >= 32:
            return token
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Von JARVIS erzeugt. Nicht committen, nicht teilen.\n"
        + yaml.safe_dump({"api_token": token}),
        encoding="utf-8",
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    announce(
        "\n=== JARVIS: Neuer API-Token erzeugt (wird nur jetzt angezeigt) ===\n"
        f"{token}\n"
        f"Gespeichert in: {path}\n"
        "Beim ersten Öffnen der Oberfläche eingeben.\n"
    )
    return token


class Lockout:
    """Zählt Fehlversuche pro IP; nach MAX_FAILURES ist die IP LOCKOUT_SECONDS gesperrt."""

    def __init__(
        self,
        max_failures: int = MAX_FAILURES,
        lock_seconds: float = LOCKOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self.clock = clock
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def remaining(self, ip: str) -> float:
        with self._lock:
            until = self._locked_until.get(ip)
            if until is None:
                return 0.0
            left = until - self.clock()
            if left <= 0:
                del self._locked_until[ip]
                self._failures.pop(ip, None)
                return 0.0
            return left

    def fail(self, ip: str) -> None:
        with self._lock:
            count = self._failures.get(ip, 0) + 1
            self._failures[ip] = count
            if count >= self.max_failures:
                self._locked_until[ip] = self.clock() + self.lock_seconds

    def success(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


class TokenAuth:
    """FastAPI-Dependency: verlangt `Authorization: Bearer <token>`."""

    def __init__(self, token: str, lockout: Lockout | None = None) -> None:
        self._token = token.encode()
        self.lockout = lockout or Lockout()

    def __call__(self, request: Request) -> None:
        ip = request.client.host if request.client else "unbekannt"
        left = self.lockout.remaining(ip)
        if left > 0:
            raise HTTPException(
                status_code=429,
                detail=f"Zu viele Fehlversuche. Gesperrt für {int(left) + 1} s.",
                headers={"Retry-After": str(int(left) + 1)},
            )
        header = request.headers.get("authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or not supplied:
            self.lockout.fail(ip)
            raise HTTPException(
                status_code=401,
                detail="Token fehlt.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not hmac.compare_digest(supplied.strip().encode(), self._token):
            self.lockout.fail(ip)
            raise HTTPException(
                status_code=401,
                detail="Token ungültig.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        self.lockout.success(ip)
