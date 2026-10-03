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

    Invoke-Test 'Port belegt: "installiert, aber nicht gestartet" mit Log-Zeilen und Hinweis' {
        [void](Stop-JarvisServer -Target $t)
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $port)
        $listener.Start()
        try {
            $r = Invoke-Install $src @('-Target', $t)
            Assert-Equal 0 $r.ExitCode ('installiert ist es trotzdem: ' + $r.Output)
            Assert-True ($r.Output -match 'installiert, aber nicht gestartet') ('gelbes Banner: ' + $r.Output)
            Assert-True ($r.Output -match 'Letzte Zeilen im Log') 'Log-Zeilen gezeigt'
            Assert-True ($r.Output -match ('Port {0} ist belegt' -f $port)) ('Hinweis auf belegten Port: ' + $r.Output)
            Assert-False ($r.Output -match '=+ JARVIS ist installiert =+') 'kein gruenes Banner'
            $r = Invoke-PwshScript -Script (Join-Path $t 'scripts/start-hidden.ps1') -Arguments @('-NoBrowser', '-NoPause', '-TimeoutSec', '8')
            Assert-Equal 2 $r.ExitCode ('start-hidden.ps1 meldet den Fehler: ' + $r.Output)
            Assert-True ($r.Output -match ('Port {0} ist belegt' -f $port)) ('start-hidden.ps1: ' + $r.Output)
        } finally {
            $listener.Stop()
        }
    }

    Invoke-Test 'Startmenue "JARVIS starten" (start-hidden.ps1): startet, wartet, meldet "laeuft bereits"' {
        $r = Invoke-PwshScript -Script (Join-Path $t 'scripts/start-hidden.ps1') -Arguments @('-NoBrowser', '-NoPause')
        Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
        Assert-True ($r.Output -match 'JARVIS laeuft: http://127\.0\.0\.1') ('Rueckmeldung: ' + $r.Output)
        Assert-True (Test-JarvisHealth -Url $health) 'Server antwortet'
        $r = Invoke-PwshScript -Script (Join-Path $t 'scripts/start-hidden.ps1') -Arguments @('-NoBrowser', '-NoPause')
        Assert-Equal 0 $r.ExitCode ('zweiter Aufruf: ' + $r.Output)
        Assert-True ($r.Output -match 'JARVIS laeuft bereits') ('kein zweiter Server: ' + $r.Output)
        Assert-Equal 1 @(Find-JarvisServerProcesses -Target $t).Count 'genau ein Serverprozess'
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
        # An Ort und Stelle mit beschaedigtem Marker: nie die Dateiliste aus dem Ordnerinhalt raten.
        $markerPath = Get-JarvisManifestPath $t
        $goodMarker = [System.IO.File]::ReadAllText($markerPath)
        [System.IO.File]::WriteAllText($markerPath, $goodMarker.Substring(0, 40))
        $r = Invoke-Install $t @('-NoStart')
        Assert-Equal 2 $r.ExitCode ('verweigert: ' + $r.Output)
        Assert-True ($r.Output -match 'Installationsmarker fehlt oder ist beschaedigt') ('Meldung: ' + $r.Output)
        Assert-True ($r.Output -match 'neu entpacken') 'Abhilfe genannt'
        Assert-False ($r.Output -match 'Stelle:') 'vorbereitete Meldung ohne Zeilennummer'
        Assert-Equal 40 ([System.IO.File]::ReadAllText($markerPath)).Length 'Marker nicht mit geratener Liste ueberschrieben'
        Assert-Equal 'MEINE NOTIZEN' (Get-TestFile $t 'notizen.txt') 'eigene Datei unveraendert'
        [System.IO.File]::WriteAllText($markerPath, $goodMarker)
        $r = Invoke-PwshScript -Script (Join-Path $t 'scripts/uninstall.ps1') -Arguments @('-AllowNonWindows', '-Yes', '-Purge')
        Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
        Assert-Equal @('notizen.txt') @([System.IO.Directory]::GetFileSystemEntries($t) | ForEach-Object { [System.IO.Path]::GetFileName($_) }) 'nur die eigene Datei bleibt (auch mit -Purge)'
        Assert-Equal 'MEINE NOTIZEN' (Get-TestFile $t 'notizen.txt') 'eigene Datei unveraendert'
        Remove-Item -LiteralPath (Join-Path $t 'notizen.txt')
    }

    Invoke-Test 'Fremder, nicht leerer Zielordner: nie installieren (auch nicht nach Rueckfrage)' {
        $foreign = New-TestDir 'fremd'
        foreach ($rel in @('privat.txt', 'README.md', 'app/__init__.py', '.venv/keep.txt', 'config.yaml', 'state/important.db')) {
            [void](Set-TestFile $foreign $rel 'PRIVAT')
        }
        $r = Invoke-Install $src @('-Target', $foreign, '-NoStart')
        Assert-Equal 2 $r.ExitCode ('Exitcode mit -Yes: ' + $r.Output)
        Assert-True ($r.Output -match 'nicht leer und enthaelt keine JARVIS-Installation') ('Meldung: ' + $r.Output)
        Assert-True ($r.Output -match 'privat\.txt' -and $r.Output -match 'README\.md') 'fremde Eintraege genannt'
        Assert-True ($r.Output.Contains(('-Target "{0}' -f $foreign))) 'Vorschlag Unterordner'
        # Interaktiv genauso (frueher: "Trotzdem dort installieren?" -> j ueberschrieb README.md, leerte .venv).
        $r = Invoke-PwshScript -Script (Join-JarvisRelPath $src $install) -Arguments @('-AllowNonWindows', '-Target', $foreign, '-NoStart') `
            -Environment $envVars -InputText "j`nj`nj`n"
        Assert-Equal 2 $r.ExitCode ('Exitcode interaktiv: ' + $r.Output)
        foreach ($rel in @('privat.txt', 'README.md', 'app/__init__.py', '.venv/keep.txt', 'config.yaml', 'state/important.db')) {
            Assert-Equal 'PRIVAT' (Get-TestFile $foreign $rel) ('unveraendert: ' + $rel)
        }
        Assert-False (Test-TestPath $foreign '.jarvis-install.json') 'kein Marker'
        Assert-False (Test-TestPath $foreign 'scripts') 'nichts kopiert'
    }

    Invoke-Test 'Fremdes Projekt mit name = "jarvis" gilt nicht als JARVIS-Installation' {
        $hobby = New-TestDir 'hobby'
        [void](Set-TestFile $hobby 'pyproject.toml' "[project]`nname = `"jarvis`"`nversion = `"0.0.1`"`n")
        [void](Set-TestFile $hobby 'README.md' 'MEINE README')
        [void](Set-TestFile $hobby 'src/mybot.py' 'print(1)')
        $r = Invoke-Install $src @('-Target', $hobby, '-NoStart')
        Assert-Equal 2 $r.ExitCode ('Exitcode: ' + $r.Output)
        Assert-Equal 'MEINE README' (Get-TestFile $hobby 'README.md') 'README.md unveraendert'
        Assert-True ((Get-TestFile $hobby 'pyproject.toml') -match '0\.0\.1') 'pyproject.toml unveraendert'
    }

    Invoke-Test 'Nur Einstellungen ohne Marker: Rueckfrage, danach nie geloescht (auch nicht mit -Purge)' {
        $only = Join-Path (New-TestDir 'nurdaten') 'JARVIS'
        $exampleText = Get-TestFile $src 'config.example.yaml'
        [void](Set-TestFile $only 'config.yaml' $exampleText)
        [void](Set-TestFile $only 'state/important.db' 'WICHTIG')
        $r = Invoke-Install $src @('-Target', $only, '-NoStart')
        Assert-Equal 1 $r.ExitCode ('mit -Yes keine stille Uebernahme: ' + $r.Output)
        Assert-False (Test-TestPath $only 'app') 'nichts kopiert'
        # Interaktiv: uebernehmen = j, Konfiguration anpassen = n
        $r = Invoke-PwshScript -Script (Join-JarvisRelPath $src $install) -Arguments @('-AllowNonWindows', '-Target', $only, '-NoStart', '-NoAutostart', '-NoShortcuts') `
            -Environment $envVars -InputText "j`nn`n"
        if ($r.ExitCode -ne 0) { Write-TestOutput 'install.ps1 (nur Einstellungen)' $r.Output }
        Assert-Equal 0 $r.ExitCode 'Exitcode'
        Assert-True ($r.Output -match 'nur Einstellungen ohne Installationsmarker') 'Hinweis'
        Assert-Equal @('config.yaml', 'state') @(Get-JarvisManifestList (Read-JarvisManifest $only) 'preexisting') 'preexisting im Marker'
        Assert-Equal $exampleText (Get-TestFile $only 'config.yaml') 'config.yaml unveraendert'
        $r = Invoke-PwshScript -Script (Join-Path $only 'scripts/uninstall.ps1') -Arguments @('-AllowNonWindows', '-Yes', '-Purge')
        Assert-Equal 0 $r.ExitCode ('Purge: ' + $r.Output)
        Assert-True ($r.Output -match 'lag schon vor der Installation hier') ('Hinweis: ' + $r.Output)
        Assert-Equal $exampleText (Get-TestFile $only 'config.yaml') 'vorhandene config.yaml bleibt'
        Assert-Equal 'WICHTIG' (Get-TestFile $only 'state/important.db') 'vorhandenes state\ bleibt'
        Assert-False (Test-TestPath $only 'secrets.yaml') 'vom Installer angelegte secrets.yaml geloescht'
        Assert-False (Test-TestPath $only 'app') 'Programm entfernt'
    }

    Invoke-Test 'Unsinnige Ziele werden abgelehnt (Exitcode 2)' {
        foreach ($bad in @('/', $HOME, (Join-Path $src 'unterordner'))) {
            $r = Invoke-Install $src @('-Target', $bad, '-NoStart')
            Assert-Equal 2 $r.ExitCode ('Exitcode fuer {0}: {1}' -f $bad, $r.Output)
        }
        $r = Invoke-Install $src @('-Target', (Join-Path (New-TestDir 'x') 'J'), '-Gibtsnicht')
        Assert-Equal 2 $r.ExitCode 'unbekannte Option'
        # So kommt -Target "D:\JARVIS\" -NoAutostart bei powershell.exe an (\" = Anfuehrungszeichen im Wert):
        $swallowed = (Join-Path (New-TestDir 'x') 'J') + '" -NoAutostart'
        $r = Invoke-Install $src @('-Target', $swallowed, '-NoStart')
        Assert-Equal 2 $r.ExitCode ('Anfuehrungszeichen im Pfad: ' + $r.Output)
        Assert-True ($r.Output -match 'endet vermutlich mit') ('Erklaerung statt "Unzulaessige Zeichen": ' + $r.Output)
    }

    Invoke-Test 'Umstieg von der manuellen Einrichtung: Einstellungen aus dem Quellordner uebernehmen' {
        $repo = New-TestDir 'repo'
        [void](Copy-JarvisProgramFiles -Source $script:JarvisRoot -Target $repo -Files @(Get-JarvisProgramFiles -Source $script:JarvisRoot))
        $cfgText = [regex]::Replace((Get-TestFile $repo 'config.example.yaml'), '(?m)^(\s+port:\s*)\d+', '${1}9555')
        [void](Set-TestFile $repo 'config.yaml' $cfgText)
        $tokenText = 'api_token: ' + [guid]::NewGuid().ToString('N') + "`n"
        [void](Set-TestFile $repo 'secrets.yaml' $tokenText)
        $mig = Join-Path (New-TestDir 'mig') 'JARVIS'
        # -Yes uebernimmt nichts still ...
        $r = Invoke-PwshScript -Script (Join-Path $repo 'scripts/install.ps1') -Arguments @('-AllowNonWindows', '-Yes', '-Target', $mig, '-NoStart', '-NoAutostart', '-NoShortcuts') -Environment $envVars
        Assert-Equal 0 $r.ExitCode ('Exitcode -Yes: ' + $r.Output)
        Assert-True ($r.Output -match 'Mit -Yes werden sie nicht uebernommen') ('Hinweis: ' + $r.Output)
        Assert-True ((Get-TestFile $mig 'secrets.yaml') -ne $tokenText) 'eigener neuer Token'
        $r = Invoke-PwshScript -Script (Join-Path $mig 'scripts/uninstall.ps1') -Arguments @('-AllowNonWindows', '-Yes', '-Purge')
        Assert-Equal 0 $r.ExitCode ('Aufraeumen: ' + $r.Output)
        # ... interaktiv: uebernehmen (Vorgabe Ja = Enter), Konfiguration anpassen = n
        $r = Invoke-PwshScript -Script (Join-Path $repo 'scripts/install.ps1') -Arguments @('-AllowNonWindows', '-Target', $mig, '-NoStart', '-NoAutostart', '-NoShortcuts') `
            -Environment $envVars -InputText "`nn`n"
        if ($r.ExitCode -ne 0) { Write-TestOutput 'install.ps1 (Umstieg)' $r.Output }
        Assert-Equal 0 $r.ExitCode 'Exitcode'
        Assert-True ($r.Output -match 'Vorhandene Einstellungen aus .* uebernehmen\? \(J/n\)') 'Rueckfrage mit Vorgabe Ja'
        Assert-Equal $cfgText (Get-TestFile $mig 'config.yaml') 'config.yaml uebernommen'
        Assert-Equal $tokenText (Get-TestFile $mig 'secrets.yaml') 'Token uebernommen (Handy kennt ihn schon)'
        Assert-True ($r.Output -match 'Token vorhanden') 'kein neuer Token'
        Assert-Equal $cfgText (Get-TestFile $repo 'config.yaml') 'Quelle unveraendert'
        Assert-Equal 0 @(Get-JarvisManifestList (Read-JarvisManifest $mig) 'preexisting').Count 'Kopien gehoeren der Installation'
    }
} finally {
    if ([System.IO.Directory]::Exists($t)) { try { [void](Stop-JarvisServer -Target $t) } catch { } }
}

Complete-Tests
