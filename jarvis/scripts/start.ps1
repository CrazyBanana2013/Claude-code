# Startet JARVIS im Vordergrund (Konsole bleibt offen, Strg+C beendet).
# Aufruf ohne Adminrechte:  powershell -ExecutionPolicy Bypass -File scripts\start.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Keine virtuelle Umgebung gefunden (.venv). Einmalig im Ordner '$root' ausfuehren:" -ForegroundColor Yellow
    Write-Host "    uv sync"
    exit 1
}
if (-not (Test-Path (Join-Path $root "config.yaml"))) {
    Write-Host "config.yaml fehlt. Einmalig anlegen und ausfuellen:" -ForegroundColor Yellow
    Write-Host "    copy config.example.yaml config.yaml"
    exit 1
}

& $python -m app
exit $LASTEXITCODE
