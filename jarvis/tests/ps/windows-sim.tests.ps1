# Windows-Zweige von install.ps1/uninstall.ps1 ohne Windows: Die Quelle ist eine Kopie, deren
# installer-lib.ps1 am Ende Ersatzfunktionen bekommt (Test-JarvisWindows = $true, Verknuepfungen
# als JSON-Dateien in Test-Ordnern statt WScript.Shell, keine CIM-Abfragen). So laufen die
# Schritte 7 (Startmenue) und 8 (Autostart), das Manifest und die Deinstallation der
# Verknuepfungen mit den echten Skripten durch.
# Braucht uv mit gefuelltem Cache (UV_OFFLINE=1: es wird nichts heruntergeladen).
. (Join-Path $PSScriptRoot 'TestHelpers.ps1')

if (Test-JarvisWindows) { Skip-AllTests 'Simulation nur unter Linux/macOS noetig' }
if (-not (Find-JarvisUv)) { Skip-AllTests 'uv nicht gefunden' }
if (-not [System.IO.File]::Exists((Join-Path $script:JarvisRoot 'app/setup_wizard.py'))) {
    Skip-AllTests 'app/setup_wizard.py fehlt noch (wird parallel gebaut)'
}

$overrides = @'

# ---- Nur fuer tests/ps/windows-sim.tests.ps1 angehaengt ----
function Test-JarvisWindows { return $true }
function Test-JarvisElevated { return $false }
function Get-JarvisStartMenuDir { return (Join-Path $env:JARVIS_SIM_PROGRAMS 'JARVIS') }
function Get-JarvisStartupDir { return $env:JARVIS_SIM_STARTUP }
function Get-JarvisShortcutRoots { return @($env:JARVIS_SIM_PROGRAMS, $env:JARVIS_SIM_STARTUP) }
function Get-JarvisVenvPython {
    param([string]$Target, [switch]$Windowed)
    return (Join-Path (Join-Path (Join-Path $Target '.venv') 'bin') 'python')
}
function New-JarvisShortcut {
    param([string]$Path, [string]$TargetPath, [string]$Arguments = '', [string]$WorkingDirectory = '',
        [string]$Description = '', [int]$WindowStyle = 1)
    $m = [ordered]@{ TargetPath = $TargetPath; Arguments = $Arguments; WorkingDirectory = $WorkingDirectory; WindowStyle = $WindowStyle }
    [System.IO.File]::WriteAllText($Path, (ConvertTo-JarvisJson $m))
    return $Path
}
function Get-JarvisShortcutInfo {
    param([string]$Path)
    if (-not [System.IO.File]::Exists($Path)) { return $null }
    try { return ([System.IO.File]::ReadAllText($Path) | ConvertFrom-Json) } catch { return $null }
}
function Get-JarvisPythonProcessDetails { return @() }
function Get-JarvisIPv4Interfaces {
    return @([pscustomobject]@{ Address = '192.168.178.20'; Interface = 'WLAN' },
        [pscustomobject]@{ Address = '172.20.0.1'; Interface = 'vEthernet (WSL)' })
}
function Unblock-JarvisFiles {
    param([string]$Target, [string[]]$Files)
    [System.IO.File]::WriteAllText($env:JARVIS_SIM_UNBLOCK, [string]@($Files).Count)
}
'@

$src = New-TestDir 'quelle'
[void](Copy-JarvisProgramFiles -Source $script:JarvisRoot -Target $src -Files @(Get-JarvisProgramFiles -Source $script:JarvisRoot))
[System.IO.File]::AppendAllText((Join-Path $src 'scripts/installer-lib.ps1'), $overrides)

$programs = New-TestDir 'programs'
$startup = New-TestDir 'startup'
$unblock = Join-Path (New-TestDir 'unblock') 'count.txt'
$envVars = @{
    UV_OFFLINE = '1'; UV_PYTHON_DOWNLOADS = 'never'; UV_NO_PROGRESS = '1'
    JARVIS_SIM_PROGRAMS = $programs; JARVIS_SIM_STARTUP = $startup; JARVIS_SIM_UNBLOCK = $unblock
}
$t = Join-Path (New-TestDir 'ziel') 'JARVIS'
$menu = Join-Path $programs 'JARVIS'
$autoLink = Join-Path $startup 'JARVIS.lnk'
$oe = [string][char]0x00F6

function Invoke-SimInstall {
    param([string[]]$Arguments, [string]$InputText)
    return (Invoke-PwshScript -Script (Join-Path $src 'scripts/install.ps1') -Arguments (@('-Target', $t, '-NoStart') + $Arguments) `
            -Environment $envVars -InputText $InputText)
}

function Read-Link {
    param([string]$Path)
    return ([System.IO.File]::ReadAllText($Path) | ConvertFrom-Json)
}

Invoke-Test 'Installation (Windows simuliert): Startmenue, Autostart, Manifest' {
    $r = Invoke-SimInstall @('-Yes')
    if ($r.ExitCode -ne 0) { Write-TestOutput 'install.ps1' $r.Output }
    Assert-Equal 0 $r.ExitCode 'Exitcode'
    Assert-False ($r.Output -match 'Testmodus') 'echter Windows-Zweig, nicht der Testmodus'
    Assert-True ([int][System.IO.File]::ReadAllText($unblock) -gt 10) 'Unblock-File fuer die kopierten Dateien'
    $expected = @(('JARVIS ' + $oe + 'ffnen.url'), 'JARVIS starten.lnk', 'JARVIS beenden.lnk', 'JARVIS deinstallieren.lnk')
    $names = @([System.IO.Directory]::GetFiles($menu) | ForEach-Object { [System.IO.Path]::GetFileName($_) } | Sort-Object)
    Assert-Equal @($expected | Sort-Object) $names 'Startmenue-Eintraege'
    Assert-True ([System.IO.File]::ReadAllText((Join-Path $menu $expected[0])) -match 'URL=http://127\.0\.0\.1:8765/') '.url auf local_url'
    $start = Read-Link (Join-Path $menu 'JARVIS starten.lnk')
    Assert-Equal '-m app' $start.Arguments 'starten: -m app'
    Assert-Equal $t $start.WorkingDirectory 'starten: Arbeitsordner'
    $stop = Read-Link (Join-Path $menu 'JARVIS beenden.lnk')
    Assert-True ($stop.Arguments -like ('*-ExecutionPolicy Bypass -File "' + (Join-Path $t 'scripts') + '*stop.ps1" -Pause')) ('beenden: ' + $stop.Arguments)
    Assert-Equal (Join-Path $t 'Uninstall.cmd') (Read-Link (Join-Path $menu 'JARVIS deinstallieren.lnk')).TargetPath 'deinstallieren'
    $auto = Read-Link $autoLink
    Assert-Equal (Join-Path $t '.venv/bin/python') $auto.TargetPath 'Autostart-Ziel (venv-Python)'
    Assert-Equal 7 $auto.WindowStyle 'Autostart minimiert'
    $m = Read-JarvisManifest $t
    Assert-Equal @($names | ForEach-Object { Join-Path $menu $_ } | Sort-Object) @(Get-JarvisManifestList $m 'shortcuts' | Sort-Object) 'shortcuts im Manifest'
    Assert-Equal $autoLink $m.autostart 'autostart im Manifest'
    Assert-True ($r.Output -match 'http://192\.168\.178\.20:8765/\s+\(Adapter "WLAN"') 'LAN-Adresse mit Adapter'
    Assert-True ($r.Output -match 'Startmenue > JARVIS > JARVIS starten') 'Start-Hinweis aufs Startmenue'
}

Invoke-Test 'Update mit -NoShortcuts -NoAutostart behaelt die bisherigen Eintraege' {
    $r = Invoke-SimInstall @('-Yes', '-NoShortcuts', '-NoAutostart')
    Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
    $m = Read-JarvisManifest $t
    Assert-Equal 4 @(Get-JarvisManifestList $m 'shortcuts').Count 'shortcuts bleiben im Manifest'
    Assert-Equal $autoLink $m.autostart 'autostart bleibt im Manifest'
    Assert-True ([System.IO.File]::Exists($autoLink)) 'Autostart-Datei bleibt'
}

Invoke-Test 'Update interaktiv: Autostart abgelehnt -> Eintrag entfernt' {
    # Antworten: Konfiguration anpassen? n / Autostart? n / Zwischenablage? n
    $r = Invoke-SimInstall @() -InputText "n`nn`nn`n"
    if ($r.ExitCode -ne 0) { Write-TestOutput 'install.ps1' $r.Output }
    Assert-Equal 0 $r.ExitCode 'Exitcode'
    Assert-True ($r.Output -match 'automatisch \(ohne Fenster\) starten\? \(J/n\)') 'Frage mit Standard Ja'
    Assert-True ($r.Output -match 'Zwischenablage kopieren\? \(j/N\)') 'Zwischenablage nur angeboten'
    Assert-False ([System.IO.File]::Exists($autoLink)) 'Autostart entfernt'
    Assert-True ($null -eq (Read-JarvisManifest $t).autostart) 'autostart im Manifest = null'
    Assert-Equal 4 @(Get-JarvisManifestList (Read-JarvisManifest $t) 'shortcuts').Count 'Startmenue neu angelegt'
}

Invoke-Test 'Deinstallation: eigene Verknuepfungen weg, fremder Autostart bleibt' {
    $r = Invoke-SimInstall @('-Yes')
    Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
    # Eine andere Installation hat inzwischen den Autostart-Eintrag uebernommen:
    $other = New-TestDir 'andere'
    $json = ConvertTo-JarvisJson ([ordered]@{ TargetPath = (Join-Path $other '.venv/bin/python'); Arguments = '-m app'; WorkingDirectory = $other; WindowStyle = 7 })
    [System.IO.File]::WriteAllText($autoLink, $json)
    [void](Set-TestFile $menu 'Eigene Notiz.txt' 'MEINS')
    $r = Invoke-PwshScript -Script (Join-Path $t 'scripts/uninstall.ps1') -Arguments @('-Yes') -Environment $envVars -WorkingDirectory $t
    if ($r.ExitCode -ne 0) { Write-TestOutput 'uninstall.ps1' $r.Output }
    Assert-Equal 0 $r.ExitCode 'Exitcode'
    foreach ($n in @(('JARVIS ' + $oe + 'ffnen.url'), 'JARVIS starten.lnk', 'JARVIS beenden.lnk', 'JARVIS deinstallieren.lnk')) {
        Assert-False ([System.IO.File]::Exists((Join-Path $menu $n))) ('entfernt: ' + $n)
    }
    Assert-Equal 'MEINS' (Get-TestFile $menu 'Eigene Notiz.txt') 'fremde Datei im Startmenue-Ordner bleibt'
    Assert-True ([System.IO.File]::Exists($autoLink)) 'Autostart der anderen Installation bleibt'
    Assert-True ($r.Output -match 'anderen JARVIS-Installation') ('Hinweis: ' + $r.Output)
    Assert-True ([System.IO.Directory]::Exists($startup)) 'Autostart-Ordner selbst bleibt'
    Assert-False (Test-TestPath $t 'app') 'Programm entfernt'
    Assert-True (Test-TestPath $t 'config.yaml') 'config.yaml bleibt ohne -Purge'
}

Complete-Tests
