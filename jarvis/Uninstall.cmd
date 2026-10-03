@echo off
rem JARVIS deinstallieren - pro Benutzer, ohne Adminrechte. Doppelklick genuegt.
rem Optionen werden an scripts\uninstall.ps1 weitergereicht:
rem   -Purge  auch config.yaml, secrets.yaml und state\ loeschen
rem   -Yes    ohne Rueckfragen
rem Mit JARVIS_NOPAUSE=1 schliesst das Fenster am Ende ohne Tastendruck.
setlocal EnableExtensions DisableDelayedExpansion
set "JARVIS_UNINSTALL_PS1=%~dp0scripts\uninstall.ps1"
rem Die Argumente jetzt merken, solange DelayedExpansion aus ist: Im Block unten wuerde ein
rem Ausrufezeichen in den Argumenten (z. B. in einem Pfad bei -Target) sonst verschluckt.
rem Ohne aeussere Anfuehrungszeichen, damit Anfuehrungszeichen in den Argumenten genauso
rem wirken wie direkt beim Aufruf.
set JARVIS_ARGS=%*
if not exist "%JARVIS_UNINSTALL_PS1%" echo FEHLER: scripts\uninstall.ps1 fehlt - ist JARVIS hier installiert?
if not exist "%JARVIS_UNINSTALL_PS1%" if not "%JARVIS_NOPAUSE%"=="1" pause
if not exist "%JARVIS_UNINSTALL_PS1%" exit /b 2
rem Den Installationsordner als Arbeitsordner verlassen, sonst kann Windows ihn nicht loeschen.
cd /d "%TEMP%"
setlocal EnableDelayedExpansion
rem Der Rest steht in einem Block: cmd liest ihn vollstaendig ein, bevor uninstall.ps1 diese Datei loescht.
rem JARVIS_ARGS wird erst beim Ausfuehren eingesetzt und dann nicht noch einmal ausgewertet.
(
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "!JARVIS_UNINSTALL_PS1!" !JARVIS_ARGS!
  set "JARVIS_RC=!ERRORLEVEL!"
  if not "!JARVIS_NOPAUSE!"=="1" pause
  exit /b !JARVIS_RC!
)
