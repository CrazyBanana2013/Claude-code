# Beendet einen per Autostart (pythonw.exe -m app) laufenden JARVIS-Server dieses Ordners.
# Aufruf:  powershell -ExecutionPolicy Bypass -File scripts\stop.ps1
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv"
$procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*-m app*" -and $_.ExecutablePath -like "$venv*" }
if (-not $procs) { Write-Host "JARVIS laeuft nicht."; exit 0 }
foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force
    Write-Host "Beendet: PID $($p.ProcessId)"
}
