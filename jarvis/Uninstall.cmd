@echo off
rem JARVIS deinstallieren - pro Benutzer, ohne Adminrechte. Doppelklick genuegt.
rem Optionen werden an scripts\uninstall.ps1 weitergereicht:
rem   -Purge  auch config.yaml, secrets.yaml und state\ loeschen
rem   -Yes    ohne Rueckfragen
rem Mit JARVIS_NOPAUSE=1 schliesst das Fenster am Ende ohne Tastendruck.
setlocal EnableExtensions DisableDelayedExpansion
set "JARVIS_UNINSTALL_PS1=%~dp0scripts\uninstall.ps1"
if not exist "%JARVIS_UNINSTALL_PS1%" echo FEHLER: scripts\uninstall.ps1 fehlt - ist JARVIS hier installiert?
if not exist "%JARVIS_UNINSTALL_PS1%" if not "%JARVIS_NOPAUSE%"=="1" pause
if not exist "%JARVIS_UNINSTALL_PS1%" exit /b 2
rem Den Installationsordner als Arbeitsordner verlassen, sonst kann Windows ihn nicht loeschen.
cd /d "%TEMP%"
setlocal EnableDelayedExpansion
rem Der Rest steht in einem Block: cmd liest ihn vollstaendig ein, bevor uninstall.ps1 diese Datei loescht.
(
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "!JARVIS_UNINSTALL_PS1!" %*
  set "JARVIS_RC=!ERRORLEVEL!"
  if not "!JARVIS_NOPAUSE!"=="1" pause
  exit /b !JARVIS_RC!
)
