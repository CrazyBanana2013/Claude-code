<#
  JARVIS installieren - pro Benutzer, ohne Adminrechte.

  Start per Doppelklick auf Install.cmd oder:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 [Optionen]

  Optionen:
    -Target <Ordner>  Installationsordner (Standard: %LOCALAPPDATA%\JARVIS; aus einer
                      bestehenden Installation gestartet: diese Installation)
    -Yes              keine Rueckfragen, Standardantworten verwenden (laedt nie uv - dafuer -InstallUv)
    -NoAutostart      keinen Autostart-Eintrag anlegen
    -NoStart          Server am Ende nicht starten
    -NoShortcuts      keine Startmenue-Eintraege anlegen
    -InstallUv        Zustimmung: uv bei Bedarf von astral.sh laden und installieren
    -NoUv             uv nicht verwenden (python -m venv + pip mit requirements.txt)
    -AllowAdmin       trotz Adminrechten fortfahren (nicht empfohlen)
    -AllowNonWindows  nur fuer Tests: unter pwsh auf Linux/macOS ausfuehren

  Ein Update ist dasselbe wie eine Installation: config.yaml, secrets.yaml und state\ im
  Zielordner werden nie ueberschrieben oder geloescht. In einen fremden, nicht leeren Ordner
  wird nicht installiert.
  Exitcodes: 0 = ok, 1 = vom Benutzer abgebrochen (auch Strg+C), 2 = Fehler.
#>
param(
    [string]$Target,
    [switch]$Yes,
    [switch]$NoAutostart,
    [switch]$NoStart,
    [switch]$NoShortcuts,
    [switch]$InstallUv,
    [switch]$NoUv,
    [switch]$AllowAdmin,
    [switch]$AllowNonWindows,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$UnknownArgs
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'installer-lib.ps1')

$TotalSteps = 10
$script:source = $null
$script:version = '0.0.0'
$script:files = @()
$script:shortcuts = @()
$script:autostart = $null
$script:pythonEnv = $null
$script:preexisting = @()
$script:completed = $false

function Save-InstallManifest {
    $m = New-JarvisManifest -Version $script:version -Source $script:source -Files $script:files `
        -Shortcuts $script:shortcuts -Autostart $script:autostart -PythonEnv $script:pythonEnv -Preexisting $script:preexisting
    Write-JarvisManifest -Target $script:Target -Manifest $m
}

function Test-InstallerPreparedError {
    # Vorbereitete Meldung (throw 'Text') statt eines unerwarteten Fehlers? Dann ohne "Stelle"-Zeile.
    param($ErrorRecord)
    $ex = $ErrorRecord.Exception
    return ($ex.GetType() -eq [System.Management.Automation.RuntimeException] -and
        [string]$ErrorRecord.FullyQualifiedErrorId -eq [string]$ex.Message)
}

function Write-InstallSummary {
    param([string]$LocalUrl, [int]$Port, [string]$Bind, [string[]]$Warnings, [bool]$Running, [bool]$StartTried,
        [bool]$OtherServer, [bool]$TokenCreated)
    $onWindows = Test-JarvisWindows
    Write-Host ''
    if ($StartTried -and -not $Running -and -not $OtherServer) {
        Write-Host '======= JARVIS ist installiert, aber nicht gestartet =======' -ForegroundColor Yellow
    } else {
        Write-Host '================ JARVIS ist installiert ================' -ForegroundColor Green
    }
    Write-Host ('Ordner:            {0}' -f $script:Target)
    if ($OtherServer) {
        Write-Host ('Achtung:           Unter {0} antwortet ein ANDERER Server (eigener Token, eigene config.yaml).' -f $LocalUrl) -ForegroundColor Yellow
        Write-Host '                   Erst diesen beenden, dann Startmenue > JARVIS > JARVIS starten.' -ForegroundColor Yellow
    } else {
        Write-Host ('Oberflaeche am PC: {0}' -f $LocalUrl)
    }
    if ($Bind -eq '0.0.0.0') {
        $lan = @(Get-JarvisLanAddresses)
        foreach ($a in $lan) {
            Write-Host ('Im Heimnetz:       http://{0}:{1}/   (Adapter "{2}", braucht die Firewall-Regel unten)' -f $a.Address, $Port, $a.Interface)
        }
        if ($onWindows -and $lan.Count -eq 0) {
            Write-Host 'Im Heimnetz:       keine private IPv4-Adresse gefunden (WLAN/LAN verbunden?).'
        }
        $tailscale = Get-JarvisTailscaleIp
        if ($tailscale) {
            Write-Host ('Ueber Tailscale:   http://{0}:{1}/   (Adresse fuer das Handy)' -f $tailscale, $Port)
        } else {
            Write-Host 'Ueber Tailscale:   Tailscale nicht gefunden - siehe README, Abschnitt 6 "Tailscale".'
        }
    } else {
        Write-Host ('Hinweis: server.bind ist {0} - vom Handy nur erreichbar, wenn das passt.' -f $Bind)
    }
    Write-Host ('Handy-Launcher:    {0}  (aufs Handy kopieren, README, Abschnitt 8 "Handy-Launcher")' -f (Join-Path (Join-Path $script:Target 'launcher') 'wake.html'))
    $secretsPath = Join-Path $script:Target 'secrets.yaml'
    if ($TokenCreated) {
        Write-Host ('API-Token:         neu erzeugt und oben in Schritt 6 angezeigt; steht auch in {0} (api_token).' -f $secretsPath)
        Write-Host '                   Beim ersten Oeffnen der Oberflaeche (PC und Handy) eingeben.'
    } else {
        Write-Host ('API-Token:         unveraendert, steht in {0} (Eintrag api_token).' -f $secretsPath)
    }
    Write-Host ('Einstellungen:     {0} - aendern mit Install.cmd (erneut, "Konfiguration anpassen" = j) oder Notepad;' -f (Join-Path $script:Target 'config.yaml'))
    Write-Host '                   nach Aenderungen per Notepad JARVIS neu starten (Startmenue: beenden, dann starten).'
    if ($onWindows) {
        Write-Host ('Modell waehlen:    "{0}" "{1}"' -f (Get-JarvisVenvPython -Target $script:Target), (Join-Path (Join-Path $script:Target 'scripts') 'pick_model.py'))
    }
    if (-not $Running) {
        if (@($script:shortcuts).Count -gt 0) {
            Write-Host 'Starten:           Startmenue > JARVIS > JARVIS starten'
        } else {
            Write-Host ('Starten:           "{0}" -m app   (im Ordner {1})' -f (Get-JarvisVenvPython -Target $script:Target -Windowed), $script:Target)
        }
        Write-Host ('Log:               {0}' -f (Get-JarvisServerLogHint -Target $script:Target))
    }
    if (@($Warnings).Count -gt 0) {
        Write-Host ''
        Write-Host 'Noch offen in config.yaml:' -ForegroundColor Yellow
        foreach ($w in $Warnings) { Write-Host ('  - ' + $w) -ForegroundColor Yellow }
    }

    $commands = @(Get-JarvisFirewallCommands -Port $Port -PythonHome (Get-JarvisVenvHome -Target $script:Target))
    Write-Host ''
    Write-Host 'Firewall (einmalig, NUR in einer Admin-PowerShell - der Installer fuehrt das nicht aus):' -ForegroundColor Cyan
    Write-Host '  Admin-PowerShell: Start > "PowerShell" tippen > Rechtsklick > "Als Administrator ausfuehren"'
    Write-Host '  (als Standardbenutzer mit Name und Kennwort eines Administrators).'
    Write-Host '  Vorher pruefen: Get-NetConnectionProfile (Heimnetz = Private) und Get-NetAdapter'
    Write-Host '  (heisst der Tailscale-Adapter anders, -InterfaceAlias anpassen).'
    foreach ($c in $commands) { Write-Host ('  ' + $c) }
    Write-Host '  Der letzte Befehl loescht Block-Regeln, die Windows fuer Python anlegt, wenn im Dialog'
    Write-Host '  "Zugriff zulassen?" Abbrechen geklickt wurde (oder ohne Adminrechte) - sie gehen den'
    Write-Host '  Freigaben vor. Nur ansehen: denselben Befehl ohne "| Remove-NetFirewallRule" ausfuehren.'
    foreach ($n in @(Get-JarvisPublicNetworks)) {
        Write-Host ('  Hinweis: Netzwerk {0} ist "Oeffentlich" - dort greift die LAN-Regel nicht. Heimnetz in der' -f $n) -ForegroundColor Yellow
        Write-Host '  Admin-PowerShell auf Privat stellen: Set-NetConnectionProfile -InterfaceAlias "<Name>" -NetworkCategory Private' -ForegroundColor Yellow
    }
    if ($onWindows -and -not $Yes) {
        if (Read-JarvisYesNo 'Firewall-Befehle in die Zwischenablage kopieren?' -Default $false) {
            try {
                Set-Clipboard -Value ($commands -join "`r`n")
                Write-JarvisOk 'Kopiert. In einer Admin-PowerShell einfuegen und selbst ausfuehren.'
            } catch {
                Write-JarvisWarn ('Zwischenablage nicht verfuegbar: ' + $_.Exception.Message)
            }
        }
    }
    Write-Host ''
    Write-Host ('WARNUNG: Niemals Port {0} im Router (FRITZ!Box) freigeben! Von unterwegs nur ueber Tailscale.' -f $Port) -ForegroundColor Red
}

$exitCode = 0
try {
    Write-Host ''
    Write-Host '=== JARVIS-Installation (pro Benutzer, ohne Adminrechte) ===' -ForegroundColor Cyan
    if (@($UnknownArgs | Where-Object { $_ }).Count -gt 0) {
        throw ('Unbekannte Option(en): {0}. Hilfe: Kopf von scripts\install.ps1.' -f ($UnknownArgs -join ' '))
    }

    # 1 ----------------------------------------------------------------------------------
    Write-JarvisStep 1 $TotalSteps 'System pruefen'
    Assert-JarvisPlatform -AllowNonWindows:$AllowNonWindows
    Assert-JarvisNotElevated -AllowAdmin:$AllowAdmin
    $onWindows = Test-JarvisWindows
    Write-JarvisOk ('PowerShell {0}, normales Benutzerkonto.' -f $PSVersionTable.PSVersion)

    # 2 ----------------------------------------------------------------------------------
    Write-JarvisStep 2 $TotalSteps 'Quell- und Zielordner'
    $script:source = Get-JarvisFullPath (Split-Path -Parent $PSScriptRoot)
    Assert-JarvisSourceFolder $script:source
    # powershell.exe liest -Target "D:\JARVIS\" als D:\JARVIS" (\" = Anfuehrungszeichen im Wert). Steht
    # -Target am Ende, genuegt Trim; sonst haengen die folgenden Optionen im Wert und sind verschluckt.
    if ($Target) { $Target = $Target.Trim().Trim('"') }
    if ($Target -and $Target.Contains('"')) {
        throw ('Der Pfad bei -Target endet vermutlich mit \" (Backslash vor dem Anfuehrungszeichen) - dadurch wurden ' +
            'die folgenden Optionen verschluckt ({0}). Bitte -Target ohne abschliessenden Backslash angeben, z. B. ' +
            '-Target "D:\JARVIS".') -f $Target
    }
    if (-not $Target -and [System.IO.File]::Exists((Get-JarvisManifestPath $script:source))) {
        # Install.cmd aus einer bestehenden Installation: diese aktualisieren/neu einrichten.
        $Target = $script:source
    }
    if (-not $Target) { $Target = Get-JarvisDefaultTarget }
    $script:Target = Assert-JarvisSafeTarget $Target
    $inPlace = Test-JarvisSamePath $script:source $script:Target
    if (-not $inPlace -and (Test-JarvisPathUnder -Path $script:Target -Root $script:source)) {
        throw 'Der Zielordner darf nicht im Quellordner liegen.'
    }
    Write-JarvisInfo ('Quelle: ' + $script:source)
    Write-JarvisInfo ('Ziel:   ' + $script:Target)
    $outsideProfile = -not (Test-JarvisPathUnder -Path $script:Target -Root (Get-JarvisHomeDir))
    if ($onWindows -and $outsideProfile) {
        Write-JarvisWarn ('Der Zielordner liegt ausserhalb deines Benutzerprofils. Dort koennen andere Konten des PCs ' +
            'oft mitlesen und Dateien aendern - der Installer schraenkt die Rechte auf deinen Benutzer ein. ' +
            'Empfohlen ist der Standardordner %LOCALAPPDATA%\JARVIS.')
    }
    $oldManifest = $null
    $targetExisted = [System.IO.Directory]::Exists($script:Target)
    if ($targetExisted) {
        $markerBroken = $false
        try {
            $oldManifest = Read-JarvisManifest $script:Target
        } catch {
            Write-JarvisWarn $_.Exception.Message
            $markerBroken = $true
            $oldManifest = $null
        }
        if ($inPlace -and ($null -eq $oldManifest -or (Test-JarvisRemnantManifest $oldManifest))) {
            # Ohne gueltigen Marker ist unbekannt, welche Dateien hier zu JARVIS gehoeren - aus dem
            # Ordnerinhalt eine Liste zu raten, wuerde eigene Dateien bei der Deinstallation mitloeschen.
            throw ('Install.cmd wurde aus dem Installationsordner gestartet, aber der Installationsmarker fehlt oder ist ' +
                'beschaedigt. Bitte das Installationspaket (ZIP) neu entpacken und dessen Install.cmd starten - ' +
                'config.yaml, secrets.yaml und state\ bleiben dabei erhalten.')
        }
        if (Test-JarvisRemnantManifest $oldManifest) {
            # Rest-Marker einer Deinstallation ohne -Purge (nur noch config.yaml, secrets.yaml, state\).
            Write-JarvisInfo 'Einstellungen einer frueheren Installation gefunden - werden uebernommen.'
            $script:preexisting = @(Get-JarvisManifestList $oldManifest 'preexisting')
        } elseif ($oldManifest) {
            Write-JarvisInfo ('Vorhandene Installation (Version {0}) wird aktualisiert.' -f $oldManifest.version)
            $script:preexisting = @(Get-JarvisManifestList $oldManifest 'preexisting')
        } elseif ($markerBroken -or (Test-JarvisLooksLikeJarvis $script:Target)) {
            Write-JarvisInfo 'Vorhandene JARVIS-Dateien ohne gueltigen Installationsmarker - werden aktualisiert, der Marker neu angelegt.'
        } elseif (Test-JarvisOnlyUserData $script:Target) {
            $found = @(Get-JarvisUserDataEntries $script:Target)
            Write-JarvisWarn ('Im Zielordner liegen nur Einstellungen ohne Installationsmarker: {0}.' -f ($found -join ', '))
            Write-JarvisInfo 'Sie werden als JARVIS-Einstellungen genutzt, aber nie geloescht (auch nicht mit -Purge).'
            if (-not (Read-JarvisYesNo 'Diese Einstellungen fuer JARVIS uebernehmen?' -Default $false -AssumeDefault:$Yes)) {
                Stop-JarvisAbort 'Installation abgebrochen - bitte einen leeren Ordner mit -Target angeben (oder ohne -Yes starten und zustimmen).'
            }
            $script:preexisting = $found
        } elseif (Test-JarvisDirNonEmpty $script:Target) {
            $foreign = @(Get-JarvisForeignEntries $script:Target)
            $shown = @($foreign | Select-Object -First 8)
            $more = ''
            if ($foreign.Count -gt $shown.Count) { $more = (' und {0} weitere' -f ($foreign.Count - $shown.Count)) }
            throw (('Der Zielordner {0} ist nicht leer und enthaelt keine JARVIS-Installation ({1}{2}). Gleichnamige ' +
                    'Dateien wuerden ueberschrieben - deshalb wird dort nicht installiert. Bitte einen leeren oder ' +
                    'neuen Ordner mit -Target angeben, z. B. -Target "{0}\JARVIS".') -f $script:Target, ($shown -join ', '), $more)
        }
        Write-JarvisInfo 'config.yaml, secrets.yaml und state\ bleiben erhalten.'
        if ((Stop-JarvisServer -Target $script:Target) -gt 0) { Write-JarvisOk 'Laufender JARVIS-Server wurde beendet.' }
        if (@(Find-JarvisServerProcesses -Target $script:Target).Count -gt 0) {
            throw 'Der JARVIS-Server laeuft noch und liess sich nicht beenden. Bitte abmelden/anmelden und erneut versuchen.'
        }
    } else {
        Write-JarvisInfo 'Neuinstallation.'
    }

    # 3 ----------------------------------------------------------------------------------
    Write-JarvisStep 3 $TotalSteps 'Programmdateien kopieren'
    $script:files = @(Get-JarvisProgramFiles -Source $script:source)
    Assert-JarvisProgramFileList -Files $script:files -Source $script:source
    if ($inPlace) {
        Write-JarvisInfo 'Quelle = Ziel: Aktualisierung an Ort und Stelle, es wird nichts kopiert.'
        # Nur die frueher installierten Dateien zaehlen - eigene Dateien im Ordner nie ins Manifest.
        $known = @(Get-JarvisManifestList $oldManifest 'files')
        $script:files = @($script:files | Where-Object { $known -contains $_ })
    } else {
        if (-not [System.IO.Directory]::Exists($script:Target)) {
            [void][System.IO.Directory]::CreateDirectory($script:Target)
            if ($onWindows -and $outsideProfile) {
                try {
                    if (Set-JarvisOwnerOnlyAcl -Path $script:Target) { Write-JarvisOk 'Zugriff auf den Zielordner nur fuer dich (plus SYSTEM/Administratoren).' }
                } catch {
                    Write-JarvisWarn ('Rechte des Zielordners konnten nicht eingeschraenkt werden: ' + (Get-JarvisErrorMessage $_))
                }
            }
        }
        $count = Copy-JarvisProgramFiles -Source $script:source -Target $script:Target -Files $script:files
        Unblock-JarvisFiles -Target $script:Target -Files $script:files
        if ($oldManifest) {
            $stale = Remove-JarvisStaleFiles -Target $script:Target -OldFiles (Get-JarvisManifestList $oldManifest 'files') -NewFiles $script:files
            if ($stale.Removed.Count -gt 0) { Write-JarvisInfo ('{0} veraltete Dateien der alten Version entfernt.' -f $stale.Removed.Count) }
        }
        Write-JarvisOk ('{0} Dateien kopiert (ohne config.yaml, secrets.yaml, state\, tests, .venv).' -f $count)
    }
    $script:version = Get-JarvisVersion $script:Target
    $script:shortcuts = @(Get-JarvisManifestList $oldManifest 'shortcuts' | Where-Object { [System.IO.File]::Exists($_) })
    if ($oldManifest -and $oldManifest.autostart -and [System.IO.File]::Exists([string]$oldManifest.autostart)) {
        $script:autostart = [string]$oldManifest.autostart
    }
    if ($oldManifest -and $oldManifest.python_env) { $script:pythonEnv = [string]$oldManifest.python_env }
    Save-InstallManifest
    Write-JarvisInfo ('Version {0}, Marker: {1}' -f $script:version, (Get-JarvisManifestPath $script:Target))

    # 4 ----------------------------------------------------------------------------------
    Write-JarvisStep 4 $TotalSteps 'Python-Umgebung einrichten (.venv)'
    $script:pythonEnv = Initialize-JarvisPythonEnvironment -Target $script:Target -AssumeYes:$Yes -InstallUv:$InstallUv -NoUv:$NoUv
    $python = Get-JarvisVenvPython -Target $script:Target
    if (-not [System.IO.File]::Exists($python)) { throw ('Python der venv fehlt: {0}' -f $python) }
    Save-InstallManifest

    # 5 ----------------------------------------------------------------------------------
    Write-JarvisStep 5 $TotalSteps 'Konfiguration (config.yaml)'
    $configPath = Join-Path $script:Target 'config.yaml'
    $secretsPath = Join-Path $script:Target 'secrets.yaml'
    $examplePath = Join-Path $script:Target 'config.example.yaml'
    $wizardArgs = @('--config', $configPath, '--example', $examplePath)
    if (-not $inPlace -and -not [System.IO.File]::Exists($configPath) -and -not [System.IO.File]::Exists($secretsPath)) {
        # Umstieg von der manuellen Einrichtung: config.yaml/secrets.yaml liegen im Quellordner (Repo).
        $migrate = @(@('config.yaml', 'secrets.yaml') | Where-Object { [System.IO.File]::Exists((Join-Path $script:source $_)) })
        if ($migrate.Count -gt 0) {
            Write-JarvisInfo ('Im Quellordner liegen eigene Einstellungen ({0}) - z. B. von der manuellen Einrichtung.' -f ($migrate -join ', '))
            if ($Yes) {
                Write-JarvisInfo 'Mit -Yes werden sie nicht uebernommen (dazu Install.cmd ohne -Yes starten).'
            } elseif (Read-JarvisYesNo ('Vorhandene Einstellungen aus {0} uebernehmen?' -f $script:source) -Default $true) {
                foreach ($name in $migrate) {
                    # Nie ueberschreiben (die Zieldateien gibt es hier ohnehin noch nicht).
                    [System.IO.File]::Copy((Join-Path $script:source $name), (Join-Path $script:Target $name), $false)
                    Write-JarvisOk ('Uebernommen: ' + $name)
                }
            }
        }
    }
    if (-not [System.IO.File]::Exists($configPath)) {
        if ($Yes) {
            Write-JarvisInfo 'Lege config.yaml aus der Vorlage an (TODO-Werte spaeter ausfuellen).'
            $wizardArgs += '--non-interactive'
        } else {
            Write-JarvisInfo 'Der Einrichtungsassistent fragt jetzt die wichtigsten Werte ab.'
        }
        $rc = Invoke-JarvisWizard -Python $python -Target $script:Target -Subcommand 'configure' -Arguments $wizardArgs
        if ($rc -eq 1) {
            Stop-JarvisAbort 'Einrichtung abgebrochen - ohne config.yaml startet JARVIS nicht. Install.cmd einfach erneut starten.'
        }
        if ($rc -ne 0) { throw ('Der Einrichtungsassistent meldet einen Fehler (Exitcode {0}, siehe oben).' -f $rc) }
        if (-not [System.IO.File]::Exists($configPath)) { throw 'config.yaml wurde nicht angelegt.' }
        Write-JarvisOk ('config.yaml angelegt: ' + $configPath)
    } else {
        Write-JarvisInfo 'config.yaml ist vorhanden und bleibt erhalten.'
        $adjust = $false
        if (-not $Yes) { $adjust = Read-JarvisYesNo 'Konfiguration jetzt anpassen?' -Default $false }
        if ($adjust) {
            $rc = Invoke-JarvisWizard -Python $python -Target $script:Target -Subcommand 'configure' -Arguments $wizardArgs
            if ($rc -eq 1) {
                Write-JarvisInfo 'Abgebrochen - die bisherige config.yaml bleibt unveraendert.'
            } elseif ($rc -ne 0) {
                throw ('Der Einrichtungsassistent meldet einen Fehler (Exitcode {0}, siehe oben).' -f $rc)
            } else {
                Write-JarvisOk 'config.yaml aktualisiert.'
            }
        } else {
            $rc = Invoke-JarvisWizard -Python $python -Target $script:Target -Subcommand 'configure' -Arguments ($wizardArgs + '--non-interactive')
            if ($rc -ne 0) {
                throw ('config.yaml ist ungueltig (Meldung oben). Bitte korrigieren (notepad "{0}") und Install.cmd erneut starten.' -f $configPath)
            }
            Write-JarvisOk 'config.yaml ist gueltig.'
        }
    }

    # 6 ----------------------------------------------------------------------------------
    Write-JarvisStep 6 $TotalSteps 'API-Token (secrets.yaml)'
    $tokenCreated = -not [System.IO.File]::Exists($secretsPath)
    $rc = Invoke-JarvisWizard -Python $python -Target $script:Target -Subcommand 'token' -Arguments @('--secrets', $secretsPath)
    if ($rc -ne 0) { throw ('Der API-Token konnte nicht angelegt werden (Exitcode {0}).' -f $rc) }
    $tokenCreated = $tokenCreated -and [System.IO.File]::Exists($secretsPath)
    if ($onWindows -and $outsideProfile -and $targetExisted -and [System.IO.File]::Exists($secretsPath)) {
        # Vorhandener Ordner ausserhalb des Profils: wenigstens den Token nur fuer dich lesbar machen.
        try { [void](Set-JarvisOwnerOnlyAcl -Path $secretsPath) } catch {
            Write-JarvisWarn ('Rechte von secrets.yaml konnten nicht eingeschraenkt werden: ' + (Get-JarvisErrorMessage $_))
        }
    }
    $port = 8765
    $bind = '0.0.0.0'
    $localUrl = 'http://127.0.0.1:8765/'
    $warnings = @()
    $info = Get-JarvisServerInfo -Python $python -Target $script:Target
    if ($info) {
        if ($info.port) { $port = [int]$info.port }
        if ($info.bind) { $bind = [string]$info.bind }
        if ($info.local_url) { $localUrl = [string]$info.local_url }
        $warnings = @(@($info.warnings) | Where-Object { $_ })
    } else {
        Write-JarvisWarn 'Adresse/Port konnten nicht aus config.yaml gelesen werden - nehme Port 8765 an.'
        $localUrl = 'http://127.0.0.1:{0}/' -f $port
    }
    if (-not $localUrl.EndsWith('/')) { $localUrl = $localUrl + '/' }

    # 7 ----------------------------------------------------------------------------------
    Write-JarvisStep 7 $TotalSteps 'Startmenue-Eintraege'
    if ($NoShortcuts) {
        Write-JarvisInfo 'Uebersprungen (-NoShortcuts).'
    } elseif (-not $onWindows) {
        Write-JarvisInfo 'Uebersprungen (kein Windows).'
    } else {
        $created = @(New-JarvisStartMenuShortcuts -Target $script:Target -LocalUrl $localUrl)
        $obsolete = @($script:shortcuts | Where-Object { $created -notcontains $_ })
        if ($obsolete.Count -gt 0) { [void](Remove-JarvisShortcutFiles -Paths $obsolete -Owner $script:Target) }
        $script:shortcuts = $created
        Write-JarvisOk ('Startmenue-Ordner: ' + (Get-JarvisStartMenuDir))
        foreach ($s in $created) { Write-JarvisInfo ('- ' + [System.IO.Path]::GetFileNameWithoutExtension($s)) }
    }
    Save-InstallManifest

    # 8 ----------------------------------------------------------------------------------
    Write-JarvisStep 8 $TotalSteps 'Autostart'
    $autostartScript = Join-Path (Join-Path $script:Target 'scripts') 'install_autostart.ps1'
    if ($NoAutostart) {
        Write-JarvisInfo 'Uebersprungen (-NoAutostart).'
    } elseif (-not $onWindows) {
        Write-JarvisInfo 'Uebersprungen (kein Windows).'
    } else {
        $existingLink = Get-JarvisAutostartPath
        if ([System.IO.File]::Exists($existingLink) -and -not (Test-JarvisShortcutOwnedBy -Path $existingLink -Target $script:Target)) {
            $otherTarget = Get-JarvisShortcutTargetPath $existingLink
            Write-JarvisInfo ('Der bisherige Autostart-Eintrag startet JARVIS aus einem anderen Ordner ({0}).' -f $otherTarget)
            Write-JarvisInfo 'Mit "Ja" wird er auf diese Installation umgestellt.'
        }
        if (Read-JarvisYesNo 'JARVIS bei jeder Anmeldung automatisch (ohne Fenster) starten?' -Default $true -AssumeDefault:$Yes) {
            $auto = New-JarvisAutostart -Target $script:Target
            $script:autostart = $auto.Path
            if ($auto.Fallback) { Write-JarvisWarn 'pythonw.exe fehlt - Autostart nutzt python.exe (minimiertes Fenster).' }
            Write-JarvisOk ('Autostart eingerichtet: ' + $auto.Path)
        } else {
            if ($script:autostart -and ((Remove-JarvisAutostart -Path $script:autostart -Owner $script:Target) -eq 'removed')) {
                Write-JarvisInfo 'Bisherigen Autostart-Eintrag entfernt.'
            }
            $script:autostart = $null
            Write-JarvisInfo 'Kein Autostart. Spaeter: Install.cmd erneut starten (fragt wieder), oder:'
            Write-JarvisInfo ('  powershell -ExecutionPolicy Bypass -File "{0}"' -f $autostartScript)
        }
    }
    Save-InstallManifest

    # 9 ----------------------------------------------------------------------------------
    Write-JarvisStep 9 $TotalSteps 'JARVIS starten'
    $healthUrl = $localUrl + 'api/health'
    $running = $false
    $startTried = $false
    $otherServer = $false
    if ($NoStart) {
        Write-JarvisInfo 'Uebersprungen (-NoStart).'
    } elseif (Test-JarvisHealth -Url $healthUrl) {
        $otherServer = $true
        Write-JarvisWarn ('Auf Port {0} antwortet schon ein anderer Server (evtl. eine andere JARVIS-Installation). Es wird kein zweiter gestartet.' -f $port)
        foreach ($o in @(Get-JarvisOtherServers -Target $script:Target)) {
            Write-JarvisInfo ('Laeuft vermutlich aus: {0} (PID {1}) - dort beenden (z. B. scripts\stop.ps1 in diesem Ordner).' -f $o.Folder, $o.Id)
        }
    } else {
        if ($onWindows -and $bind -notmatch '^(127\.|localhost$|::1$)') {
            Write-JarvisInfo 'Gleich kann Windows fragen, ob "Python" Netzwerkzugriff bekommt ("Zugriff zulassen?").'
            Write-JarvisInfo 'Ohne Adminrechte ist "Abbrechen" in Ordnung - dann aber in der Admin-PowerShell auch den'
            Write-JarvisInfo 'Aufraeum-Befehl fuer Block-Regeln ausfuehren (steht unten bei den Firewall-Befehlen).'
        }
        $startTried = $true
        $proc = Start-JarvisServer -Target $script:Target
        Write-JarvisInfo 'Warte auf den Server (bis zu 20 Sekunden) ...'
        $running = Wait-JarvisHealth -Url $healthUrl -TimeoutSec 20 -Process $proc
        if ($running) {
            Write-JarvisOk ('JARVIS laeuft: ' + $localUrl)
        } else {
            $logPath = Get-JarvisServerLogHint -Target $script:Target
            Write-JarvisWarn ('JARVIS antwortet nicht. Log: ' + $logPath)
            $tail = @(Get-JarvisLogTail -Path $logPath -Lines 8)
            if ($tail.Count -gt 0) {
                Write-JarvisInfo 'Letzte Zeilen im Log:'
                foreach ($line in $tail) { Write-JarvisInfo ('  ' + $line) }
            }
            $problem = Get-JarvisStartProblem -LogLines $tail -Port $port
            if ($problem) { Write-JarvisWarn $problem }
        }
    }

    # 10 ---------------------------------------------------------------------------------
    Write-JarvisStep 10 $TotalSteps 'Abschluss'
    Save-InstallManifest
    Write-JarvisOk ('Installationsmarker geschrieben: ' + (Get-JarvisManifestPath $script:Target))
    Write-InstallSummary -LocalUrl $localUrl -Port $port -Bind $bind -Warnings $warnings -Running $running `
        -StartTried $startTried -OtherServer $otherServer -TokenCreated $tokenCreated
    $script:completed = $true
} catch [System.OperationCanceledException] {
    Write-Host ''
    Write-JarvisWarn $_.Exception.Message
    $exitCode = 1
    $script:completed = $true
} catch {
    Write-Host ''
    Write-JarvisErr (Get-JarvisErrorMessage $_)
    if (-not (Test-InstallerPreparedError $_) -and $_.InvocationInfo -and $_.InvocationInfo.ScriptName) {
        Write-Host ('       (Stelle: {0}, Zeile {1})' -f (Split-Path -Leaf $_.InvocationInfo.ScriptName), $_.InvocationInfo.ScriptLineNumber) -ForegroundColor DarkGray
    }
    Write-Host 'Die Installation ist nicht vollstaendig. Nach dem Beheben Install.cmd einfach erneut starten -'
    Write-Host 'Einstellungen (config.yaml, secrets.yaml, state\) bleiben dabei erhalten.'
    $exitCode = 2
    $script:completed = $true
} finally {
    if (-not $script:completed) {
        # Strg+C haelt das ganze Skript an: catch-Bloecke und "exit" laufen dann nicht mehr, nur finally.
        Write-Host ''
        Write-JarvisWarn 'Abgebrochen (Strg+C) - Install.cmd einfach erneut starten; config.yaml, secrets.yaml und state\ bleiben erhalten.'
        [System.Environment]::Exit(1)
    }
}
exit $exitCode
