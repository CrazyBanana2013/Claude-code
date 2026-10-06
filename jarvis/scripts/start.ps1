# Startet JARVIS im Vordergrund (Konsole bleibt offen, Strg+C beendet).
# Aufruf ohne Adminrechte:  powershell -ExecutionPolicy Bypass -File scripts\start.ps1
# (Mit dem Installer: Startmenue > JARVIS > JARVIS starten - ohne Fenster.)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
# -LiteralPath: Ordnernamen mit [ ] sind sonst Platzhalter.
Set-Location -LiteralPath $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "Keine virtuelle Umgebung gefunden (.venv) in '$root'." -ForegroundColor Yellow
    Write-Host "    Install.cmd ausfuehren (oder im Repo-Ordner: uv sync)"
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $root "config.yaml"))) {
    Write-Host "config.yaml fehlt in '$root'." -ForegroundColor Yellow
    Write-Host "    Install.cmd ausfuehren (startet den Einrichtungsassistenten)"
    Write-Host "    oder von Hand: copy config.example.yaml config.yaml"
    exit 1
}

& $python -m app
exit $LASTEXITCODE
