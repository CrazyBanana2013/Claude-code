# Ganzer Ablauf mit echten Skripten unter pwsh (-AllowNonWindows):
# Installation -> Update mit Serverstart -> Deinstallation -> Neuinstallation -> -Purge.
# Braucht uv mit gefuelltem Cache (UV_OFFLINE=1: es wird nichts heruntergeladen).
. (Join-Path $PSScriptRoot 'TestHelpers.ps1')

if (Test-JarvisWindows) { Skip-AllTests 'Ablauftest laeuft im Testmodus (-AllowNonWindows) unter Linux/macOS' }
$uv = Find-JarvisUv
if (-not $uv) { Skip-AllTests 'uv nicht gefunden' }

$install = 'scripts/install.ps1'
$envVars = @{ UV_OFFLINE = '1'; UV_PYTHON_DOWNLOADS = 'never'; UV_NO_PROGRESS = '1' }

function Get-FreePort {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    return $port
}

function Invoke-Install {
    param([string]$Source, [string[]]$Arguments)
    return (Invoke-PwshScript -Script (Join-JarvisRelPath $Source $install) -Arguments (@('-AllowNonWindows', '-Yes') + $Arguments) -Environment $envVars)
}

# Quelle wie im ZIP, plus Koeder, die nie im Ziel landen duerfen.
$src = New-TestDir 'quelle'
[void](Copy-JarvisProgramFiles -Source $script:JarvisRoot -Target $src -Files @(Get-JarvisProgramFiles -Source $script:JarvisRoot))
foreach ($decoy in @('config.yaml', 'secrets.yaml', 'state/scripts.json', 'tests/test_x.py', '.venv/pyvenv.cfg',
        'app/__pycache__/z.cpython-311.pyc', 'dist/alt.zip')) {
    [void](Set-TestFile $src $decoy 'KOEDER')
}
if (-not (Test-TestPath $src 'app/setup_wizard.py')) {
    Skip-AllTests 'app/setup_wizard.py fehlt noch (wird parallel gebaut)'
}
$version = Get-JarvisVersion $src
$t = Join-Path (New-TestDir 'ziel') 'JARVIS'
$port = Get-FreePort
$health = 'http://127.0.0.1:{0}/api/health' -f $port

try {
    Invoke-Test 'Neuinstallation (-Yes -NoStart)' {
        $r = Invoke-Install $src @('-Target', $t, '-NoStart')
        if ($r.ExitCode -ne 0) { Write-TestOutput 'install.ps1' $r.Output }
        Assert-Equal 0 $r.ExitCode 'Exitcode'
        foreach ($f in @('app/__main__.py', 'app/setup_wizard.py', 'web/index.html', 'launcher/wake.html', 'scripts/install.ps1',
                'scripts/uninstall.ps1', 'scripts/installer-lib.ps1', 'scripts/stop.ps1', 'Install.cmd', 'Uninstall.cmd',
                'config.example.yaml', 'pyproject.toml', 'uv.lock', '.venv/bin/python', 'config.yaml', 'secrets.yaml')) {
            Assert-True (Test-TestPath $t $f) ('vorhanden: ' + $f)
        }
        foreach ($f in @('tests', 'dist', 'state/scripts.json', 'app/__pycache__/z.cpython-311.pyc', '.venv/pyvenv.cfg.KOEDER')) {
            Assert-False (Test-TestPath $t $f) ('nicht kopiert: ' + $f)
        }
        Assert-True ((Get-TestFile $t 'config.yaml') -notmatch 'KOEDER') 'config.yaml stammt vom Assistenten, nicht aus der Quelle'
        Assert-True ((Get-TestFile $t 'secrets.yaml') -match 'api_token') 'Token angelegt'
        Assert-True ($r.Output -match '\[10/10\]') 'alle 10 Schritte'
        Assert-True ($r.Output -match 'New-NetFirewallRule') 'Firewall-Befehle angezeigt'
        Assert-True ($r.Output -match 'Niemals Port 8765') 'Warnung Portfreigabe'
        $m = Read-JarvisManifest $t
        Assert-Equal $version $m.version 'version'
        Assert-Equal $src $m.source 'source'
        Assert-Equal 0 @(Get-JarvisManifestList $m 'shortcuts').Count 'keine Verknuepfungen im Testmodus'
        Assert-True ($null -eq $m.autostart) 'kein Autostart im Testmodus'
        Assert-Equal 'uv' $m.python_env 'python_env'
        $files = @(Get-JarvisManifestList $m 'files')
        Assert-True ($files -contains 'app/__main__.py') 'files enthaelt Programmdateien'
        foreach ($f in @('config.yaml', 'secrets.yaml', 'state/scripts.json', 'tests/test_x.py')) { Assert-False ($files -contains $f) ('nicht in files: ' + $f) }
    }

    Invoke-Test 'Update interaktiv: Konfiguration nicht anpassen (n) -> nur pruefen' {
        $cfgBefore = Get-TestFile $t 'config.yaml'
        $r = Invoke-PwshScript -Script (Join-JarvisRelPath $src $install) -Arguments @('-AllowNonWindows', '-Target', $t, '-NoStart') `
            -Environment $envVars -InputText "n`n"
        if ($r.ExitCode -ne 0) { Write-TestOutput 'install.ps1 (interaktiv n)' $r.Output }
        Assert-Equal 0 $r.ExitCode 'Exitcode'
        Assert-True ($r.Output -match 'Konfiguration jetzt anpassen\? \(j/N\)') 'Rueckfrage gestellt'
        Assert-True ($r.Output -match 'config.yaml ist gueltig') 'nur geprueft'
        Assert-Equal $cfgBefore (Get-TestFile $t 'config.yaml') 'config.yaml unveraendert'
    }

    Invoke-Test 'Update interaktiv: Assistent abgebrochen (Exit 1) -> alte config.yaml bleibt' {
        $cfgBefore = Get-TestFile $t 'config.yaml'
        # "j" startet den Assistenten, danach Dateiende = Abbruch im Assistenten.
        $r = Invoke-PwshScript -Script (Join-JarvisRelPath $src $install) -Arguments @('-AllowNonWindows', '-Target', $t, '-NoStart') `
            -Environment $envVars -InputText "j`n"
        if ($r.ExitCode -ne 0) { Write-TestOutput 'install.ps1 (interaktiv j)' $r.Output }
        Assert-Equal 0 $r.ExitCode 'Exitcode (Installation geht weiter)'
        Assert-True ($r.Output -match 'bisherige config.yaml bleibt unveraendert') 'Abbruch als "behalten" gewertet'
        Assert-Equal $cfgBefore (Get-TestFile $t 'config.yaml') 'config.yaml unveraendert'
        Assert-True ($r.Output -match '\[10/10\]') 'Installation bis zum Ende'
    }

    Invoke-Test 'Beschaedigter Installationsmarker blockiert kein Update' {
        [System.IO.File]::WriteAllText((Get-JarvisManifestPath $t), '{kaputt')
        $r = Invoke-Install $src @('-Target', $t, '-NoStart')
        if ($r.ExitCode -ne 0) { Write-TestOutput 'install.ps1 (kaputter Marker)' $r.Output }
        Assert-Equal 0 $r.ExitCode 'Exitcode'
        Assert-True ($r.Output -match 'beschaedigt') 'Hinweis auf kaputten Marker'
        Assert-Equal $version (Read-JarvisManifest $t).version 'Marker neu geschrieben'
    }

    # Benutzer passt die Konfiguration an (eigener Port, nur lokal) und hat Daten in state/.
    $cfg = Get-TestFile $t 'config.yaml'
    $cfg = [regex]::Replace($cfg, '(?m)^(\s+bind:\s*).*$', '${1}"127.0.0.1"')
    $cfg = [regex]::Replace($cfg, '(?m)^(\s+port:\s*)\d+', ('${1}' + $port))
    [void](Set-TestFile $t 'config.yaml' $cfg)
    $secrets = Get-TestFile $t 'secrets.yaml'
    [void](Set-TestFile $t 'state/eigene-daten.json' '{"meins": true}')
    # Datei einer "alten Version", die es in der neuen nicht mehr gibt:
    [void](Set-TestFile $t 'app/veraltet.py' 'alt')
    $dict = ConvertTo-JarvisManifestDict (Read-JarvisManifest $t)
    $dict['files'] = [string[]](@($dict['files']) + 'app/veraltet.py')
    Write-JarvisManifest -Target $t -Manifest $dict

    Invoke-Test 'Update mit Serverstart: Einstellungen bleiben, Server antwortet' {
        $r = Invoke-Install $src @('-Target', $t)
        if ($r.ExitCode -ne 0) { Write-TestOutput 'install.ps1 (Update)' $r.Output }
        Assert-Equal 0 $r.ExitCode 'Exitcode'
        Assert-True ($r.Output -match 'wird aktualisiert') 'Update erkannt'
        Assert-True ($r.Output -match 'config.yaml ist gueltig') 'vorhandene config.yaml nur geprueft'
        Assert-True ($r.Output -match 'JARVIS laeuft') ('Server gestartet: ' + $r.Output)
        Assert-Equal $cfg (Get-TestFile $t 'config.yaml') 'config.yaml unveraendert'
        Assert-Equal $secrets (Get-TestFile $t 'secrets.yaml') 'secrets.yaml unveraendert'
        Assert-Equal '{"meins": true}' (Get-TestFile $t 'state/eigene-daten.json') 'state unveraendert'
        Assert-False (Test-TestPath $t 'app/veraltet.py') 'veraltete Datei entfernt'
        Assert-True (Test-JarvisHealth -Url $health) 'GET /api/health'
        Assert-True (Test-TestPath $t 'state/server.pid') 'PID-Datei (Vertrag 2)'
        Assert-Equal 1 @(Find-JarvisServerProcesses -Target $t).Count 'genau ein Serverprozess'
    }

    Invoke-Test 'Update bei laufendem Server beendet ihn vorher' {
        $before = @(Find-JarvisServerProcesses -Target $t | ForEach-Object { $_.Id })
        $r = Invoke-Install $src @('-Target', $t, '-NoStart')
        Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
        Assert-True ($r.Output -match 'Laufender JARVIS-Server wurde beendet') 'Meldung'
        foreach ($id in $before) { Assert-False (Test-JarvisProcessAlive $id) ('alter Prozess beendet: ' + $id) }
        $r = Invoke-Install $src @('-Target', $t)
        Assert-Equal 0 $r.ExitCode ('Neustart: ' + $r.Output)
        Assert-True (Test-JarvisHealth -Url $health) 'Server laeuft wieder'
    }

    Invoke-Test 'Deinstallation aus dem Installationsordner: Server gestoppt, Einstellungen bleiben' {
        $r = Invoke-PwshScript -Script (Join-Path $t 'scripts/uninstall.ps1') -Arguments @('-AllowNonWindows', '-Yes') -WorkingDirectory $t
        if ($r.ExitCode -ne 0) { Write-TestOutput 'uninstall.ps1' $r.Output }
        Assert-Equal 0 $r.ExitCode 'Exitcode'
        Assert-False (Test-JarvisHealth -Url $health) 'Server antwortet nicht mehr'
        Assert-Equal 0 @(Find-JarvisServerProcesses -Target $t).Count 'kein Serverprozess'
        foreach ($f in @('app', 'web', 'launcher', 'scripts', '.venv', 'Install.cmd', 'Uninstall.cmd', 'pyproject.toml')) {
            Assert-False (Test-TestPath $t $f) ('entfernt: ' + $f)
        }
        Assert-True (Test-JarvisRemnantManifest (Read-JarvisManifest $t)) 'Rest-Marker neben den Einstellungen'
        Assert-Equal $cfg (Get-TestFile $t 'config.yaml') 'config.yaml bleibt'
        Assert-Equal $secrets (Get-TestFile $t 'secrets.yaml') 'secrets.yaml bleibt'
        Assert-True (Test-TestPath $t 'state/eigene-daten.json') 'state bleibt'
        $r = Invoke-PwshScript -Script (Join-Path $script:ScriptsDir 'uninstall.ps1') -Arguments @('-AllowNonWindows', '-Yes', '-Target', $t)
        Assert-Equal 0 $r.ExitCode ('zweite Deinstallation ohne -Purge: nichts zu tun: ' + $r.Output)
        Assert-Equal $cfg (Get-TestFile $t 'config.yaml') 'config.yaml bleibt auch beim zweiten Mal'
    }

    Invoke-Test 'Neuinstallation uebernimmt die alte config.yaml, -Purge raeumt alles' {
        $r = Invoke-Install $src @('-Target', $t, '-NoStart')
        Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
        Assert-True ($r.Output -match 'Einstellungen einer frueheren Installation') ('Rest-Marker erkannt: ' + $r.Output)
        Assert-False (Test-JarvisRemnantManifest (Read-JarvisManifest $t)) 'wieder ein vollstaendiger Marker'
        Assert-Equal $cfg (Get-TestFile $t 'config.yaml') 'config.yaml uebernommen'
        Assert-Equal $secrets (Get-TestFile $t 'secrets.yaml') 'Token uebernommen'
        Assert-True ($r.Output -match 'Token vorhanden') 'Token wird nicht neu angezeigt'
        # Install.cmd aus dem Installationsordner erneut gestartet (Quelle = Ziel):
        [void](Set-TestFile $t 'notizen.txt' 'MEINE NOTIZEN')
        $r = Invoke-Install $t @('-NoStart')
        Assert-Equal 0 $r.ExitCode ('Exitcode an Ort und Stelle: ' + $r.Output)
        Assert-True ($r.Output -match 'an Ort und Stelle') 'Aktualisierung an Ort und Stelle'
        $files = @(Get-JarvisManifestList (Read-JarvisManifest $t) 'files')
        Assert-True ($files -contains 'app/__main__.py') 'Programmdateien bleiben im Manifest'
        Assert-False ($files -contains 'notizen.txt') 'eigene Datei kommt nicht ins Manifest'
        $r = Invoke-PwshScript -Script (Join-Path $t 'scripts/uninstall.ps1') -Arguments @('-AllowNonWindows', '-Yes', '-Purge')
        Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
        Assert-Equal @('notizen.txt') @([System.IO.Directory]::GetFileSystemEntries($t) | ForEach-Object { [System.IO.Path]::GetFileName($_) }) 'nur die eigene Datei bleibt (auch mit -Purge)'
        Assert-Equal 'MEINE NOTIZEN' (Get-TestFile $t 'notizen.txt') 'eigene Datei unveraendert'
        Remove-Item -LiteralPath (Join-Path $t 'notizen.txt')
    }

    Invoke-Test 'Fremder, nicht leerer Zielordner: mit -Yes Abbruch (Exitcode 1)' {
        $foreign = New-TestDir 'fremd'
        [void](Set-TestFile $foreign 'privat.txt' 'PRIVAT')
        $r = Invoke-Install $src @('-Target', $foreign, '-NoStart')
        Assert-Equal 1 $r.ExitCode ('Exitcode: ' + $r.Output)
        Assert-Equal 'PRIVAT' (Get-TestFile $foreign 'privat.txt') 'fremde Datei unveraendert'
        Assert-False (Test-TestPath $foreign 'app') 'nichts kopiert'
        Assert-False (Test-TestPath $foreign '.jarvis-install.json') 'kein Marker'
    }

    Invoke-Test 'Unsinnige Ziele werden abgelehnt (Exitcode 2)' {
        foreach ($bad in @('/', $HOME, (Join-Path $src 'unterordner'))) {
            $r = Invoke-Install $src @('-Target', $bad, '-NoStart')
            Assert-Equal 2 $r.ExitCode ('Exitcode fuer {0}: {1}' -f $bad, $r.Output)
        }
        $r = Invoke-Install $src @('-Target', (Join-Path (New-TestDir 'x') 'J'), '-Gibtsnicht')
        Assert-Equal 2 $r.ExitCode 'unbekannte Option'
    }
} finally {
    if ([System.IO.Directory]::Exists($t)) { try { [void](Stop-JarvisServer -Target $t) } catch { } }
}

Complete-Tests
