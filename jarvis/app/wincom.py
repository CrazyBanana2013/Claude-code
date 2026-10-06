"""COM für die Worker-Threads von JARVIS (Lautstärke, Sprachausgabe) – nur unter Windows benutzt.

Beide Threads laufen als MTA (COINIT_MULTITHREADED) statt als STA: Ein STA-Thread bekommt ein verstecktes
Fenster (Klasse OleMainThreadWndClass) und MUSS Nachrichten verarbeiten – laut MicrosoftDocs win32
``com/single-threaded-apartments.md`` auch ein reiner Client, damit Broadcast-Nachrichten anderer Programme
(SendMessage an HWND_BROADCAST, DDE) beantwortet werden. Unsere Threads warten aber nur auf eine Queue bzw.
Condition; ein Programm, das ohne Zeitlimit broadcastet, bliebe dann hängen. Ein MTA braucht weder
Nachrichtenschleife noch Fenster.

SAPI.SpVoice und MMDeviceEnumerator sind (laut Registry auf üblichen Windows-Installationen) mit
ThreadingModel "Both" registriert und laufen damit direkt im MTA; wäre es "Apartment", legte COM selbst
einen Host-Thread an – es funktioniert also in beiden Fällen (am echten PC nicht geprüft).
"""

from __future__ import annotations

import sys
from typing import Any

COINIT_MULTITHREADED = 0x0


def init_mta() -> Any:
    """COM für den aufrufenden Thread als MTA initialisieren; liefert das comtypes-Modul.

    comtypes initialisiert COM schon beim ERSTEN Import für den importierenden Thread – mit
    ``sys.coinit_flags``, sonst als STA. Deshalb wird der Wert vor dem Import gesetzt. Der eigene
    CoInitializeEx-Aufruf gilt für Threads, in denen comtypes schon importiert war (S_FALSE schadet nicht).
    """
    sys.coinit_flags = COINIT_MULTITHREADED
    import comtypes

    comtypes.CoInitializeEx(COINIT_MULTITHREADED)
    return comtypes
