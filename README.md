# Claude-code

## JARVIS – lokaler Assistent für den Windows-PC

Das Projekt liegt im Ordner [`jarvis/`](jarvis/): Web-Oberfläche fürs Handy (LED-Strip, Zimmersensoren,
Skripte, PC), lokales Ollama-Sprachmodell, feste PC-Aktionen (Lautstärke, Medientasten, PC sperren, Webseiten,
Programme aus einer festen Liste – keine freie Maus-/Tastatursteuerung), „Bildschirm beschreiben“ mit einem
lokalen Vision-Modell und eine Stimme (Windows-Sprachausgabe, Spracheingabe lokal mit Whisper). Alles läuft auf
dem eigenen PC, ohne Cloud-Dienst.

Installation ohne Adminrechte: Repository als ZIP herunterladen (*Code* → *Download ZIP*), entpacken (z. B. nach
`C:\JARVIS-Setup`, nicht in einen OneDrive-Ordner) und im Unterordner `jarvis` doppelt auf `Install.cmd` klicken.

Alle Schritte, Firewall, Tailscale (auch HTTPS fürs Handy-Mikrofon) und Fehlerbehebung:
[jarvis/README.md](jarvis/README.md).
