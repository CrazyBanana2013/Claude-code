"""Dummy-Skript für Tests: schreibt seine PID und läuft, bis es beendet wird."""
import os
import signal
import sys
import time
from pathlib import Path

marker = Path(sys.argv[1])
ignore_term = len(sys.argv) > 2 and sys.argv[2] == "--ignore-term"
if ignore_term and hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
marker.write_text(str(os.getpid()))
while True:
    time.sleep(0.1)
