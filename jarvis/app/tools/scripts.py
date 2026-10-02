"""Skripte aus der Config starten, stoppen und überwachen.

- Start nur per Argumentliste (nie shell=True), in eigener Prozessgruppe.
- PID + Startzeit werden in state/scripts.json gemerkt; psutil prüft damit nach einem
  Server-Neustart, ob genau dieser Prozess noch läuft (Schutz gegen wiederverwendete PIDs).
- Stop: erst sanft (POSIX: SIGTERM an die Gruppe, Windows: taskkill ohne /F = WM_CLOSE),
  nach 5 s hart (Prozessbaum per psutil beenden).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import psutil
from pydantic import BaseModel, ConfigDict, Field

from app.config import ScriptConfig
from app.tools.registry import Registry, ToolContext, ToolError

STOP_GRACE_SECONDS = 5.0
IS_WINDOWS = os.name == "nt"


class ScriptParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script_id: str = Field(min_length=1, max_length=64, description="ID des Skripts aus scripts_list")


class OptionalScriptParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script_id: str | None = Field(None, max_length=64, description="ID des Skripts; leer = alle")


class ScriptManager:
    def __init__(self, state_dir: Path, grace: float = STOP_GRACE_SECONDS) -> None:
        self.state_file = state_dir / "scripts.json"
        self.log_dir = state_dir / "logs"
        self.grace = grace
        self._lock = threading.RLock()

    # --- Zustand -----------------------------------------------------------------
    def _load(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def process(self, script_id: str) -> psutil.Process | None:
        entry = self._load().get(script_id)
        if not isinstance(entry, dict):
            return None
        try:
            proc = psutil.Process(int(entry["pid"]))
            if abs(proc.create_time() - float(entry["create_time"])) > 1.0:
                return None  # PID wurde von einem anderen Prozess wiederverwendet
            if proc.status() == psutil.STATUS_ZOMBIE:
                proc.wait(timeout=0)
                return None
            return proc
        except (psutil.Error, KeyError, ValueError, TypeError, OSError):
            return None

    def status(self, cfg: ScriptConfig) -> dict:
        with self._lock:
            proc = self.process(cfg.id)
            entry = self._load().get(cfg.id) or {}
        return {
            "id": cfg.id,
            "label": cfg.label,
            "configured": cfg.is_configured,
            "running": proc is not None,
            "pid": proc.pid if proc else None,
            "started_at": entry.get("started_at") if proc else None,
        }

    # --- Start / Stop -----------------------------------------------------------
    def start(self, cfg: ScriptConfig) -> dict:
        if not cfg.is_configured:
            raise ToolError(
                f"Skript '{cfg.label}' ist noch nicht eingerichtet: 'cwd' und 'command' in config.yaml "
                "enthalten noch TODO-Platzhalter."
            )
        cwd = Path(cfg.cwd)
        if not cwd.is_dir():
            raise ToolError(f"Arbeitsordner für '{cfg.label}' existiert nicht (Wert von 'cwd' prüfen).")
        with self._lock:
            if cfg.single_instance and self.process(cfg.id) is not None:
                raise ToolError(f"'{cfg.label}' läuft bereits.")
            self.log_dir.mkdir(parents=True, exist_ok=True)
            kwargs: dict = {"cwd": str(cwd), "stdin": subprocess.DEVNULL}
            log_file = None
            if IS_WINDOWS:
                flags = subprocess.CREATE_NEW_PROCESS_GROUP
                if cfg.hide_window:
                    flags |= subprocess.CREATE_NO_WINDOW
                else:
                    flags |= subprocess.CREATE_NEW_CONSOLE  # eigenes Fenster, Ausgabe dort sichtbar
                kwargs["creationflags"] = flags
            else:
                kwargs["start_new_session"] = True
            if not IS_WINDOWS or cfg.hide_window:
                log_file = open(self.log_dir / f"{cfg.id}.log", "ab")
                kwargs["stdout"] = log_file
                kwargs["stderr"] = subprocess.STDOUT
            try:
                proc = subprocess.Popen(list(cfg.command), shell=False, **kwargs)
            except FileNotFoundError:
                raise ToolError(
                    f"Programm für '{cfg.label}' nicht gefunden: erstes Element von 'command' prüfen."
                ) from None
            except OSError as exc:
                raise ToolError(f"Start von '{cfg.label}' fehlgeschlagen: {exc.strerror or exc}") from None
            finally:
                if log_file:
                    log_file.close()
            try:
                create_time = psutil.Process(proc.pid).create_time()
            except psutil.Error:
                create_time = time.time()
            data = self._load()
            data[cfg.id] = {
                "pid": proc.pid,
                "create_time": create_time,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._save(data)
            # Popen-Objekt merken, damit der Prozess nach dem Ende sauber eingesammelt wird.
            _CHILDREN[proc.pid] = proc
        return {"status": "started", **self.status(cfg)}

    def stop(self, cfg: ScriptConfig) -> dict:
        with self._lock:
            proc = self.process(cfg.id)
            if proc is None:
                self._forget(cfg.id)
                return {"status": "not_running", **self.status(cfg)}
            try:
                tree = [proc, *proc.children(recursive=True)]
            except psutil.Error:
                tree = [proc]
            self._soft_stop(proc)
            _gone, alive = psutil.wait_procs(tree, timeout=self.grace)
            forced = False
            if alive:
                forced = True
                for p in alive:
                    try:
                        p.kill()
                    except psutil.Error:
                        pass
                psutil.wait_procs(alive, timeout=3)
            popen = _CHILDREN.pop(proc.pid, None)
            if popen is not None:
                try:
                    popen.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            self._forget(cfg.id)
        return {"status": "killed" if forced else "stopped", **self.status(cfg)}

    def _soft_stop(self, proc: psutil.Process) -> None:
        try:
            if IS_WINDOWS:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T"],
                    capture_output=True,
                    timeout=5,
                    creationflags=flags,
                )
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    proc.terminate()
        except (psutil.Error, OSError, subprocess.SubprocessError):
            pass

    def _forget(self, script_id: str) -> None:
        data = self._load()
        if data.pop(script_id, None) is not None:
            self._save(data)


_CHILDREN: dict[int, subprocess.Popen] = {}


def _manager(ctx: ToolContext) -> ScriptManager:
    return ctx.extras.setdefault("scripts", ScriptManager(ctx.state_dir))


def _script(ctx: ToolContext, script_id: str) -> ScriptConfig:
    cfg = ctx.config.script(script_id.strip().lower())
    if cfg is None:
        known = ", ".join(s.id for s in ctx.config.scripts) or "keine"
        raise ToolError(f"Unbekanntes Skript '{script_id}'. Verfügbar: {known}.")
    return cfg


def scripts_list(ctx: ToolContext, _p) -> dict:
    mgr = _manager(ctx)
    return {"scripts": [mgr.status(s) for s in ctx.config.scripts]}


def scripts_status(ctx: ToolContext, p: OptionalScriptParams) -> dict:
    if not p.script_id:
        return scripts_list(ctx, p)
    return _manager(ctx).status(_script(ctx, p.script_id))


def scripts_start(ctx: ToolContext, p: ScriptParams) -> dict:
    return _manager(ctx).start(_script(ctx, p.script_id))


def scripts_stop(ctx: ToolContext, p: ScriptParams) -> dict:
    return _manager(ctx).stop(_script(ctx, p.script_id))


def register(registry: Registry) -> None:
    registry.tool("scripts_list", "Listet alle konfigurierten Skripte mit Zustand.")(scripts_list)
    registry.tool("scripts_status", "Zustand eines Skripts (oder aller) abfragen.", OptionalScriptParams)(
        scripts_status
    )
    registry.tool("scripts_start", "Startet ein konfiguriertes Skript.", ScriptParams)(scripts_start)
    registry.tool("scripts_stop", "Stoppt ein laufendes Skript.", ScriptParams)(scripts_stop)

