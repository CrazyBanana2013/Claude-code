# Beendet den JARVIS-Server dieses Ordners (gestartet per Autostart, Startmenue oder python -m app).
# Aufruf:  powershell -ExecutionPolicy Bypass -File scripts\stop.ps1
#   -Target <Ordner>  anderer Installationsordner (Standard: der Ordner ueber scripts\)
#   -Pause            Fenster danach 3 Sekunden offen lassen (fuer die Startmenue-Verknuepfung)
# Erkennung: zuerst state\server.pid (Programm muss in .venv dieses Ordners liegen), sonst Suche
# ueber alle Python-Prozesse mit "-m app" aus dieser .venv. Keine Adminrechte noetig.
param([string]$Target, [switch]$Pause)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'installer-lib.ps1')

$exitCode = 0
try {
    if (-not $Target) { $Target = Split-Path -Parent $PSScriptRoot }
    $Target = Get-JarvisFullPath $Target
    $count = Stop-JarvisServer -Target $Target
    if ($count -eq 0) {
        Write-Host 'JARVIS laeuft nicht.'
    } elseif (@(Find-JarvisServerProcesses -Target $Target).Count -gt 0) {
        Write-Host 'JARVIS laeuft noch - bitte abmelden/anmelden.' -ForegroundColor Yellow
        $exitCode = 2
    } else {
        Write-Host 'JARVIS beendet.'
    }
} catch {
    Write-Host ('FEHLER: ' + (Get-JarvisErrorMessage $_)) -ForegroundColor Red
    $exitCode = 2
}
if ($Pause) { Start-Sleep -Seconds 3 }
exit $exitCode
