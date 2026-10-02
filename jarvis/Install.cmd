@echo off
rem JARVIS installieren oder aktualisieren - pro Benutzer, ohne Adminrechte.
rem Doppelklick genuegt. Optionen werden an scripts\install.ps1 weitergereicht, z. B.:
rem   Install.cmd -Target D:\Tools\JARVIS -NoAutostart
rem Mit JARVIS_NOPAUSE=1 schliesst das Fenster am Ende ohne Tastendruck.
setlocal EnableExtensions DisableDelayedExpansion
if not exist "%~dp0scripts\install.ps1" echo FEHLER: scripts\install.ps1 fehlt. Bitte das ZIP-Archiv zuerst komplett entpacken und Install.cmd im entpackten Ordner starten.
if not exist "%~dp0scripts\install.ps1" if not "%JARVIS_NOPAUSE%"=="1" pause
if not exist "%~dp0scripts\install.ps1" exit /b 2
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
set "JARVIS_RC=%ERRORLEVEL%"
if not "%JARVIS_NOPAUSE%"=="1" pause
exit /b %JARVIS_RC%
