# Legt eine Verknuepfung "JARVIS" im Autostart-Ordner des aktuellen Benutzers an (shell:startup).
# Startet JARVIS beim Anmelden ohne Konsolenfenster (pythonw.exe). Keine Adminrechte noetig.
#   Installieren:  powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
#   Entfernen:     powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove
param([switch]$Remove)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$startup = [Environment]::GetFolderPath("Startup")
$link = Join-Path $startup "JARVIS.lnk"

if ($Remove) {
    if (Test-Path $link) { Remove-Item $link; Write-Host "Autostart entfernt: $link" }
    else { Write-Host "Kein Autostart-Eintrag vorhanden." }
    exit 0
}

$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$python = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $pythonw) {
    $target = $pythonw
} elseif (Test-Path $python) {
    # Fallback: mit Konsole, aber minimiert.
    $target = $python
    Write-Host "pythonw.exe nicht gefunden - nutze python.exe (minimiertes Konsolenfenster)." -ForegroundColor Yellow
} else {
    Write-Host "Keine virtuelle Umgebung gefunden (.venv). Zuerst im Ordner '$root' ausfuehren: uv sync" -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path (Join-Path $root "config.yaml"))) {
    Write-Host "Warnung: config.yaml fehlt noch - JARVIS wird beim Start abbrechen (siehe state\logs\server.log)." -ForegroundColor Yellow
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $target
$shortcut.Arguments = "-m app"
$shortcut.WorkingDirectory = $root
$shortcut.WindowStyle = 7   # minimiert
$shortcut.Description = "JARVIS - lokaler Assistent"
$shortcut.Save()

Write-Host "Autostart eingerichtet: $link"
Write-Host "Ziel: $target -m app  (Arbeitsordner: $root)"
Write-Host "Logs: $root\state\logs\server.log"
