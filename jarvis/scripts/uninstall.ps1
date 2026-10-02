<#
  JARVIS deinstallieren - pro Benutzer, ohne Adminrechte.

  Start per Doppelklick auf Uninstall.cmd (im Installationsordner), ueber
  Startmenue > JARVIS > JARVIS deinstallieren oder:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall.ps1 [Optionen]

  Optionen:
    -Target <Ordner>  Installationsordner (Standard: der Ordner ueber scripts\)
    -Purge            auch config.yaml, secrets.yaml (Token) und state\ (Logs, Status) loeschen
    -Yes              keine Rueckfragen (ohne -Purge bleiben die Einstellungen erhalten)
    -AllowAdmin       trotz Adminrechten fortfahren (nicht empfohlen)
    -AllowNonWindows  nur fuer Tests: unter pwsh auf Linux/macOS ausfuehren

  Geloescht werden nur die Dateien, die laut Installationsmarker (.jarvis-install.json)
  installiert wurden, dazu .venv und die eigenen Verknuepfungen. Ohne Marker wird nichts
  geloescht. Ohne -Purge bleibt neben den Einstellungen ein Rest-Marker (ohne Dateiliste) liegen:
  So darf ein spaeteres "uninstall.ps1 -Target <Ordner> -Purge" (z. B. aus dem entpackten Paket)
  die Einstellungen noch loeschen, und eine Neuinstallation uebernimmt sie.
  Exitcodes: 0 = ok, 1 = abgebrochen, 2 = Fehler / nicht alles entfernt.
#>
param(
    [string]$Target,
    [switch]$Purge,
    [switch]$Yes,
    [switch]$AllowAdmin,
    [switch]$AllowNonWindows,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$UnknownArgs
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'installer-lib.ps1')

$TotalSteps = 5
$exitCode = 0
try {
    Write-Host ''
    Write-Host '=== JARVIS deinstallieren ===' -ForegroundColor Cyan
    if (@($UnknownArgs | Where-Object { $_ }).Count -gt 0) {
        throw ('Unbekannte Option(en): {0}. Hilfe: Kopf von scripts\uninstall.ps1.' -f ($UnknownArgs -join ' '))
    }

    # 1 ----------------------------------------------------------------------------------
    Write-JarvisStep 1 $TotalSteps 'Pruefen'
    Assert-JarvisPlatform -AllowNonWindows:$AllowNonWindows
    Assert-JarvisNotElevated -AllowAdmin:$AllowAdmin
    if ($Target) { $Target = $Target.Trim().Trim('"') }
    if (-not $Target) { $Target = Split-Path -Parent $PSScriptRoot }
    $Target = Assert-JarvisSafeTarget $Target
    if (-not [System.IO.Directory]::Exists($Target)) { throw ('Ordner nicht gefunden: {0}' -f $Target) }
    $manifest = Read-JarvisManifest $Target
    if (-not $manifest) {
        throw ("In '{0}' gibt es keinen Installationsmarker ({1}). Aus Sicherheitsgruenden wird nichts geloescht." -f $Target, (Get-JarvisMarkerName))
    }
    $isRemnant = Test-JarvisRemnantManifest $manifest
    $doPurge = [bool]$Purge
    if ($isRemnant) {
        # Rest einer frueheren Deinstallation ohne -Purge: nur noch Einstellungen und Daten.
        Write-JarvisInfo ('JARVIS wurde hier bereits deinstalliert ({0}); es liegen nur noch Einstellungen und Daten in {1}.' -f (Format-JarvisDate $manifest.uninstalled_at), $Target)
        if (-not $doPurge -and -not $Yes) {
            $doPurge = Read-JarvisYesNo 'Einstellungen und Daten jetzt loeschen (config.yaml, secrets.yaml mit Token, state\)?' -Default $false
            if (-not $doPurge) { Stop-JarvisAbort 'Abgebrochen - nichts wurde geaendert.' }
        }
    } else {
        Write-JarvisInfo ('Installation: {0} (Version {1}, installiert {2})' -f $Target, $manifest.version, (Format-JarvisDate $manifest.installed_at))
        if (-not $Yes) {
            if (-not (Read-JarvisYesNo 'JARVIS jetzt entfernen?' -Default $false)) { Stop-JarvisAbort 'Deinstallation abgebrochen - nichts wurde geaendert.' }
        }
        if (-not $doPurge -and -not $Yes) {
            $doPurge = Read-JarvisYesNo 'Auch Einstellungen und Daten loeschen (config.yaml, secrets.yaml mit Token, state\)?' -Default $false
        }
    }

    # Arbeitsordner verlassen, sonst ist der Installationsordner unter Windows gesperrt.
    $safeDir = Get-JarvisSafeWorkingDir -Target $Target
    if ($safeDir) {
        Set-Location -LiteralPath $safeDir
        [System.Environment]::CurrentDirectory = $safeDir
    }

    # 2 ----------------------------------------------------------------------------------
    Write-JarvisStep 2 $TotalSteps 'JARVIS-Server beenden'
    $stopped = Stop-JarvisServer -Target $Target
    if ($stopped -eq 0) { Write-JarvisInfo 'JARVIS laeuft nicht.' }
    if (@(Find-JarvisServerProcesses -Target $Target).Count -gt 0) {
        throw 'Der JARVIS-Server laeuft noch und liess sich nicht beenden. Bitte abmelden/anmelden und erneut versuchen.'
    }

    # 3 ----------------------------------------------------------------------------------
    Write-JarvisStep 3 $TotalSteps 'Autostart und Startmenue-Eintraege entfernen'
    $links = New-Object System.Collections.Generic.List[string]
    foreach ($s in @(Get-JarvisManifestList $manifest 'shortcuts')) { $links.Add($s) }
    if ($manifest.autostart) {
        $links.Add([string]$manifest.autostart)
    } elseif (Test-JarvisWindows) {
        # Autostart, der spaeter per install_autostart.ps1 angelegt wurde und auf diese venv zeigt.
        $candidate = Get-JarvisAutostartPath
        $linkTarget = Get-JarvisShortcutTargetPath $candidate
        if ($linkTarget -and (Test-JarvisPathUnder -Path $linkTarget -Root (Get-JarvisVenvDir $Target))) { $links.Add($candidate) }
    }
    $linkResult = Remove-JarvisShortcutFiles -Paths $links.ToArray() -Owner $Target
    foreach ($x in $linkResult.Removed) { Write-JarvisInfo ('Entfernt: ' + $x) }
    foreach ($x in $linkResult.Skipped) { Write-JarvisWarn ('Nicht angefasst (liegt nicht im Startmenue/Autostart): ' + $x) }
    foreach ($x in $linkResult.Foreign) { Write-JarvisInfo ('Bleibt (gehoert zu einer anderen JARVIS-Installation): ' + $x) }
    if ($linkResult.Removed.Count -eq 0) { Write-JarvisInfo 'Keine Verknuepfungen vorhanden.' }

    # 4 ----------------------------------------------------------------------------------
    Write-JarvisStep 4 $TotalSteps 'Dateien entfernen'
    $result = New-JarvisRemovalResult
    Add-JarvisRemovalResult -Into $result -From $linkResult
    $venv = Get-JarvisVenvDir $Target
    if ([System.IO.Directory]::Exists($venv) -or (Test-JarvisReparsePoint $venv)) {
        try {
            Remove-JarvisTree -Path $venv -Root $Target
            $result.Removed.Add($venv)
            Write-JarvisInfo 'Entfernt: .venv'
        } catch {
            $result.Failed.Add(('.venv ({0})' -f (Get-JarvisErrorMessage $_)))
        }
    }
    $fileResult = Remove-JarvisRelativeFiles -Target $Target -Files (Get-JarvisManifestList $manifest 'files')
    Add-JarvisRemovalResult -Into $result -From $fileResult
    Write-JarvisInfo ('Programmdateien entfernt: {0}' -f $fileResult.Removed.Count)
    foreach ($x in $fileResult.Skipped) { Write-JarvisWarn ('Nicht angefasst (ungueltiger Eintrag im Marker): ' + $x) }
    if ($doPurge) {
        # config.yaml, config.yaml.bak, secrets.yaml und Zwischendateien des Assistenten - nur direkt im Ziel.
        $names = @([System.IO.Directory]::GetFiles($Target) | ForEach-Object { [System.IO.Path]::GetFileName($_) } |
            Where-Object { Test-JarvisUserDataFileName $_ } | Sort-Object)
        foreach ($name in $names) {
            $path = Join-Path $Target $name
            try { Remove-JarvisFileEntry $path; $result.Removed.Add($path); Write-JarvisInfo ('Entfernt: ' + $name) }
            catch { $result.Failed.Add(('{0} ({1})' -f $name, (Get-JarvisErrorMessage $_))) }
        }
        $stateDir = Join-Path $Target 'state'
        if ([System.IO.Directory]::Exists($stateDir) -or (Test-JarvisReparsePoint $stateDir)) {
            try { Remove-JarvisTree -Path $stateDir -Root $Target; $result.Removed.Add($stateDir); Write-JarvisInfo 'Entfernt: state\' }
            catch { $result.Failed.Add(('state ({0})' -f (Get-JarvisErrorMessage $_))) }
        }
    }
    if ($result.Failed.Count -eq 0) {
        # Bleiben Einstellungen liegen, ersetzt ein Rest-Marker (ohne Dateiliste) den Marker - sonst
        # duerfte ein spaeteres -Purge sie nicht mehr loeschen. Sonst wird der Marker entfernt.
        $marker = Get-JarvisManifestPath $Target
        $keptUserData = @()
        if (-not $doPurge) { $keptUserData = @(Get-JarvisUserDataEntries $Target) }
        try {
            if ($keptUserData.Count -gt 0) {
                Write-JarvisManifest -Target $Target -Manifest (New-JarvisRemnantManifest $manifest)
            } else {
                Remove-JarvisFileEntry $marker
            }
        } catch {
            $result.Failed.Add(('{0} ({1})' -f (Get-JarvisMarkerName), (Get-JarvisErrorMessage $_)))
        }
    }

    # 5 ----------------------------------------------------------------------------------
    Write-JarvisStep 5 $TotalSteps 'Ergebnis'
    $left = @()
    if ([System.IO.Directory]::Exists($Target)) {
        $entries = @([System.IO.Directory]::GetFileSystemEntries($Target) | ForEach-Object { [System.IO.Path]::GetFileName($_) } | Sort-Object)
        # Der (Rest-)Marker gehoert dem Installer und wird nicht als "behalten" aufgelistet.
        $left = @($entries | Where-Object { $_ -ne (Get-JarvisMarkerName) })
        if ($entries.Count -eq 0) {
            try {
                [System.IO.Directory]::Delete($Target, $false)
                Write-JarvisOk ('Ordner entfernt: ' + $Target)
            } catch {
                Write-JarvisWarn ('Der leere Ordner {0} ist noch in Benutzung (z. B. ein offenes Konsolenfenster) und kann spaeter von Hand geloescht werden.' -f $Target)
            }
        }
    }
    if ($left.Count -gt 0) {
        Write-JarvisInfo ('Behalten in {0}:' -f $Target)
        foreach ($name in $left) { Write-JarvisInfo ('  ' + $name) }
        if (-not $doPurge -and (@($left | Where-Object { ((Get-JarvisProtectedNames) -contains $_) -or (Test-JarvisUserDataFileName $_) }).Count -gt 0)) {
            Write-JarvisInfo 'Einstellungen und Token bleiben erhalten (fuer eine spaetere Neuinstallation).'
            Write-JarvisInfo 'Spaeter komplett loeschen: den Ordner von Hand loeschen oder im entpackten Installationspaket'
            Write-JarvisInfo ('  Uninstall.cmd -Target "{0}" -Purge' -f $Target)
        }
    }
    if ($result.Failed.Count -gt 0) {
        Write-JarvisWarn 'Nicht alles konnte entfernt werden:'
        foreach ($x in $result.Failed) { Write-JarvisWarn ('  ' + $x) }
        Write-JarvisInfo 'Der Installationsmarker bleibt erhalten - nach Abmelden/Anmelden Uninstall.cmd erneut starten.'
        $exitCode = 2
    } else {
        Write-JarvisOk 'JARVIS wurde deinstalliert. uv, Python und Ollama bleiben unveraendert installiert.'
    }
} catch [System.OperationCanceledException] {
    Write-Host ''
    Write-JarvisWarn $_.Exception.Message
    $exitCode = 1
} catch {
    Write-Host ''
    Write-JarvisErr (Get-JarvisErrorMessage $_)
    if ($_.InvocationInfo -and $_.InvocationInfo.ScriptName) {
        Write-Host ('       (Stelle: {0}, Zeile {1})' -f (Split-Path -Leaf $_.InvocationInfo.ScriptName), $_.InvocationInfo.ScriptLineNumber) -ForegroundColor DarkGray
    }
    $exitCode = 2
}
exit $exitCode
