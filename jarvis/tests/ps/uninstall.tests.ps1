# uninstall.ps1 als eigener Prozess: Marker-Pflicht, gefaehrliche Ziele, nur bekannte Dateien,
# Einstellungen bleiben ohne -Purge, Rueckfragen, Start aus dem Installationsordner heraus.
. (Join-Path $PSScriptRoot 'TestHelpers.ps1')
if (Test-JarvisWindows) { Skip-AllTests 'Tests nutzen Linux-Pfade (Testmodus -AllowNonWindows)' }

$uninstall = Join-Path $script:ScriptsDir 'uninstall.ps1'
$programFiles = @('app/__init__.py', 'app/__main__.py', 'app/tools/registry.py', 'web/index.html', 'launcher/wake.html',
    'Install.cmd', 'Uninstall.cmd', 'README.md', 'config.example.yaml', 'pyproject.toml')

function New-Installed {
    param([string[]]$Files = $programFiles, [switch]$WithUserData, [string[]]$ExtraManifestFiles = @())
    $t = New-TestDir 'inst'
    foreach ($f in $Files) { [void](Set-TestFile $t $f ('Inhalt ' + $f)) }
    [void](Set-TestFile $t '.venv/bin/python' '')
    [void](Set-TestFile $t '.venv/lib/python3.11/site-packages/fastapi/__init__.py' '')
    [void](Set-TestFile $t 'app/__pycache__/__main__.cpython-311.pyc' '')
    if ($WithUserData) {
        [void](Set-TestFile $t 'config.yaml' 'MEINE CONFIG')
        [void](Set-TestFile $t 'secrets.yaml' 'api_token: geheim')
        [void](Set-TestFile $t 'config.yaml.bak' 'ALTE CONFIG')
        [void](Set-TestFile $t 'state/logs/server.log' 'log')
    }
    $m = New-JarvisManifest -Version '0.1.0' -Source '/quelle' -Files (@($Files) + $ExtraManifestFiles) -Shortcuts @() -PythonEnv 'uv'
    Write-JarvisManifest -Target $t -Manifest $m
    return $t
}

function Invoke-Uninstall {
    param([string[]]$Arguments = @(), [string]$InputText, [hashtable]$Environment, [string]$Script = $uninstall, [string]$WorkingDirectory)
    return (Invoke-PwshScript -Script $Script -Arguments (@('-AllowNonWindows') + $Arguments) -InputText $InputText `
            -Environment $Environment -WorkingDirectory $WorkingDirectory)
}

Invoke-Test 'Ohne Marker wird nichts geloescht' {
    $t = New-TestDir 'ohne-marker'
    foreach ($f in $programFiles) { [void](Set-TestFile $t $f) }
    $r = Invoke-Uninstall @('-Target', $t, '-Yes', '-Purge')
    Assert-Equal 2 $r.ExitCode ('Exitcode: ' + $r.Output)
    Assert-True ($r.Output -match 'Installationsmarker') ('Meldung: ' + $r.Output)
    foreach ($f in $programFiles) { Assert-True (Test-TestPath $t $f) ('noch da: ' + $f) }
}

Invoke-Test 'Gefaehrliche Ziele werden abgelehnt' {
    $fakeLocal = New-TestDir 'localappdata'
    Write-JarvisManifest -Target $fakeLocal -Manifest (New-JarvisManifest -Version '0.1.0' -Source 's' -Files @('a.txt'))
    [void](Set-TestFile $fakeLocal 'a.txt')
    $envVars = @{ LOCALAPPDATA = $fakeLocal }
    foreach ($bad in @('/', $HOME, $fakeLocal, (Split-Path -Parent $fakeLocal))) {
        $r = Invoke-Uninstall @('-Target', $bad, '-Yes', '-Purge') -Environment $envVars
        Assert-Equal 2 $r.ExitCode ('Exitcode fuer {0}: {1}' -f $bad, $r.Output)
        Assert-True ($r.Output -match 'Stammverzeichnis|geschuetzter Ordner') ('Meldung fuer {0}: {1}' -f $bad, $r.Output)
    }
    Assert-True (Test-TestPath $fakeLocal 'a.txt') 'Datei in LOCALAPPDATA bleibt'
    Assert-True (Test-TestPath $fakeLocal '.jarvis-install.json') 'Marker in LOCALAPPDATA bleibt'
}

Invoke-Test 'Nur bekannte Dateien, Einstellungen bleiben ohne -Purge' {
    $t = New-Installed -WithUserData
    $outsideName = 'draussen-' + [guid]::NewGuid().ToString('N').Substring(0, 6) + '.txt'
    $outside = Set-TestFile (Split-Path -Parent $t) $outsideName 'BLEIBT'
    [void](Set-TestFile $t 'notizen.txt' 'meins')
    [void](Set-TestFile $t 'app/eigenes.py' 'meins')
    # Manipulierter Marker: Pfade nach draussen und geschuetzte Dateien.
    $m = New-JarvisManifest -Version '0.1.0' -Source '/quelle' -Files (@($programFiles) + @(('../' + $outsideName), $outside, 'config.yaml', 'state/logs/server.log')) -Shortcuts @('/etc/passwd', (Join-Path $t 'notizen.txt'))
    Write-JarvisManifest -Target $t -Manifest $m
    $r = Invoke-Uninstall @('-Target', $t, '-Yes')
    Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
    foreach ($f in $programFiles) { Assert-False (Test-TestPath $t $f) ('entfernt: ' + $f) }
    foreach ($d in @('.venv', 'web', 'launcher', 'app/tools', 'app/__pycache__')) { Assert-False (Test-TestPath $t $d) ('entfernt: ' + $d) }
    $rest = Read-JarvisManifest $t
    Assert-True (Test-JarvisRemnantManifest $rest) 'Rest-Marker statt Marker (Einstellungen bleiben)'
    Assert-Equal 0 @(Get-JarvisManifestList $rest 'files').Count 'Rest-Marker listet keine Dateien'
    Assert-Equal 'MEINE CONFIG' (Get-TestFile $t 'config.yaml') 'config.yaml bleibt'
    Assert-Equal 'api_token: geheim' (Get-TestFile $t 'secrets.yaml') 'secrets.yaml bleibt'
    Assert-Equal 'ALTE CONFIG' (Get-TestFile $t 'config.yaml.bak') 'config.yaml.bak bleibt'
    Assert-True (Test-TestPath $t 'state/logs/server.log') 'state bleibt'
    Assert-True (Test-TestPath $t 'notizen.txt') 'fremde Datei bleibt (auch wenn als Verknuepfung eingetragen)'
    Assert-True ($r.Output -match 'Einstellungen und Token bleiben erhalten') ('Hinweis auf -Purge: ' + $r.Output)
    Assert-True (Test-TestPath $t 'app/eigenes.py') 'fremde Datei im Programmordner bleibt'
    Assert-Equal 'BLEIBT' ([System.IO.File]::ReadAllText($outside)) 'Datei ausserhalb bleibt'
    Assert-True ([System.IO.File]::Exists('/etc/passwd')) '/etc/passwd bleibt'
    Assert-True ($r.Output -match 'Behalten') ('Liste der behaltenen Dateien: ' + $r.Output)
}

Invoke-Test 'Rest-Marker: spaeteres -Purge loescht die Einstellungen' {
    $t = New-Installed -WithUserData
    $r = Invoke-Uninstall @('-Target', $t, '-Yes')
    Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
    Assert-True (Test-JarvisRemnantManifest (Read-JarvisManifest $t)) 'Rest-Marker geschrieben'
    Assert-False ($r.Output -match '(?m)^\s+\.jarvis-install\.json\s*$') ('Marker nicht als "behalten" gelistet: ' + $r.Output)
    Assert-True ($r.Output.Contains(('Uninstall.cmd -Target "{0}" -Purge' -f $t))) ('Hinweis auf spaeteres -Purge: ' + $r.Output)
    $markerBefore = Get-TestFile $t '.jarvis-install.json'
    # Erneut ohne -Purge: nichts zu tun, alles bleibt.
    $r = Invoke-Uninstall @('-Target', $t, '-Yes')
    Assert-Equal 0 $r.ExitCode ('Exitcode zweiter Lauf: ' + $r.Output)
    Assert-True ($r.Output -match 'bereits deinstalliert') ('Hinweis: ' + $r.Output)
    Assert-Equal 'MEINE CONFIG' (Get-TestFile $t 'config.yaml') 'config.yaml bleibt'
    Assert-Equal $markerBefore (Get-TestFile $t '.jarvis-install.json') 'Rest-Marker unveraendert (auch uninstalled_at)'
    # Interaktiv: Loeschen abgelehnt -> Abbruch, nichts geaendert.
    $r = Invoke-Uninstall @('-Target', $t) -InputText "n`n"
    Assert-Equal 1 $r.ExitCode ('Exitcode abgelehnt: ' + $r.Output)
    Assert-Equal 'api_token: geheim' (Get-TestFile $t 'secrets.yaml') 'secrets.yaml bleibt'
    # -Purge (z. B. aus dem entpackten Paket): Einstellungen, state und Marker weg, Ordner weg.
    $r = Invoke-Uninstall @('-Target', $t, '-Yes', '-Purge')
    Assert-Equal 0 $r.ExitCode ('Exitcode -Purge: ' + $r.Output)
    Assert-False ([System.IO.Directory]::Exists($t)) ('Ordner entfernt: ' + $r.Output)
}

Invoke-Test 'Ohne Einstellungen: kein Rest-Marker, Ordner entfernt' {
    $t = New-Installed
    $r = Invoke-Uninstall @('-Target', $t, '-Yes')
    Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
    Assert-False ([System.IO.Directory]::Exists($t)) ('Ordner entfernt: ' + $r.Output)
}

Invoke-Test 'Rest-Marker mit fremder Datei: -Purge laesst sie liegen' {
    $t = New-Installed -WithUserData
    $r = Invoke-Uninstall @('-Target', $t, '-Yes')
    Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
    [void](Set-TestFile $t 'notizen.txt' 'MEINS')
    $r = Invoke-Uninstall @('-Target', $t, '-Yes', '-Purge')
    Assert-Equal 0 $r.ExitCode ('Exitcode -Purge: ' + $r.Output)
    Assert-Equal @('notizen.txt') @([System.IO.Directory]::GetFileSystemEntries($t) | ForEach-Object { [System.IO.Path]::GetFileName($_) }) 'nur die fremde Datei bleibt'
}

Invoke-Test '-Purge loescht Einstellungen und den leeren Ordner' {
    $t = New-Installed -WithUserData
    [void](Set-TestFile $t '.config.yaml.k3j4.tmp' 'Rest des Assistenten')
    $r = Invoke-Uninstall @('-Target', $t, '-Yes', '-Purge')
    Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
    Assert-False ([System.IO.Directory]::Exists($t)) ('Ordner entfernt: ' + $r.Output)
}

Invoke-Test '-Purge loescht keine fremden Dateien' {
    $t = New-Installed -WithUserData
    [void](Set-TestFile $t 'eigene/config.yaml' 'FREMD')
    [void](Set-TestFile $t 'myconfig.yaml' 'FREMD')
    $r = Invoke-Uninstall @('-Target', $t, '-Yes', '-Purge')
    Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
    Assert-Equal 'FREMD' (Get-TestFile $t 'eigene/config.yaml') 'config.yaml in Unterordner bleibt'
    Assert-Equal 'FREMD' (Get-TestFile $t 'myconfig.yaml') 'aehnlicher Name bleibt'
    foreach ($f in @('config.yaml', 'config.yaml.bak', 'secrets.yaml', 'state', '.jarvis-install.json')) { Assert-False (Test-TestPath $t $f) ('entfernt: ' + $f) }
}

Invoke-Test 'Rueckfragen: Nein bricht ab, Purge-Standard ist Nein' {
    $t = New-Installed -WithUserData
    $r = Invoke-Uninstall @('-Target', $t) -InputText "n`n"
    Assert-Equal 1 $r.ExitCode ('Exitcode: ' + $r.Output)
    foreach ($f in $programFiles) { Assert-True (Test-TestPath $t $f) ('noch da: ' + $f) }
    Assert-True (Test-TestPath $t '.jarvis-install.json') 'Marker bleibt'
    $r = Invoke-Uninstall @('-Target', $t) -InputText "j`n`n"
    Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
    Assert-False (Test-TestPath $t 'app/__main__.py') 'Programm entfernt'
    Assert-True (Test-TestPath $t 'config.yaml') 'Enter bei Purge-Frage = Nein'
    Assert-True (Test-TestPath $t 'secrets.yaml') 'Token bleibt'
}

Invoke-Test 'Start aus dem Installationsordner heraus (wie Uninstall.cmd)' {
    $files = @($programFiles) + @('scripts/uninstall.ps1', 'scripts/installer-lib.ps1')
    $t = New-Installed -Files $files -WithUserData
    Copy-Item -LiteralPath $uninstall -Destination (Join-Path $t 'scripts/uninstall.ps1') -Force
    Copy-Item -LiteralPath (Join-Path $script:ScriptsDir 'installer-lib.ps1') -Destination (Join-Path $t 'scripts/installer-lib.ps1') -Force
    $r = Invoke-Uninstall @('-Yes', '-Purge') -Script (Join-Path $t 'scripts/uninstall.ps1') -WorkingDirectory $t
    Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
    Assert-False ([System.IO.Directory]::Exists($t)) ('Ordner entfernt: ' + $r.Output)
}

Invoke-Test 'Unbekannte Option wird abgelehnt' {
    $t = New-Installed
    $r = Invoke-Uninstall @('-Target', $t, '-Yes', '-Gibtsnicht')
    Assert-Equal 2 $r.ExitCode ('Exitcode: ' + $r.Output)
    Assert-True ($r.Output -match 'Unbekannte Option') ('Meldung: ' + $r.Output)
    Assert-True (Test-TestPath $t 'app/__main__.py') 'nichts geloescht'
}

Complete-Tests
