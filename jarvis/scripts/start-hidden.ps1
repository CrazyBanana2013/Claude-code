# Startet den JARVIS-Server dieses Ordners ohne Konsolenfenster (pythonw.exe) und gibt Rueckmeldung:
# wartet bis zu 20 Sekunden auf /api/health und oeffnet dann die Oberflaeche im Browser. Klappt der
# Start nicht, zeigt das Fenster die letzten Zeilen aus dem Log (mit Hinweis) und wartet auf Enter.
# Ziel der Startmenue-Verknuepfung "JARVIS starten". Keine Adminrechte noetig.
#   powershell -ExecutionPolicy Bypass -File scripts\start-hidden.ps1
#   -Target <Ordner>  anderer Installationsordner (Standard: der Ordner ueber scripts\)
#   -NoBrowser        Oberflaeche nicht oeffnen (fuer Tests)
#   -NoPause          bei Fehlern nicht auf Enter warten (fuer Tests)
param([string]$Target, [switch]$NoBrowser, [switch]$NoPause, [int]$TimeoutSec = 20)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'installer-lib.ps1')

function Wait-ForEnter {
    if ($NoPause) { return }
    try { [void](Read-Host 'Enter zum Schliessen') } catch { }
}

$exitCode = 0
try {
    if (-not $Target) { $Target = Split-Path -Parent $PSScriptRoot }
    $Target = Get-JarvisFullPath $Target
    $python = Get-JarvisVenvPython -Target $Target
    if (-not [System.IO.File]::Exists($python)) {
        throw ('Keine virtuelle Umgebung gefunden ({0}). Bitte Install.cmd ausfuehren.' -f $python)
    }
    $port = 8765
    $localUrl = 'http://127.0.0.1:8765/'
    $info = Get-JarvisServerInfo -Python $python -Target $Target
    if ($info) {
        if ($info.port) { $port = [int]$info.port }
        if ($info.local_url) { $localUrl = [string]$info.local_url }
    }
    if (-not $localUrl.EndsWith('/')) { $localUrl = $localUrl + '/' }
    $healthUrl = $localUrl + 'api/health'

    $own = @(Find-JarvisServerProcesses -Target $Target)
    if ((Test-JarvisHealth -Url $healthUrl) -and $own.Count -gt 0) {
        Write-Host ('JARVIS laeuft bereits: ' + $localUrl) -ForegroundColor Green
    } elseif (Test-JarvisHealth -Url $healthUrl) {
        throw ('Unter {0} antwortet schon ein anderer Server (eigener Token, eigene config.yaml). Diesen zuerst beenden.' -f $localUrl)
    } else {
        $proc = $null
        if ($own.Count -eq 0) {
            Write-Host 'JARVIS startet ...'
            $proc = Start-JarvisServer -Target $Target
        } else {
            Write-Host 'JARVIS startet gerade ...'
        }
        if (-not (Wait-JarvisHealth -Url $healthUrl -TimeoutSec $TimeoutSec -Process $proc)) {
            $logPath = Get-JarvisServerLogHint -Target $Target
            $tail = @(Get-JarvisLogTail -Path $logPath -Lines 10)
            Write-Host ('JARVIS antwortet nicht. Log: ' + $logPath) -ForegroundColor Yellow
            foreach ($line in $tail) { Write-Host ('  ' + $line) }
            $problem = Get-JarvisStartProblem -LogLines $tail -Port $port
            if ($problem) { Write-Host $problem -ForegroundColor Yellow }
            $exitCode = 2
        } else {
            Write-Host ('JARVIS laeuft: ' + $localUrl) -ForegroundColor Green
        }
    }
    if ($exitCode -eq 0 -and -not $NoBrowser) {
        try { Start-Process -FilePath $localUrl } catch { Write-Host ('Browser konnte nicht geoeffnet werden: ' + $localUrl) }
    }
} catch {
    Write-Host ('FEHLER: ' + (Get-JarvisErrorMessage $_)) -ForegroundColor Red
    $exitCode = 2
}
if ($exitCode -ne 0) { Wait-ForEnter } else { Start-Sleep -Seconds 2 }
exit $exitCode
