# Legt eine Verknuepfung "JARVIS" im Autostart-Ordner des aktuellen Benutzers an (shell:startup).
# Startet JARVIS beim Anmelden ohne Konsolenfenster (pythonw.exe). Keine Adminrechte noetig.
#   Installieren:  powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
#   Entfernen:     powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove
# Install.cmd erledigt das auf Wunsch mit; dieses Skript ist fuer spaetere Aenderungen.
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'installer-lib.ps1')

$root = Split-Path -Parent $PSScriptRoot

function Update-ManifestAutostart {
    # Haelt den Installationsmarker aktuell, damit die Deinstallation den Eintrag mit entfernt.
    param([string]$Path)
    $manifest = $null
    try { $manifest = Read-JarvisManifest $root } catch { return }
    if (-not $manifest) { return }
    $m = ConvertTo-JarvisManifestDict $manifest
    $m['autostart'] = $null
    if ($Path) { $m['autostart'] = $Path }
    Write-JarvisManifest -Target $root -Manifest $m
}

try {
    if (-not (Test-JarvisWindows)) { throw 'Autostart gibt es nur unter Windows.' }
    if ($Remove) {
        $link = Get-JarvisAutostartPath
        $status = Remove-JarvisAutostart -Path $link -Owner $root
        if ($status -eq 'removed') {
            Write-Host ('Autostart entfernt: ' + $link)
        } elseif ($status -eq 'foreign') {
            Write-Host ('Der Autostart-Eintrag {0} startet eine andere JARVIS-Installation ({1}) und bleibt bestehen.' -f $link, (Get-JarvisShortcutTargetPath $link)) -ForegroundColor Yellow
        } else {
            Write-Host 'Kein Autostart-Eintrag vorhanden.'
        }
        Update-ManifestAutostart -Path $null
        exit 0
    }
    $python = Get-JarvisVenvPython -Target $root
    if (-not [System.IO.File]::Exists($python)) {
        Write-Host ("Keine virtuelle Umgebung gefunden (.venv). Zuerst Install.cmd ausfuehren (oder im Ordner '{0}': uv sync)." -f $root) -ForegroundColor Yellow
        exit 1
    }
    if (-not [System.IO.File]::Exists((Join-Path $root 'config.yaml'))) {
        Write-Host 'Warnung: config.yaml fehlt noch - JARVIS wird beim Start abbrechen (siehe state\logs\server.log).' -ForegroundColor Yellow
    }
    $auto = New-JarvisAutostart -Target $root
    if ($auto.Fallback) {
        Write-Host 'pythonw.exe nicht gefunden - nutze python.exe (minimiertes Konsolenfenster).' -ForegroundColor Yellow
    }
    Update-ManifestAutostart -Path $auto.Path
    Write-Host ('Autostart eingerichtet: ' + $auto.Path)
    Write-Host ('Ziel: {0} -m app  (Arbeitsordner: {1})' -f $auto.Executable, $root)
    Write-Host ('Logs: ' + (Join-Path (Join-Path $root 'state') 'logs\server.log'))
} catch {
    Write-Host ('FEHLER: ' + (Get-JarvisErrorMessage $_)) -ForegroundColor Red
    exit 2
}
exit 0
