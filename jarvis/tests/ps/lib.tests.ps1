# Unit-Tests fuer scripts/installer-lib.ps1 (laufen unter pwsh auf Linux und Windows).
. (Join-Path $PSScriptRoot 'TestHelpers.ps1')
if (Test-JarvisWindows) { Skip-AllTests 'Unit-Tests nutzen Linux-Pfade/Symlinks (Testmodus)' }

$python3 = (Get-Command -Name 'python3' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1)
if (-not $python3) { $python3 = (Get-Command -Name 'python' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1) }

# --- Dateiauswahl und Kopieren --------------------------------------------------------------

Invoke-Test 'Programmdateien: Ausschluesse und Sortierung' {
    $src = New-TestDir 'src'
    foreach ($rel in @('app/__init__.py', 'app/__main__.py', 'app/tools/registry.py', 'web/index.html',
            'launcher/wake.html', 'scripts/installer-lib.ps1', 'README.md', 'config.example.yaml',
            'pyproject.toml', 'uv.lock', 'requirements.txt', 'Install.cmd', 'Uninstall.cmd',
            # ausgeschlossen:
            'app/__pycache__/x.cpython-311.pyc', 'app/mod.pyc', 'scripts/__pycache__/y.pyc', 'tests/test_x.py',
            '.venv/bin/python', 'state/scripts.json', 'dist/JARVIS-Setup-0.1.0.zip', 'config.yaml',
            'secrets.yaml', '.pytest_cache/v/x', '.git/HEAD', '.jarvis-install.json', '.gitignore',
            'web/node_modules/a.js', 'config.yaml.bak', '.config.yaml.abc123.tmp', 'secrets.yaml.bak')) {
        [void](Set-TestFile $src $rel)
    }
    $outside = New-TestDir 'outside'
    [void](Set-TestFile $outside 'secret.txt')
    [void](New-Item -ItemType SymbolicLink -Path (Join-Path $src 'linked') -Target $outside)
    $expected = @('Install.cmd', 'README.md', 'Uninstall.cmd', 'app/__init__.py', 'app/__main__.py',
        'app/tools/registry.py', 'config.example.yaml', 'launcher/wake.html', 'pyproject.toml',
        'requirements.txt', 'scripts/installer-lib.ps1', 'uv.lock', 'web/index.html')
    Assert-Equal $expected @(Get-JarvisProgramFiles -Source $src) 'Dateiliste'
}

Invoke-Test 'Kopieren ueberschreibt nie config.yaml, secrets.yaml, state/' {
    $src = New-TestDir 'src'
    $dst = New-TestDir 'dst'
    foreach ($rel in @('app/__main__.py', 'config.yaml', 'secrets.yaml', 'state/scripts.json', '.jarvis-install.json')) {
        [void](Set-TestFile $src $rel 'NEU')
    }
    [void](Set-TestFile $dst 'config.yaml' 'MEINE CONFIG')
    [void](Set-TestFile $dst 'secrets.yaml' 'MEIN TOKEN')
    [void](Set-TestFile $dst 'state/scripts.json' 'MEIN STATUS')
    $old = Set-TestFile $dst 'app/__main__.py' 'ALT'
    (New-Object System.IO.FileInfo $old).IsReadOnly = $true
    # Auch wenn jemand die geschuetzten Namen in die Liste schmuggelt:
    [void](Set-TestFile $src 'config.yaml.bak' 'NEU')
    [void](Set-TestFile $dst 'config.yaml.bak' 'MEINE SICHERUNG')
    $n = Copy-JarvisProgramFiles -Source $src -Target $dst -Files @('app/__main__.py', 'config.yaml', 'secrets.yaml', 'state/scripts.json', '.jarvis-install.json', 'config.yaml.bak')
    Assert-Equal 1 $n 'Anzahl kopierter Dateien'
    Assert-Equal 'MEINE SICHERUNG' (Get-TestFile $dst 'config.yaml.bak') 'config.yaml.bak (Sicherung des Assistenten)'
    Assert-Equal 'NEU' (Get-TestFile $dst 'app/__main__.py') 'Programmdatei aktualisiert'
    Assert-Equal 'MEINE CONFIG' (Get-TestFile $dst 'config.yaml') 'config.yaml'
    Assert-Equal 'MEIN TOKEN' (Get-TestFile $dst 'secrets.yaml') 'secrets.yaml'
    Assert-Equal 'MEIN STATUS' (Get-TestFile $dst 'state/scripts.json') 'state'
    Assert-False (Test-TestPath $dst '.jarvis-install.json') 'Marker der Quelle nicht kopieren'
}

Invoke-Test 'Kopieren lehnt Pfade mit .. ab' {
    $src = New-TestDir 'src'
    $dst = New-TestDir 'dst'
    Assert-Throws { Copy-JarvisProgramFiles -Source $src -Target $dst -Files @('../boese.txt') } '*Ungueltiger Dateipfad*'
    Assert-Throws { Copy-JarvisProgramFiles -Source $src -Target $dst -Files @('/etc/passwd') } '*Ungueltiger Dateipfad*'
    foreach ($bad in @('C:\x.txt', 'a/../b', 'a//b', 'a|b', 'a:b', ('a' + [char]0 + 'b'), 'a"b', '', ' ')) {
        Assert-False (Test-JarvisRelPathValid $bad) ('ungueltig: ' + $bad)
    }
    foreach ($ok in @('app/__main__.py', 'scripts\stop.ps1', 'web/index.html')) { Assert-True (Test-JarvisRelPathValid $ok) ('gueltig: ' + $ok) }
}

Invoke-Test 'Benutzerdaten-Dateinamen' {
    foreach ($n in @('config.yaml', 'config.yaml.bak', 'secrets.yaml', '.config.yaml.x1y2.tmp', '.secrets.yaml.tmp')) {
        Assert-True (Test-JarvisUserDataFileName $n) ('Benutzerdaten: ' + $n)
        Assert-True (Test-JarvisProtectedRelPath $n) ('geschuetzt: ' + $n)
    }
    foreach ($n in @('config.example.yaml', 'pyproject.toml', 'app/config.yaml', 'myconfig.yaml')) {
        Assert-False (Test-JarvisUserDataFileName $n) ('keine Benutzerdaten: ' + $n)
    }
    Assert-True (Test-JarvisProtectedRelPath 'state/logs/server.log') 'state/'
    Assert-False (Test-JarvisProtectedRelPath 'app/config.yaml.bak') 'nur direkt im Installationsordner'
}

Invoke-Test 'Veraltete Dateien beim Update entfernen' {
    $dst = New-TestDir 'dst'
    foreach ($rel in @('app/a.py', 'app/alt.py', 'app/altpaket/x.py', 'app/altpaket/__pycache__/x.pyc')) { [void](Set-TestFile $dst $rel) }
    $r = Remove-JarvisStaleFiles -Target $dst -OldFiles @('app/a.py', 'app/alt.py', 'app/altpaket/x.py', 'config.yaml') -NewFiles @('app/a.py')
    Assert-True (Test-TestPath $dst 'app/a.py') 'aktuelle Datei bleibt'
    Assert-False (Test-TestPath $dst 'app/alt.py') 'alte Datei weg'
    Assert-False (Test-TestPath $dst 'app/altpaket') 'leerer Ordner (samt __pycache__) weg'
    Assert-True ($r.Skipped -contains 'config.yaml') 'config.yaml wird nie als veraltet geloescht'
}

# --- Loeschen ---------------------------------------------------------------------------------

Invoke-Test 'Remove-JarvisRelativeFiles loescht nur bekannte Dateien' {
    $root = New-TestDir 'inst'
    $outsideDir = New-TestDir 'outside'
    $outsideFile = Set-TestFile $outsideDir 'bleibt.txt' 'X'
    foreach ($rel in @('app/a.py', 'app/sub/b.py', 'app/__pycache__/a.pyc', 'app/eigene.py', 'web/index.html',
            'notizen.txt', 'config.yaml', 'secrets.yaml', 'state/log.txt')) { [void](Set-TestFile $root $rel) }
    $rel = '../' + (Split-Path -Leaf $outsideDir) + '/bleibt.txt'
    $r = Remove-JarvisRelativeFiles -Target $root -Files @('app/a.py', 'app/sub/b.py', 'web/index.html', 'fehlt.py',
        $rel, $outsideFile, 'config.yaml', 'state/log.txt', 'app/sub')
    Assert-False (Test-TestPath $root 'app/a.py') 'a.py'
    Assert-False (Test-TestPath $root 'app/sub') 'leerer Unterordner'
    Assert-False (Test-TestPath $root 'web') 'leerer Ordner web'
    Assert-False (Test-TestPath $root 'app/__pycache__') '__pycache__ in betroffenem Ordner'
    Assert-True (Test-TestPath $root 'app/eigene.py') 'unbekannte Datei bleibt'
    Assert-True (Test-TestPath $root 'notizen.txt') 'notizen.txt bleibt'
    Assert-True (Test-TestPath $root 'config.yaml') 'config.yaml bleibt'
    Assert-True (Test-TestPath $root 'state/log.txt') 'state bleibt'
    Assert-True ([System.IO.File]::Exists($outsideFile)) 'Datei ausserhalb bleibt'
    foreach ($s in @($rel, $outsideFile, 'config.yaml', 'state/log.txt')) { Assert-True ($r.Skipped -contains $s) ('uebersprungen: ' + $s) }
    Assert-Equal 0 $r.Failed.Count 'keine Fehler'
}

Invoke-Test 'Remove-JarvisTree folgt keinen Links' {
    $root = New-TestDir 'inst'
    $outside = New-TestDir 'outside'
    [void](Set-TestFile $outside 'sub/wichtig.txt' 'WICHTIG')
    [void](Set-TestFile $root '.venv/bin/real.txt')
    [void](New-Item -ItemType SymbolicLink -Path (Join-Path $root '.venv/lib') -Target $outside)
    [void](New-Item -ItemType SymbolicLink -Path (Join-Path $root '.venv/bin/python') -Target (Join-Path $outside 'sub/wichtig.txt'))
    Remove-JarvisTree -Path (Join-Path $root '.venv') -Root $root
    Assert-False (Test-TestPath $root '.venv') '.venv weg'
    Assert-Equal 'WICHTIG' (Get-TestFile $outside 'sub/wichtig.txt') 'Ziel der Links unangetastet'
    # Ein Link als Startpunkt wird nur selbst entfernt.
    [void](New-Item -ItemType SymbolicLink -Path (Join-Path $root 'state') -Target $outside)
    Remove-JarvisTree -Path (Join-Path $root 'state') -Root $root
    Assert-True (Test-TestPath $outside 'sub/wichtig.txt') 'Link-Ziel bleibt'
}

Invoke-Test 'Remove-JarvisTree verweigert Pfade ausserhalb' {
    $root = New-TestDir 'inst'
    $other = New-TestDir 'other'
    Assert-Throws { Remove-JarvisTree -Path $other -Root $root } '*Sicherheitsstopp*'
    Assert-Throws { Remove-JarvisTree -Path $root -Root $root } '*Sicherheitsstopp*'
    Assert-True ([System.IO.Directory]::Exists($other)) 'anderer Ordner bleibt'
}

# --- Pfade und Zielpruefung ---------------------------------------------------------------------

Invoke-Test 'Test-JarvisPathUnder' {
    $base = New-TestDir 'p'
    Assert-True (Test-JarvisPathUnder -Path (Join-Path $base 'a/b') -Root $base) 'Unterordner'
    Assert-False (Test-JarvisPathUnder -Path $base -Root $base) 'gleich ohne AllowEqual'
    Assert-True (Test-JarvisPathUnder -Path $base -Root $base -AllowEqual) 'gleich mit AllowEqual'
    Assert-False (Test-JarvisPathUnder -Path ($base + 'xyz') -Root $base) 'Praefix-Trick'
    Assert-False (Test-JarvisPathUnder -Path (Join-Path $base '../x') -Root $base) '..'
}

Invoke-Test 'Zielordner: gefaehrliche Ziele werden abgelehnt' {
    $fakeLocal = New-TestDir 'localappdata'
    $oldLocal = $env:LOCALAPPDATA
    $env:LOCALAPPDATA = $fakeLocal
    try {
        Assert-Throws { Assert-JarvisSafeTarget '/' } '*Stammverzeichnis*'
        Assert-Throws { Assert-JarvisSafeTarget $HOME } '*geschuetzter Ordner*'
        Assert-Throws { Assert-JarvisSafeTarget $fakeLocal } '*geschuetzter Ordner*'
        Assert-Throws { Assert-JarvisSafeTarget (Split-Path -Parent $fakeLocal) } '*geschuetzter Ordner*'
        Assert-Throws { Assert-JarvisSafeTarget '' } '*Kein Zielordner*'
        $ok = Join-Path $fakeLocal 'JARVIS'
        Assert-Equal $ok (Assert-JarvisSafeTarget $ok) 'Unterordner von LOCALAPPDATA ist erlaubt'
        Assert-Equal $ok (Get-JarvisDefaultTarget) 'Standardziel'
    } finally {
        $env:LOCALAPPDATA = $oldLocal
    }
}

Invoke-Test 'Plattform- und Adminpruefung' {
    Assert-Throws { Assert-JarvisPlatform } '*Windows*'
    Assert-JarvisPlatform -AllowNonWindows
    function Test-JarvisElevated { return $true }
    Assert-Throws { Assert-JarvisNotElevated } '*Administratorrechten*AllowAdmin*'
    Assert-JarvisNotElevated -AllowAdmin
}

Invoke-Test 'Get-JarvisVersion liest [project].version' {
    $dir = New-TestDir 'v'
    [void](Set-TestFile $dir 'pyproject.toml' "[tool.x]`nversion = `"9.9.9`"`n[project]`nname = `"jarvis`"`nversion = `"1.2.3`"`n")
    Assert-Equal '1.2.3' (Get-JarvisVersion $dir) 'Version'
    # Nur der Projektname reicht nicht (den tragen auch fremde Hobbyprojekte) ...
    Assert-False (Test-JarvisLooksLikeJarvis $dir) 'nur pyproject.toml mit name = "jarvis"'
    # ... erst mit den eigenen Installer-Dateien ist es eine JARVIS-Installation ohne Marker.
    foreach ($rel in @('scripts/installer-lib.ps1', 'app/setup_wizard.py', 'config.example.yaml')) { [void](Set-TestFile $dir $rel) }
    Assert-True (Test-JarvisLooksLikeJarvis $dir) 'als JARVIS erkannt'
    $projectVersion = Get-JarvisVersion $script:JarvisRoot
    Assert-True ($projectVersion -match '^\d+\.\d+') ('Version des Projekts: ' + $projectVersion)
    if ($python3) {
        $code = 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])'
        $r = Invoke-JarvisCapture -FilePath $python3.Path -ArgumentList @('-c', $code, (Join-Path $script:JarvisRoot 'pyproject.toml'))
        if ($r.ExitCode -eq 0) { Assert-Equal $r.Output[-1] $projectVersion 'gleich wie tomllib' }
    }
}

# --- Manifest ---------------------------------------------------------------------------------

Invoke-Test 'Manifest: schreiben, lesen, gueltiges ASCII-JSON' {
    $dir = New-TestDir 'm'
    $uml = 'C:\Users\J' + [char]0x00FC + 'rgen\JARVIS'
    $sc = 'C:\Start\JARVIS ' + [char]0x00F6 + 'ffnen.url'
    $m = New-JarvisManifest -Version '0.1.0' -Source $uml -Files @('app/__main__.py', 'web/index.html') -Shortcuts @($sc) -Autostart $null -PythonEnv 'uv'
    Write-JarvisManifest -Target $dir -Manifest $m
    $bytes = [System.IO.File]::ReadAllBytes((Get-JarvisManifestPath $dir))
    Assert-Equal 0 @($bytes | Where-Object { $_ -gt 127 }).Count 'Manifest ist ASCII'
    $back = Read-JarvisManifest $dir
    Assert-Equal '0.1.0' $back.version 'version'
    Assert-Equal $uml $back.source 'source mit Umlaut'
    Assert-Equal @($sc) @(Get-JarvisManifestList $back 'shortcuts') 'shortcuts'
    Assert-True ($null -eq $back.autostart) 'autostart null'
    Assert-Equal @('app/__main__.py', 'web/index.html') @(Get-JarvisManifestList $back 'files') 'files'
    $raw = [System.IO.File]::ReadAllText((Get-JarvisManifestPath $dir))
    Assert-True ($raw -match '"installed_at": "\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"') 'installed_at ISO-8601'
    # Zurueckschreiben (z. B. durch install_autostart.ps1) behaelt das ISO-Format bei:
    $dict = ConvertTo-JarvisManifestDict $back
    $dict['autostart'] = 'C:\Start\JARVIS.lnk'
    Write-JarvisManifest -Target $dir -Manifest $dict
    $raw2 = [System.IO.File]::ReadAllText((Get-JarvisManifestPath $dir))
    Assert-True ($raw2 -match '"installed_at": "\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"') ('installed_at nach Rueckschreiben: ' + $raw2)
    Assert-Equal @($sc) @(Get-JarvisManifestList (Read-JarvisManifest $dir) 'shortcuts') 'shortcuts nach Rueckschreiben'
    Assert-Equal 'C:\Start\JARVIS.lnk' (Read-JarvisManifest $dir).autostart 'autostart nach Rueckschreiben'
    if ($python3) {
        $code = 'import json,sys; m=json.load(open(sys.argv[1], encoding="utf-8")); assert m["shortcuts"] and m["autostart"].endswith("JARVIS.lnk") and m["version"]=="0.1.0" and m["installed_at"].endswith("Z"); print("ok")'
        $r = Invoke-JarvisCapture -FilePath $python3.Path -ArgumentList @('-c', $code, (Get-JarvisManifestPath $dir))
        Assert-Equal 0 $r.ExitCode ('Python liest das Manifest: ' + $r.ErrorText)
    }
}

Invoke-Test 'Manifest: leere Listen bleiben Listen, kaputter Marker wird gemeldet' {
    $dir = New-TestDir 'm'
    Write-JarvisManifest -Target $dir -Manifest (New-JarvisManifest -Version '1' -Source 's' -Files @() -Shortcuts @())
    $text = [System.IO.File]::ReadAllText((Get-JarvisManifestPath $dir))
    Assert-True ($text -match '"shortcuts": \[\]') 'shortcuts []'
    Assert-True ($text -match '"files": \[\]') 'files []'
    Assert-True ($text -match '"autostart": null') 'autostart null'
    Assert-Equal 0 @(Get-JarvisManifestList (Read-JarvisManifest $dir) 'files').Count 'keine Dateien'
    [System.IO.File]::WriteAllText((Get-JarvisManifestPath $dir), '{kaputt')
    Assert-Throws { Read-JarvisManifest $dir } '*beschaedigt*'
    Assert-True ($null -eq (Read-JarvisManifest (New-TestDir 'leer'))) 'kein Marker -> $null'
}

# --- Kommandozeilen ---------------------------------------------------------------------------

Invoke-Test 'ConvertTo-JarvisArgString: Rundweg ueber echte Prozess-Argumente' {
    if (-not $python3) { Write-Host '      (python3 fehlt - uebersprungen)'; return }
    $values = @('einfach', 'mit Leerzeichen', 'An"fuehrung', 'endet\', 'C:\Program Files\x\', '', 'a\\"b', "tab`tx", '-m', 'irm https://astral.sh/uv/install.ps1 | iex')
    $code = 'import json,sys; print(json.dumps(sys.argv[2:]))'
    $r = Invoke-JarvisCapture -FilePath $python3.Path -ArgumentList (@('-c', $code, 'marker') + $values)
    Assert-Equal 0 $r.ExitCode 'Exitcode'
    $got = @($r.Output[-1] | ConvertFrom-Json)
    Assert-Equal $values.Count $got.Count 'Anzahl Argumente'
    for ($i = 0; $i -lt $values.Count; $i++) { Assert-Equal $values[$i] $got[$i] ('Argument ' + $i) }
}

Invoke-Test 'uv: offizieller Windows-Installationsbefehl laut Doku' {
    function Test-JarvisWindows { return $true }
    $cmd = Get-JarvisUvInstallCommand
    Assert-Equal 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"' $cmd.Display 'Anzeige'
    Assert-Equal @('-ExecutionPolicy', 'ByPass', '-c', 'irm https://astral.sh/uv/install.ps1 | iex') @($cmd.ArgumentList) 'Argumente'
    Assert-Equal '-ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"' (ConvertTo-JarvisArgString $cmd.ArgumentList) 'Kommandozeile'
    Assert-True ($cmd.FilePath -like '*powershell*') 'Windows PowerShell'
}

Invoke-Test 'uv: Installation nur mit UV_NO_MODIFY_PATH, Standardorte laut Doku' {
    $script:calls = @()
    function Invoke-JarvisProcess { param($FilePath, $ArgumentList, $WorkingDirectory, $Environment) $script:calls += , @{ File = $FilePath; Args = $ArgumentList; Env = $Environment }; return 0 }
    [void](Install-JarvisUv)
    Assert-Equal 1 $script:calls.Count 'ein Aufruf'
    Assert-Equal '1' $script:calls[0].Env['UV_NO_MODIFY_PATH'] 'PATH bleibt unveraendert'
    $fakeHome = New-TestDir 'home'
    $oldHome = $env:HOME
    $oldProfile = $env:USERPROFILE
    $env:HOME = $fakeHome
    $env:USERPROFILE = $fakeHome
    try {
        function Test-JarvisWindows { return $true }
        $c = @(Get-JarvisUvCandidates)
        Assert-True ($c -contains [System.IO.Path]::Combine($fakeHome, '.local', 'bin', 'uv.exe')) '%USERPROFILE%\.local\bin\uv.exe'
        Assert-True ($c -contains [System.IO.Path]::Combine($fakeHome, '.cargo', 'bin', 'uv.exe')) 'alter Ort ~\.cargo\bin'
    } finally {
        $env:HOME = $oldHome
        $env:USERPROFILE = $oldProfile
    }
}

Invoke-Test 'uv: wird in ~/.local/bin gefunden, auch ohne PATH' {
    $fakeHome = New-TestDir 'home'
    $uv = Set-TestFile $fakeHome '.local/bin/uv' "#!/bin/sh`necho 'uv 9.9.9 (test)'`n"
    & chmod +x $uv
    $oldHome = $env:HOME
    $oldPath = $env:PATH
    $env:HOME = $fakeHome
    $env:PATH = '/usr/bin:/bin'
    try {
        $found = Find-JarvisUv
        Assert-True ($null -ne $found) 'gefunden'
        Assert-Equal $uv $found.Path 'Pfad'
        Assert-Equal 'uv 9.9.9 (test)' $found.Version 'Version'
    } finally {
        $env:HOME = $oldHome
        $env:PATH = $oldPath
    }
}

Invoke-Test 'uv sync: Argumente und Projekt-venv im Ziel' {
    $script:calls = @()
    function Invoke-JarvisProcess { param($FilePath, $ArgumentList, $WorkingDirectory, $Environment) $script:calls += , @{ File = $FilePath; Args = $ArgumentList; Cwd = $WorkingDirectory; Env = $Environment }; return 0 }
    $t = New-TestDir 't'
    [void](Invoke-JarvisUvSync -Uv '/x/uv' -Target $t)
    Assert-Equal @('sync', '--frozen', '--no-dev') @($script:calls[0].Args) 'Argumente'
    Assert-Equal $t $script:calls[0].Cwd 'Arbeitsordner'
    Assert-Equal (Join-Path $t '.venv') $script:calls[0].Env['UV_PROJECT_ENVIRONMENT'] 'venv-Pfad'
}

Invoke-Test 'Python-Umgebung: ohne Zustimmung kein Download, Fallback pip mit Hashes' {
    $t = New-TestDir 't'
    [void](Set-TestFile $t 'requirements.txt' 'fastapi==1 --hash=sha256:00')
    $script:calls = @()
    $script:asked = @()
    function Find-JarvisUv { return $null }
    function Install-JarvisUv { throw 'Download ohne Zustimmung!' }
    function Read-JarvisAnswer { param($Prompt) $script:asked += $Prompt; return 'n' }
    function Find-JarvisPython { return [pscustomobject]@{ Path = '/opt/py311/bin/python3'; Version = '3.11' } }
    function Invoke-JarvisProcess {
        param($FilePath, $ArgumentList, $WorkingDirectory, $Environment)
        $script:calls += , @{ File = $FilePath; Args = @($ArgumentList) }
        if ($ArgumentList -contains 'venv') { [void](Set-TestFile $t '.venv/bin/python' '') }
        return 0
    }
    $kind = Initialize-JarvisPythonEnvironment -Target $t
    Assert-Equal 'pip' $kind 'Ergebnis'
    Assert-Equal 1 $script:asked.Count 'genau eine Rueckfrage'
    Assert-True ($script:asked[0] -like '*uv jetzt herunterladen*(j/N)*') 'Frage mit Standard Nein'
    Assert-Equal '/opt/py311/bin/python3' $script:calls[0].File 'venv mit System-Python'
    Assert-Equal @('-m', 'venv', '--clear', (Join-Path $t '.venv')) $script:calls[0].Args 'venv-Argumente'
    Assert-Equal (Get-JarvisVenvPython -Target $t) $script:calls[1].File 'pip aus der venv'
    Assert-Equal @('-m', 'pip', 'install', '--require-hashes', '--no-input', '--disable-pip-version-check', '-r', (Join-Path $t 'requirements.txt')) $script:calls[1].Args 'pip-Argumente'
    # -Yes (AssumeYes) ist keine Zustimmung zum Download:
    $script:asked = @()
    $script:calls = @()
    $kind = Initialize-JarvisPythonEnvironment -Target $t -AssumeYes
    Assert-Equal 'pip' $kind 'Ergebnis mit -Yes'
    Assert-Equal 0 $script:asked.Count 'keine Rueckfrage mit -Yes'
}

Invoke-Test 'Python-Umgebung: mit Zustimmung uv installieren und uv sync' {
    $t = New-TestDir 't'
    $script:installed = $false
    $script:syncs = 0
    function Find-JarvisUv { if ($script:installed) { return [pscustomobject]@{ Path = '/home/x/.local/bin/uv'; Version = 'uv 1' } }; return $null }
    function Install-JarvisUv { $script:installed = $true; return 0 }
    function Read-JarvisAnswer { param($Prompt) return 'j' }
    function Invoke-JarvisUvSync { param($Uv, $Target) $script:syncs++; [void](Set-TestFile $t '.venv/bin/python' ''); return 0 }
    $script:pyChecks = 0
    # Vor der Frage wird nur nachgesehen, ob es ohne uv ginge (hier: kein Python) - kein pip-Fallback.
    function Find-JarvisPython { $script:pyChecks++; return $null }
    Assert-Equal 'uv' (Initialize-JarvisPythonEnvironment -Target $t) 'Ergebnis'
    Assert-True $script:installed 'uv installiert'
    Assert-Equal 1 $script:syncs 'uv sync'
    Assert-Equal 1 $script:pyChecks 'Python nur einmal vor der Frage gesucht'
}

Invoke-Test 'Python-Umgebung: uv sync scheitert -> pip; ohne Python klare Meldung' {
    $t = New-TestDir 't'
    [void](Set-TestFile $t 'requirements.txt' '')
    function Find-JarvisUv { return [pscustomobject]@{ Path = '/x/uv'; Version = 'uv 1' } }
    function Invoke-JarvisUvSync { param($Uv, $Target) return 2 }
    function Find-JarvisPython { return $null }
    Assert-Throws { Initialize-JarvisPythonEnvironment -Target $t } '*Python 3.11*-InstallUv*'
    Remove-Item -LiteralPath (Join-Path $t 'requirements.txt')
    Assert-Throws { Initialize-JarvisPythonEnvironment -Target $t } '*requirements.txt fehlt*'
}

Invoke-Test 'Python-Umgebung: ohne uv und ohne Python -> sofort klare Anleitung' {
    $t = New-TestDir 't'
    [void](Set-TestFile $t 'requirements.txt' '')
    $script:asked = @()
    function Find-JarvisUv { return $null }
    function Find-JarvisPython { return $null }
    function Install-JarvisUv { throw 'Download ohne Zustimmung!' }
    function Read-JarvisAnswer { param($Prompt) $script:asked += $Prompt; return '' }
    function Invoke-JarvisProcess { throw 'darf ohne Python nichts starten' }
    Assert-Throws { Initialize-JarvisPythonEnvironment -Target $t } '*Python 3.11*uv-Download zustimmen*Use admin privileges when installing py.exe*'
    Assert-Equal 1 $script:asked.Count 'eine Frage (Standard Nein)'
    Assert-Throws { Initialize-JarvisPythonEnvironment -Target $t -AssumeYes } '*Python 3.11*'
    $msg = Get-JarvisNoPythonMessage
    Assert-True ($msg -like '*Microsoft Store funktioniert hierfuer nicht*') 'kein Store-Python empfohlen'
    Assert-True ($msg -like '*Python install manager*') 'Python install manager als Alternative'
}

Invoke-Test 'Python aus dem Microsoft Store wird nicht genommen' {
    $store = 'C:\Users\Max\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe'
    $storeBase = 'C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.12_3.12.2800.0_x64__qbz5n2kfra8p0'
    Assert-True (Test-JarvisStorePythonPath $store) 'WindowsApps-Alias'
    Assert-True (Test-JarvisStorePythonPath $storeBase) 'Paketordner (sys.base_prefix)'
    Assert-True (Test-JarvisStorePythonPath 'C:\Users\Max\AppData\Local\Packages\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\x') 'umgeleiteter LocalCache'
    Assert-False (Test-JarvisStorePythonPath 'C:\Users\Max\AppData\Local\Programs\Python\Python312\python.exe') 'python.org nur fuer mich'
    Assert-False (Test-JarvisStorePythonPath 'C:\Users\Max\AppData\Local\Python\pythoncore-3.14-64\python.exe') 'Python install manager'
    Assert-False (Test-JarvisStorePythonPath '') 'leer'
    # Find-JarvisPython entscheidet nach dem gemeldeten Pfad (der py-Launcher findet Store-Python ueber die Registry).
    function Get-JarvisPythonCandidates {
        return @([pscustomobject]@{ Exe = 'py'; Args = @('-3') }, [pscustomobject]@{ Exe = 'python'; Args = @() })
    }
    $script:pyOutput = @{
        'py'     = @($store, $storeBase, '312')
        'python' = @('C:\Users\Max\AppData\Local\Programs\Python\Python313\python.exe', 'C:\Users\Max\AppData\Local\Programs\Python\Python313', '313')
    }
    function Invoke-JarvisCapture { param($FilePath, $ArgumentList, $TimeoutSec) return [pscustomobject]@{ ExitCode = 0; Output = $script:pyOutput[$FilePath]; ErrorText = '' } }
    $py = Find-JarvisPython
    Assert-Equal 'C:\Users\Max\AppData\Local\Programs\Python\Python313\python.exe' $py.Path 'python.org-Python statt Store'
    Assert-Equal '3.13' $py.Version 'Version'
    $script:pyOutput['python'] = @('C:\x\python.exe', $storeBase, '313')
    Assert-True ($null -eq (Find-JarvisPython)) 'nur Store-Python (auch als base_prefix) -> keins'
}

Invoke-Test 'pip-Fallback: fehlt .venv nach "python -m venv" -> klare Meldung statt pip-Fehler' {
    $t = New-TestDir 't'
    [void](Set-TestFile $t 'requirements.txt' 'fastapi==1 --hash=sha256:00')
    $script:calls = @()
    function Find-JarvisUv { return $null }
    function Read-JarvisAnswer { param($Prompt) return 'n' }
    function Find-JarvisPython { return [pscustomobject]@{ Path = '/opt/py311/bin/python3'; Version = '3.11' } }
    # venv meldet Erfolg, die Dateien landen aber anderswo (wie bei der Umleitung des Store-Pythons).
    function Invoke-JarvisProcess { param($FilePath, $ArgumentList, $WorkingDirectory, $Environment) $script:calls += , @($ArgumentList); return 0 }
    Assert-Throws { Initialize-JarvisPythonEnvironment -Target $t } '*nicht angelegt*Microsoft Store*uv-Download*'
    Assert-Equal 1 $script:calls.Count 'pip wird gar nicht erst gestartet'
}

Invoke-Test 'Find-JarvisPython findet ein Python >= 3.11' {
    if (-not $python3) { Write-Host '      (python3 fehlt - uebersprungen)'; return }
    $py = Find-JarvisPython
    $r = Invoke-JarvisCapture -FilePath $python3.Path -ArgumentList @('-c', 'import sys; print(sys.version_info >= (3, 11))')
    if ($r.Output[-1] -eq 'True') {
        Assert-True ($null -ne $py) 'gefunden'
        Assert-True ([System.IO.File]::Exists($py.Path)) 'Pfad existiert'
    } else {
        Assert-True ($null -eq $py) 'zu altes Python wird nicht genommen'
    }
}

Invoke-Test 'Wizard-Aufrufe (Vertrag setup_wizard)' {
    Assert-Equal @('-m', 'app.setup_wizard', 'configure', '--config', 'c.yaml', '--example', 'e.yaml', '--non-interactive') `
        @(Get-JarvisWizardArguments -Subcommand 'configure' -Arguments @('--config', 'c.yaml', '--example', 'e.yaml', '--non-interactive')) 'configure'
    Assert-Equal @('-m', 'app.setup_wizard', 'token', '--secrets', 's.yaml') @(Get-JarvisWizardArguments -Subcommand 'token' -Arguments @('--secrets', 's.yaml')) 'token'
    if (-not $python3) { return }
    # info: JSON-Zeile wird gelesen, auch wenn davor andere Ausgaben stehen.
    $t = New-TestDir 't'
    [void](Set-TestFile $t 'app/__init__.py' '')
    [void](Set-TestFile $t 'app/setup_wizard.py' ("import json, sys`nprint('Hinweis vorab')`nprint(json.dumps({'bind': '0.0.0.0', 'port': 9123, 'local_url': 'http://127.0.0.1:9123/', 'warnings': ['llm.model ist noch TODO ' + chr(8594)]}))`n"))
    $info = Get-JarvisServerInfo -Python $python3.Path -Target $t
    Assert-Equal 9123 $info.port 'port'
    Assert-Equal 'http://127.0.0.1:9123/' $info.local_url 'local_url'
    Assert-Equal ('llm.model ist noch TODO ' + [char]0x2192) @($info.warnings)[0] 'Warnung mit Unicode'
    [void](Set-TestFile $t 'app/setup_wizard.py' "import sys`nprint('config.yaml fehlt', file=sys.stderr)`nsys.exit(2)`n")
    Assert-True ($null -eq (Get-JarvisServerInfo -Python $python3.Path -Target $t)) 'Fehler -> $null'
}

# --- Verknuepfungen (Windows-Funktionen ersetzt) ----------------------------------------------

Invoke-Test 'Startmenue: vier Eintraege mit richtigen Zielen' {
    $t = New-TestDir 'target'
    $menu = New-TestDir 'programs'
    [void](Set-TestFile $t '.venv/Scripts/pythonw.exe' '')
    $script:links = @()
    function Test-JarvisWindows { return $true }
    function Get-JarvisStartMenuDir { return (Join-Path $menu 'JARVIS') }
    function Get-JarvisWindowsPowerShellPath { return 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' }
    function New-JarvisShortcut {
        param($Path, $TargetPath, $Arguments = '', $WorkingDirectory = '', $Description = '', $WindowStyle = 1)
        $script:links += , @{ Path = $Path; Target = $TargetPath; Args = $Arguments; Cwd = $WorkingDirectory; Style = $WindowStyle }
        [System.IO.File]::WriteAllText($Path, 'lnk')
        return $Path
    }
    $created = @(New-JarvisStartMenuShortcuts -Target $t -LocalUrl 'http://127.0.0.1:8765/')
    $names = @($created | ForEach-Object { [System.IO.Path]::GetFileName($_) })
    Assert-Equal @(('JARVIS ' + [char]0x00F6 + 'ffnen.url'), 'JARVIS starten.lnk', 'JARVIS beenden.lnk', 'JARVIS deinstallieren.lnk') $names 'Namen'
    $url = [System.IO.File]::ReadAllText($created[0])
    Assert-True ($url -match "\[InternetShortcut\]\r\nURL=http://127\.0\.0\.1:8765/\r\n") ('.url-Inhalt: ' + $url)
    $start = $script:links[0]
    # "JARVIS starten" laeuft ueber start-hidden.ps1 (Rueckmeldung, Browser, Log-Zeilen bei Fehlern).
    Assert-True ($start.Target -like '*powershell.exe') 'starten -> powershell.exe'
    $startScript = Join-Path (Join-Path $t 'scripts') 'start-hidden.ps1'
    Assert-Equal ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $startScript) $start.Args 'starten Argumente'
    Assert-Equal $t $start.Cwd 'starten Arbeitsordner'
    $stop = $script:links[1]
    Assert-True ($stop.Target -like '*powershell.exe') 'beenden -> powershell.exe'
    $stopScript = Join-Path (Join-Path $t 'scripts') 'stop.ps1'
    Assert-Equal ('-NoProfile -ExecutionPolicy Bypass -File "{0}" -Pause' -f $stopScript) $stop.Args 'beenden Argumente'
    $un = $script:links[2]
    Assert-Equal (Join-Path $t 'Uninstall.cmd') $un.Target 'deinstallieren -> Uninstall.cmd'
    Assert-True ($un.Cwd -ne $t) 'deinstallieren startet nicht im Installationsordner'
}

Invoke-Test 'Autostart: pythonw.exe minimiert, Fallback python.exe' {
    $t = New-TestDir 'target'
    $startup = New-TestDir 'startup'
    $script:links = @()
    function Test-JarvisWindows { return $true }
    function Get-JarvisStartupDir { return $startup }
    function New-JarvisShortcut {
        param($Path, $TargetPath, $Arguments = '', $WorkingDirectory = '', $Description = '', $WindowStyle = 1)
        $script:links += , @{ Path = $Path; Target = $TargetPath; Args = $Arguments; Cwd = $WorkingDirectory; Style = $WindowStyle }
        return $Path
    }
    Assert-Throws { New-JarvisAutostart -Target $t } '*Keine virtuelle Umgebung*'
    [void](Set-TestFile $t '.venv/Scripts/python.exe' '')
    $a = New-JarvisAutostart -Target $t
    Assert-True $a.Fallback 'Fallback python.exe'
    [void](Set-TestFile $t '.venv/Scripts/pythonw.exe' '')
    $a = New-JarvisAutostart -Target $t
    Assert-False $a.Fallback 'pythonw.exe vorhanden'
    Assert-Equal (Join-Path $startup 'JARVIS.lnk') $a.Path 'Pfad'
    $last = $script:links[-1]
    Assert-True ($last.Target -like '*pythonw.exe') 'Ziel pythonw.exe'
    Assert-Equal '-m app' $last.Args 'Argumente'
    Assert-Equal 7 $last.Style 'minimiert'
    Assert-Equal $t $last.Cwd 'Arbeitsordner'
}

Invoke-Test 'Verknuepfungen loeschen nur im Startmenue/Autostart' {
    $programs = New-TestDir 'programs'
    $startup = New-TestDir 'startup'
    $other = New-TestDir 'other'
    function Get-JarvisShortcutRoots { return @($programs, $startup) }
    $a = Set-TestFile $programs 'JARVIS/JARVIS starten.lnk'
    $b = Set-TestFile $startup 'JARVIS.lnk'
    $c = Set-TestFile $other 'JARVIS.lnk'
    $d = Set-TestFile $programs 'JARVIS/notiz.txt'
    $e = Set-TestFile $programs 'Fremd/JARVIS.lnk'
    $r = Remove-JarvisShortcutFiles -Paths @($a, $b, $c, $d, $e)
    Assert-False ([System.IO.File]::Exists($a)) 'Startmenue-Eintrag weg'
    Assert-False ([System.IO.File]::Exists($b)) 'Autostart weg'
    Assert-True ([System.IO.File]::Exists($c)) 'fremder Ordner bleibt'
    Assert-True ([System.IO.File]::Exists($d)) 'keine .lnk/.url bleibt'
    Assert-True ([System.IO.File]::Exists($e)) 'anderer Unterordner bleibt'
    Assert-True ([System.IO.Directory]::Exists((Join-Path $programs 'JARVIS'))) 'nicht leerer JARVIS-Ordner bleibt'
    Assert-Equal 3 $r.Skipped.Count 'uebersprungen'
    Remove-Item -LiteralPath $d
    [void](Remove-JarvisShortcutFiles -Paths @($a))
    Assert-False ([System.IO.Directory]::Exists((Join-Path $programs 'JARVIS'))) 'leerer JARVIS-Ordner weg'
    Assert-False (Test-JarvisShortcutPathAllowed ('C:\x' + [char]0 + '.lnk')) 'ungueltiger Pfad wirft nicht'
}

Invoke-Test 'Verknuepfungen einer anderen Installation bleiben liegen' {
    $programs = New-TestDir 'programs'
    $startup = New-TestDir 'startup'
    $mine = New-TestDir 'mine'
    $other = New-TestDir 'other'
    function Get-JarvisShortcutRoots { return @($programs, $startup) }
    $script:infos = @{}
    function Get-JarvisShortcutInfo { param($Path) return $script:infos[[System.IO.Path]::GetFileName($Path)] }
    $start = Set-TestFile $programs 'JARVIS/JARVIS starten.lnk'
    $stop = Set-TestFile $programs 'JARVIS/JARVIS beenden.lnk'
    $url = Set-TestFile $programs ('JARVIS/JARVIS ' + [char]0x00F6 + 'ffnen.url')
    $auto = Set-TestFile $startup 'JARVIS.lnk'
    $unreadable = Set-TestFile $startup 'JARVIS-alt.lnk'
    $script:infos['JARVIS starten.lnk'] = [pscustomobject]@{ TargetPath = (Join-Path $mine '.venv/Scripts/pythonw.exe'); Arguments = '-m app'; WorkingDirectory = $mine }
    $script:infos['JARVIS beenden.lnk'] = [pscustomobject]@{ TargetPath = '/win/powershell.exe'; Arguments = ('-NoProfile -File "{0}/scripts/stop.ps1" -Pause' -f $mine); WorkingDirectory = '' }
    $script:infos['JARVIS.lnk'] = [pscustomobject]@{ TargetPath = (Join-Path $other '.venv/Scripts/pythonw.exe'); Arguments = '-m app'; WorkingDirectory = $other }
    Assert-True (Test-JarvisShortcutOwnedBy -Path $start -Target $mine) 'Ziel in der eigenen venv'
    Assert-True (Test-JarvisShortcutOwnedBy -Path $stop -Target $mine) 'eigener Pfad in den Argumenten'
    Assert-False (Test-JarvisShortcutOwnedBy -Path $stop -Target ($mine + 'x')) 'Praefix-Trick in den Argumenten'
    Assert-False (Test-JarvisShortcutOwnedBy -Path $auto -Target $mine) 'Autostart zeigt auf andere Installation'
    Assert-True (Test-JarvisShortcutOwnedBy -Path $url -Target $mine) '.url gilt als eigene'
    Assert-True (Test-JarvisShortcutOwnedBy -Path $unreadable -Target $mine) 'nicht lesbare .lnk gilt als eigene'
    $r = Remove-JarvisShortcutFiles -Paths @($start, $stop, $url, $auto) -Owner $mine
    Assert-Equal 4 $r.Removed.Count ('drei Verknuepfungen + leerer Startmenue-Ordner: ' + ($r.Removed -join ', '))
    Assert-False ([System.IO.Directory]::Exists((Join-Path $programs 'JARVIS'))) 'Startmenue-Ordner weg'
    Assert-Equal @($auto) @($r.Foreign) 'fremder Autostart'
    Assert-True ([System.IO.File]::Exists($auto)) 'fremder Autostart bleibt'
    Assert-Equal 'foreign' (Remove-JarvisAutostart -Path $auto -Owner $mine) 'Status foreign'
    Assert-True ([System.IO.File]::Exists($auto)) 'immer noch da'
    Assert-Equal 'removed' (Remove-JarvisAutostart -Path $auto -Owner $other) 'Status removed'
    Assert-Equal 'missing' (Remove-JarvisAutostart -Path $auto -Owner $other) 'Status missing'
}

# --- Prozesserkennung -------------------------------------------------------------------------

Invoke-Test 'Serverprozess-Erkennung (synthetisch)' {
    $t = New-TestDir 'target'
    $venv = Join-Path $t '.venv'
    $py = Join-Path (Join-Path $venv 'bin') 'python'
    $now = [DateTime]::UtcNow
    $mk = { param($path, $cmd, $parent, $name, $start) [pscustomobject]@{ Id = 4242; Name = $name; Path = $path; CommandLine = $cmd; ParentPath = $parent; StartTimeUtc = $start } }
    Assert-True (Test-JarvisOwnServerProcess (& $mk $py ('"{0}" -m app' -f $py) $null 'python' $now) $t $null) 'direkt aus der venv'
    Assert-True (Test-JarvisOwnServerProcess (& $mk '/usr/bin/python3.11' ($py + ' -m app') $null 'python' $now) $t $null) 'Linux: argv0 in der venv'
    Assert-True (Test-JarvisOwnServerProcess (& $mk 'C:\Python312\pythonw.exe' '"C:\Python312\pythonw.exe" -m app' (Join-Path $venv 'Scripts\pythonw.exe') 'pythonw.exe' $now) $t $null) 'Windows: venv-Starter als Eltern'
    Assert-False (Test-JarvisOwnServerProcess (& $mk $py ($py + ' -m pip install x') $null 'python' $now) $t $null) 'anderes Kommando'
    Assert-False (Test-JarvisOwnServerProcess (& $mk '/andere/.venv/bin/python' '/andere/.venv/bin/python -m app' $null 'python' $now) $t $null) 'andere Installation'
    Assert-False (Test-JarvisOwnServerProcess (& $mk ($t + 'x/.venv/bin/python') ($t + 'x/.venv/bin/python -m app') $null 'python' $now) $t $null) 'Praefix-Trick'
    $pidInfo = [pscustomobject]@{ Pid = 4242; Executable = $py; Started = $now.AddSeconds(2) }
    Assert-True (Test-JarvisOwnServerProcess (& $mk '/usr/bin/python3' $null $null 'python3' $now) $t $pidInfo) 'PID-Datei mit passender Startzeit'
    $old = [pscustomobject]@{ Pid = 4242; Executable = $py; Started = $now.AddHours(-3) }
    Assert-False (Test-JarvisOwnServerProcess (& $mk '/usr/bin/python3' $null $null 'python3' $now) $t $old) 'PID wiederverwendet (Startzeit passt nicht)'
    Assert-False (Test-JarvisOwnServerProcess (& $mk '/usr/bin/vim' $null $null 'vim' $now) $t $pidInfo) 'kein Python'
    $exact = [pscustomobject]@{ Pid = 4242; Executable = $py; Started = $now.AddSeconds(3); CreateTime = $now.AddMilliseconds(400) }
    Assert-True (Test-JarvisOwnServerProcess (& $mk '/usr/bin/python3' $null $null 'python3' $now) $t $exact) 'create_time passt'
    $wrong = [pscustomobject]@{ Pid = 4242; Executable = $py; Started = $now.AddSeconds(3); CreateTime = $now.AddSeconds(-30) }
    Assert-False (Test-JarvisOwnServerProcess (& $mk '/usr/bin/python3' $null $null 'python3' $now) $t $wrong) 'create_time passt nicht (auch wenn started passen wuerde)'
    Assert-Equal 'C:\a b\p.exe' (Get-JarvisCommandLineExe '"C:\a b\p.exe" -m app') 'Programm in Anfuehrungszeichen'
    Assert-Equal '/x/python' (Get-JarvisCommandLineExe '/x/python -m app') 'Programm ohne Anfuehrungszeichen'
}

Invoke-Test 'Ordner mit Einstellungen einer frueheren Installation' {
    $d = New-TestDir 'alt'
    Assert-False (Test-JarvisOnlyUserData $d) 'leer'
    [void](Set-TestFile $d 'config.yaml')
    [void](Set-TestFile $d 'state/logs/server.log')
    Assert-True (Test-JarvisOnlyUserData $d) 'nur config.yaml + state'
    [void](Set-TestFile $d 'privat.txt')
    Assert-False (Test-JarvisOnlyUserData $d) 'mit fremder Datei'
}

Invoke-Test 'Format-JarvisDate' {
    Assert-True ((Format-JarvisDate '2026-10-02T16:00:00Z') -match '^2026-10-0\d \d\d:\d\d$') 'ISO-String'
    Assert-True ((Format-JarvisDate ([DateTime]::UtcNow)) -match '^\d{4}-\d\d-\d\d \d\d:\d\d$') 'DateTime'
    Assert-Equal '?' (Format-JarvisDate $null) 'null'
}

Invoke-Test 'PID-Datei: Zeitformate' {
    $t = New-TestDir 'target'
    foreach ($case in @(
            @{ Text = '2026-10-02T16:00:00+00:00'; Expect = '2026-10-02T16:00:00' },
            @{ Text = '2026-10-02T18:00:00+02:00'; Expect = '2026-10-02T16:00:00' },
            @{ Text = '2026-10-02T16:00:00Z'; Expect = '2026-10-02T16:00:00' },
            @{ Text = '2026-10-02T16:00:00.123456+00:00'; Expect = '2026-10-02T16:00:00' })) {
        [void](Set-TestFile $t 'state/server.pid' ('{{"pid": 1234, "executable": "/x/.venv/bin/python", "started": "{0}"}}' -f $case.Text))
        $info = Read-JarvisPidFile $t
        Assert-Equal 1234 $info.Pid 'pid'
        Assert-Equal $case.Expect $info.Started.ToString('yyyy-MM-ddTHH:mm:ss') ('started ' + $case.Text)
    }
    [void](Set-TestFile $t 'state/server.pid' '{"pid": 77, "executable": "/x/.venv/bin/python", "started": "2026-10-02T16:00:00+00:00", "create_time": 1790956800.25}')
    $info = Read-JarvisPidFile $t
    Assert-Equal '2026-10-02T16:00:00.250' $info.CreateTime.ToString('yyyy-MM-ddTHH:mm:ss.fff') 'create_time (Epoch) -> UTC'
    [void](Set-TestFile $t 'state/server.pid' '{"pid": 77, "executable": "/x", "started": null, "create_time": null}')
    $info = Read-JarvisPidFile $t
    Assert-True ($null -eq $info.CreateTime -and $null -eq $info.Started) 'fehlende Zeiten -> $null'
    [void](Set-TestFile $t 'state/server.pid' 'kein json')
    Assert-True ($null -eq (Read-JarvisPidFile $t)) 'kaputte PID-Datei wird ignoriert'
}

# --- Sonstiges --------------------------------------------------------------------------------

Invoke-Test 'Ja/Nein-Fragen' {
    $script:answers = New-Object System.Collections.Generic.Queue[string]
    function Read-JarvisAnswer { param($Prompt) return $script:answers.Dequeue() }
    foreach ($a in @('', 'j', 'NEIN', 'vielleicht', 'yes')) { $script:answers.Enqueue($a) }
    Assert-True (Read-JarvisYesNo 'Frage?' -Default $true) 'leer -> Standard ja'
    Assert-True (Read-JarvisYesNo 'Frage?' -Default $false) 'j'
    Assert-False (Read-JarvisYesNo 'Frage?' -Default $true) 'NEIN'
    Assert-True (Read-JarvisYesNo 'Frage?' -Default $false) 'erst ungueltig, dann yes'
    function Read-JarvisAnswer { param($Prompt) throw 'darf nicht fragen' }
    Assert-False (Read-JarvisYesNo 'Frage?' -Default $false -AssumeDefault) 'AssumeDefault nein'
    Assert-True (Read-JarvisYesNo 'Frage?' -Default $true -AssumeDefault) 'AssumeDefault ja'
}

Invoke-Test 'Fehlermeldungen ohne .NET-Huelle' {
    $msg = $null
    try { [void][System.IO.File]::ReadAllText('/gibt/es/nicht.txt') } catch { $msg = Get-JarvisErrorMessage $_ }
    Assert-True ($msg -and $msg -notlike 'Exception calling*') ('Meldung: ' + $msg)
    Assert-True ($msg -like '*nicht.txt*') ('Pfad in der Meldung: ' + $msg)
    try { throw 'Eigene Meldung' } catch { $msg = Get-JarvisErrorMessage $_ }
    Assert-Equal 'Eigene Meldung' $msg 'throw-Text bleibt'
}

Invoke-Test 'Private IPv4-Adressen' {
    foreach ($ip in @('10.1.2.3', '172.16.0.1', '172.31.255.1', '192.168.0.20')) { Assert-True (Test-JarvisPrivateIPv4 $ip) $ip }
    foreach ($ip in @('8.8.8.8', '172.32.0.1', '100.64.0.1', '127.0.0.1', '169.254.1.1', 'fe80::1', 'kein')) { Assert-False (Test-JarvisPrivateIPv4 $ip) $ip }
    Assert-Equal 0 @(Get-JarvisLanAddresses).Count 'ohne Windows keine Adressliste'
    function Get-JarvisIPv4Interfaces {
        return @(
            [pscustomobject]@{ Address = '192.168.0.20'; Interface = 'WLAN' },
            [pscustomobject]@{ Address = '127.0.0.1'; Interface = 'Loopback' },
            [pscustomobject]@{ Address = '100.101.102.103'; Interface = 'Tailscale' },
            [pscustomobject]@{ Address = '10.0.0.5'; Interface = 'Ethernet' },
            [pscustomobject]@{ Address = '192.168.0.20'; Interface = 'WLAN 2' },
            [pscustomobject]@{ Address = '169.254.3.4'; Interface = 'APIPA' })
    }
    $lan = @(Get-JarvisLanAddresses)
    Assert-Equal @('10.0.0.5', '192.168.0.20') @($lan | ForEach-Object { $_.Address }) 'nur private, ohne Doppelte'
    Assert-Equal @('Ethernet', 'WLAN') @($lan | ForEach-Object { $_.Interface }) 'Adapternamen'
    foreach ($ip in @('100.64.0.1', '100.127.255.254')) { Assert-True (Test-JarvisTailscaleIPv4 $ip) $ip }
    foreach ($ip in @('100.63.0.1', '100.128.0.1', '10.0.0.1', 'x')) { Assert-False (Test-JarvisTailscaleIPv4 $ip) $ip }
}

Invoke-Test 'Tailscale-IP: CLI (nur lesend), sonst Adapter' {
    $bin = New-TestDir 'bin'
    $fake = Set-TestFile $bin 'tailscale' "#!/bin/sh`nif [ `"`$1 `$2`" = 'ip -4' ]; then echo 100.88.1.2; exit 0; fi`necho falsch >&2; exit 3`n"
    & chmod +x $fake
    $oldPath = $env:PATH
    try {
        $env:PATH = $bin + [System.IO.Path]::PathSeparator + $oldPath
        Assert-Equal '100.88.1.2' (Get-JarvisTailscaleIp) 'aus tailscale ip -4'
        $env:PATH = '/nirgendwo'
        function Get-JarvisIPv4Interfaces { return @([pscustomobject]@{ Address = '100.99.0.7'; Interface = 'Tailscale' }) }
        Assert-Equal '100.99.0.7' (Get-JarvisTailscaleIp) 'vom Tailscale-Adapter'
        function Get-JarvisIPv4Interfaces { return @([pscustomobject]@{ Address = '192.168.1.2'; Interface = 'WLAN' }) }
        Assert-True ($null -eq (Get-JarvisTailscaleIp)) 'kein Tailscale'
    } finally {
        $env:PATH = $oldPath
    }
}

Invoke-Test 'Firewall-Befehle entsprechen README Abschnitt 5' {
    $readme = [System.IO.File]::ReadAllText((Join-Path $script:JarvisRoot 'README.md'))
    $start = $readme.IndexOf('## 5.')
    Assert-True ($start -ge 0) 'Abschnitt 5 vorhanden'
    $end = $readme.IndexOf("`n## ", $start + 5)
    if ($end -lt 0) { $end = $readme.Length }
    $section = $readme.Substring($start, $end - $start) -replace "``\r?\n\s*", ' '
    $fromReadme = @($section -split "`r?`n" | ForEach-Object { $_.Trim() } |
        Where-Object { $_ -like 'New-NetFirewallRule *' -or $_ -like 'Get-NetFirewallApplicationFilter *' } |
        ForEach-Object { $_ -replace '\s+', ' ' })
    $ours = @(Get-JarvisFirewallCommands -Port 8765 | ForEach-Object { $_ -replace '\s+', ' ' })
    Assert-Equal $fromReadme $ours 'gleiche Befehle wie im README (zwei Freigaben + Aufraeumen der Block-Regeln)'
    Assert-True ((Get-JarvisFirewallCommands -Port 9000)[0] -like '*JARVIS 9000 (LAN)*-LocalPort 9000 *') 'Port wird eingesetzt'
}

Invoke-Test 'Firewall: Block-Regeln fuer das Python der venv aufraeumen (nur angezeigt)' {
    $pyHome = "C:\Users\Max O'Brien\AppData\Roaming\uv\python\cpython-3.12.11-windows-x86_64-none"
    $cmds = @(Get-JarvisFirewallCommands -Port 8765 -PythonHome $pyHome)
    Assert-Equal 3 $cmds.Count 'drei Befehle'
    $cleanup = $cmds[2]
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($cleanup, [ref]$tokens, [ref]$errors)
    Assert-Equal 0 @($errors).Count ('gueltiger PowerShell-Befehl: ' + $cleanup)
    Assert-True ($cleanup -like "*`$_.Direction -eq 'Inbound' -and `$_.Action -eq 'Block'*| Remove-NetFirewallRule") ('nur eingehende Block-Regeln: ' + $cleanup)
    $like = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.BinaryExpressionAst] -and $n.Operator -eq 'Ilike' }, $true))[0]
    $pattern = $like.Right.Value
    Assert-True (($pyHome + '\pythonw.exe') -like $pattern) ('pythonw.exe: ' + $pattern)
    Assert-True (($pyHome.ToLowerInvariant() + '\python.exe') -like $pattern) 'python.exe, Gross-/Kleinschreibung egal'
    Assert-False ('C:\Andere\python.exe' -like $pattern) 'fremdes Python bleibt'
    $br = @(Get-JarvisFirewallCommands -PythonHome 'D:\Py [test]\')[2]
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($br, [ref]$tokens, [ref]$errors)
    $pattern = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.BinaryExpressionAst] -and $n.Operator -eq 'Ilike' }, $true))[0].Right.Value
    Assert-True ('D:\Py [test]\python.exe' -like $pattern) ('[ ] im Pfad maskiert: ' + $pattern)
}

Invoke-Test 'Get-JarvisVenvHome liest home aus pyvenv.cfg' {
    $t = New-TestDir 't'
    Assert-True ($null -eq (Get-JarvisVenvHome -Target $t)) 'ohne pyvenv.cfg'
    $uml = 'C:\Users\Max M' + [char]0x00FC + 'ller\AppData\Roaming\uv\python\cpython-3.12'
    $path = Join-JarvisRelPath $t '.venv/pyvenv.cfg'
    [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($path))
    [System.IO.File]::WriteAllText($path, ("home = {0}`r`nimplementation = CPython`r`nversion_info = 3.12.11`r`n" -f $uml), (New-Object System.Text.UTF8Encoding($false)))
    Assert-Equal $uml (Get-JarvisVenvHome -Target $t) 'home mit Umlaut'
}

# --- Links, OneDrive-Platzhalter, Ordner-Links im Ziel ------------------------------------------

Invoke-Test 'Reparse-Tags: Links/Junctions ja, OneDrive-Platzhalter nein' {
    Assert-True (Test-JarvisLinkTag ([long]2684354572)) 'IO_REPARSE_TAG_SYMLINK (0xA000000C)'
    Assert-True (Test-JarvisLinkTag ([long]2684354563)) 'IO_REPARSE_TAG_MOUNT_POINT = Junction (0xA0000003)'
    Assert-True (Test-JarvisLinkTag ([long]2147483675)) 'IO_REPARSE_TAG_APPEXECLINK (0x8000001B)'
    Assert-True (Test-JarvisLinkTag $null) 'Tag nicht lesbar = Link (sichere Seite)'
    # IO_REPARSE_TAG_CLOUD (0x9000001A), CLOUD_1 (0x9000101A), CLOUD_F (0x9000F01A), DEDUP (0x80000013)
    foreach ($tag in @([long]2415919130, [long]2415923226, [long]2415980570, [long]2147483667)) {
        Assert-False (Test-JarvisLinkTag $tag) ('kein Link: ' + $tag)
    }
    $placeholder = [pscustomobject]@{ Attributes = [System.IO.FileAttributes]'Directory, ReparsePoint'; FullName = 'C:\OneDrive\JARVIS-Setup' }
    $plain = [pscustomobject]@{ Attributes = [System.IO.FileAttributes]'Directory'; FullName = 'C:\x' }
    function Get-JarvisReparseTag { param($Path) return [long]2415919130 }
    Assert-False (Test-JarvisLinkLike $placeholder) 'OneDrive-Ordner ist kein Link'
    Assert-False (Test-JarvisLinkLike $plain) 'normaler Ordner'
    function Get-JarvisReparseTag { param($Path) return [long]2684354563 }
    Assert-True (Test-JarvisLinkLike $placeholder) 'Junction'
    function Get-JarvisReparseTag { param($Path) return $null }
    Assert-True (Test-JarvisLinkLike $placeholder) 'unbekannt'
}

Invoke-Test 'Programmdateien aus OneDrive (Platzhalter) werden kopiert, Links nicht' {
    $src = New-TestDir 'src'
    $cloud = New-TestDir 'cloud'
    $outside = New-TestDir 'outside'
    foreach ($rel in @('pyproject.toml', 'README.md')) { [void](Set-TestFile $src $rel) }
    foreach ($rel in @('__main__.py', 'tools/led.py')) { [void](Set-TestFile $cloud $rel) }
    [void](Set-TestFile $outside 'geheim.txt')
    # Unter Linux tragen nur symbolische Links das Attribut ReparsePoint: "app" spielt einen
    # OneDrive-Platzhalter-Ordner (Tag CLOUD), "linked" einen echten Link.
    [void](New-Item -ItemType SymbolicLink -Path (Join-Path $src 'app') -Target $cloud)
    [void](New-Item -ItemType SymbolicLink -Path (Join-Path $src 'linked') -Target $outside)
    function Get-JarvisReparseTag {
        param($Path)
        if ([System.IO.Path]::GetFileName($Path) -eq 'app') { return [long]2415919130 }
        return [long]2684354572
    }
    Assert-Equal @('README.md', 'app/__main__.py', 'app/tools/led.py', 'pyproject.toml') @(Get-JarvisProgramFiles -Source $src) 'Dateiliste'
}

Invoke-Test 'Pflichtdateien fehlen in der Dateiliste -> OneDrive-Hinweis vor dem Kopieren' {
    Assert-Throws { Assert-JarvisProgramFileList -Files @('README.md', 'pyproject.toml') -Source 'C:\Users\Max\OneDrive\Desktop\JARVIS-Setup' } '*fehlen Programmdateien*app/__main__.py*OneDrive*C:\JARVIS-Setup*'
    Assert-JarvisProgramFileList -Files @(Get-JarvisRequiredProgramFiles | ForEach-Object { $_.ToUpperInvariant() }) -Source 'x'
    Assert-JarvisProgramFileList -Files @(Get-JarvisProgramFiles -Source $script:JarvisRoot) -Source $script:JarvisRoot
}

Invoke-Test 'Ordner-Link im Ziel: nie dahinter loeschen oder schreiben' {
    $root = New-TestDir 'inst'
    $outside = New-TestDir 'outside'
    [void](Set-TestFile $outside 'wake.html' 'MEINE VERSION')
    [void](Set-TestFile $outside 'my-own.html' 'MEINS')
    [void](New-Item -ItemType SymbolicLink -Path (Join-Path $root 'launcher') -Target $outside)
    [void](Set-TestFile $root 'app/a.py')
    Assert-True (Test-JarvisHasLinkAncestor -Root $root -Path (Join-Path $root 'launcher/wake.html')) 'Link erkannt'
    Assert-False (Test-JarvisHasLinkAncestor -Root $root -Path (Join-Path $root 'app/a.py')) 'normaler Ordner'
    $r = Remove-JarvisRelativeFiles -Target $root -Files @('launcher/wake.html', 'app/a.py')
    Assert-Equal 'MEINE VERSION' (Get-TestFile $outside 'wake.html') 'Datei hinter dem Link bleibt'
    Assert-True ($r.Skipped -contains 'launcher/wake.html') 'als uebersprungen gemeldet'
    Assert-False (Test-TestPath $root 'app/a.py') 'normale Programmdatei entfernt'
    $r = Remove-JarvisStaleFiles -Target $root -OldFiles @('launcher/wake.html', 'launcher/my-own.html') -NewFiles @()
    Assert-Equal 'MEINS' (Get-TestFile $outside 'my-own.html') 'Update loescht nichts hinter dem Link'
    $src = New-TestDir 'src'
    [void](Set-TestFile $src 'launcher/wake.html' 'NEU')
    Assert-Throws { Copy-JarvisProgramFiles -Source $src -Target $root -Files @('launcher/wake.html') } '*Ordner-Link*nie ausserhalb*'
    Assert-Equal 'MEINE VERSION' (Get-TestFile $outside 'wake.html') 'nicht durch den Link ueberschrieben'
    # Ein Datei-Link am Zielort wird ersetzt, nie durch ihn hindurch geschrieben.
    $dst = New-TestDir 'dst'
    $foreign = Set-TestFile $outside 'fremd.py' 'FREMD'
    [void][System.IO.Directory]::CreateDirectory((Join-Path $dst 'app'))
    [void](New-Item -ItemType SymbolicLink -Path (Join-Path $dst 'app/x.py') -Target $foreign)
    [void](Set-TestFile $src 'app/x.py' 'NEU')
    Assert-Equal 1 (Copy-JarvisProgramFiles -Source $src -Target $dst -Files @('app/x.py')) 'kopiert'
    Assert-Equal 'FREMD' ([System.IO.File]::ReadAllText($foreign)) 'Ziel des Datei-Links unveraendert'
    Assert-Equal 'NEU' (Get-TestFile $dst 'app/x.py') 'echte Datei am Zielort'
    Assert-False (Test-JarvisReparsePoint (Join-Path $dst 'app/x.py')) 'kein Link mehr'
}

Invoke-Test 'Marker-Eintraege, die Windows auf geschuetzte Namen kuerzt, sind ungueltig' {
    foreach ($bad in @('config.yaml ', 'secrets.yaml ', 'config.yaml.', 'state./scripts.json', 'state ./logs/server.log',
            ' config.yaml', 'CONFIG~1.YAM', 'STATE~1/scripts.json', 'app/x.py.', 'app /x.py')) {
        Assert-False (Test-JarvisRelPathValid $bad) ('ungueltig: [' + $bad + ']')
    }
    Assert-True (Test-JarvisProtectedRelPath 'config.yaml ') 'geschuetzt trotz Leerzeichen'
    Assert-True (Test-JarvisProtectedRelPath 'state./x') 'geschuetzt trotz Punkt'
    $root = New-TestDir 'inst'
    [void](Set-TestFile $root 'config.yaml' 'MEINE CONFIG')
    [void](Set-TestFile $root 'secrets.yaml' 'MEIN TOKEN')
    [void](Set-TestFile $root 'state/scripts.json' 'MEIN STATUS')
    $r = Remove-JarvisRelativeFiles -Target $root -Files @('config.yaml ', 'secrets.yaml ', 'state./scripts.json', 'CONFIG~1.YAM')
    Assert-Equal 4 $r.Skipped.Count 'alle uebersprungen'
    Assert-Equal 'MEINE CONFIG' (Get-TestFile $root 'config.yaml') 'config.yaml bleibt'
    Assert-Equal 'MEIN TOKEN' (Get-TestFile $root 'secrets.yaml') 'secrets.yaml bleibt'
    Assert-Equal 'MEIN STATUS' (Get-TestFile $root 'state/scripts.json') 'state bleibt'
}

Invoke-Test 'Zielordner ueber Link, Netzwerk- oder Geraetepfad auf geschuetzte Ordner wird abgelehnt' {
    $fakeHome = New-TestDir 'fakehome'
    $links = New-TestDir 'links'
    $homeLink = Join-Path $links 'homelink'
    $parentLink = Join-Path $links 'parentlink'
    [void](New-Item -ItemType SymbolicLink -Path $homeLink -Target $fakeHome)
    [void](New-Item -ItemType SymbolicLink -Path $parentLink -Target (Split-Path -Parent $fakeHome))
    $oldHome = $env:HOME
    $env:HOME = $fakeHome
    try {
        Assert-Throws { Assert-JarvisSafeTarget $fakeHome } '*geschuetzter Ordner*'
        Assert-Throws { Assert-JarvisSafeTarget $homeLink } '*zeigt auf*geschuetzter Ordner*'
        Assert-Throws { Assert-JarvisSafeTarget $parentLink } '*geschuetzter Ordner*'
        Assert-Equal (Join-Path $fakeHome 'JARVIS') (Resolve-JarvisPhysicalPath (Join-Path $homeLink 'JARVIS')) 'Link aufgeloest, Rest angehaengt'
        $ok = Join-Path $homeLink 'JARVIS'
        Assert-Equal $ok (Assert-JarvisSafeTarget $ok) 'eigener Unterordner hinter dem Link ist erlaubt'
    } finally {
        $env:HOME = $oldHome
    }
    foreach ($bad in @('\\server\share\JARVIS', '\\?\C:\JARVIS', '\\.\C:\JARVIS', '//server/share/JARVIS')) {
        Assert-Throws { Assert-JarvisSafeTarget $bad } '*Netzwerk- und Geraetepfade*'
    }
}

Invoke-Test 'Marker wird atomar geschrieben (Zwischendatei, kein Rest)' {
    $dir = New-TestDir 'm'
    Write-JarvisManifest -Target $dir -Manifest (New-JarvisManifest -Version '1.0.0' -Source 's' -Files @('a.py'))
    Write-JarvisManifest -Target $dir -Manifest (New-JarvisManifest -Version '2.0.0' -Source 's' -Files @('b.py'))
    Assert-Equal '2.0.0' (Read-JarvisManifest $dir).version 'ersetzt'
    Assert-False (Test-TestPath $dir (Get-JarvisMarkerTempName)) 'keine Zwischendatei'
    # Rest einer abgebrochenen Schreibaktion stoert nicht, wird nie kopiert und nie als Programmdatei gefuehrt.
    [void](Set-TestFile $dir (Get-JarvisMarkerTempName) '{halb')
    Write-JarvisManifest -Target $dir -Manifest (New-JarvisManifest -Version '3.0.0' -Source 's')
    Assert-Equal '3.0.0' (Read-JarvisManifest $dir).version 'trotz altem Rest'
    Assert-False (Test-TestPath $dir (Get-JarvisMarkerTempName)) 'Rest aufgeraeumt'
    Assert-True (Test-JarvisProtectedRelPath (Get-JarvisMarkerTempName)) 'geschuetzt'
    Assert-True (Test-JarvisExcludedFile -Name (Get-JarvisMarkerTempName) -IsTop $true) 'nie kopiert'
    $m = New-JarvisManifest -Version '1' -Source 's' -Preexisting @('config.yaml', 'state')
    Write-JarvisManifest -Target $dir -Manifest $m
    Assert-Equal @('config.yaml', 'state') @(Get-JarvisManifestList (Read-JarvisManifest $dir) 'preexisting') 'preexisting'
    Write-JarvisManifest -Target $dir -Manifest (New-JarvisRemnantManifest (Read-JarvisManifest $dir))
    $rest = Read-JarvisManifest $dir
    Assert-True (Test-JarvisRemnantManifest $rest) 'Rest-Marker'
    Assert-Equal @('config.yaml', 'state') @(Get-JarvisManifestList $rest 'preexisting') 'Rest-Marker behaelt preexisting'
}

Invoke-Test 'Fremde Eintraege im Zielordner' {
    $d = New-TestDir 'ziel'
    foreach ($rel in @('config.yaml', 'secrets.yaml', 'state/x', '.jarvis-install.json', 'README.md', 'app/__init__.py', '.venv/keep.txt')) { [void](Set-TestFile $d $rel) }
    Assert-Equal @('.venv', 'app', 'README.md') @(Get-JarvisForeignEntries $d) 'nur fremde Namen'
}

Invoke-Test 'Entfernt-Zaehler zaehlt nur Dateien, Ordner getrennt' {
    $root = New-TestDir 'inst'
    foreach ($rel in @('app/a.py', 'app/sub/b.py', 'web/index.html')) { [void](Set-TestFile $root $rel) }
    $r = Remove-JarvisRelativeFiles -Target $root -Files @('app/a.py', 'app/sub/b.py', 'web/index.html')
    Assert-Equal 3 $r.Removed.Count 'drei Dateien'
    Assert-Equal 3 $r.RemovedDirs.Count 'app/sub, app, web'
    Assert-False (Test-TestPath $root 'app') 'Ordner weg'
}

# --- Serverstart ------------------------------------------------------------------------------

Invoke-Test 'Serverstart (Windows): Arbeitsordner woertlich, ohne Fenster' {
    $t = Join-Path (New-TestDir 'br') 'JARVIS [neu]'
    [void](Set-TestFile $t '.venv/Scripts/python.exe' '')
    function Test-JarvisWindows { return $true }
    $psi = New-JarvisServerStartInfo -Target $t
    Assert-Equal $t $psi.WorkingDirectory 'Arbeitsordner mit [ ] unveraendert'
    Assert-Equal '-m app' $psi.Arguments 'Argumente'
    Assert-True $psi.UseShellExecute 'ShellExecute (eigener Prozess, keine geerbten Handles)'
    Assert-Equal 'Minimized' ([string]$psi.WindowStyle) 'python.exe minimiert'
    [void](Set-TestFile $t '.venv/Scripts/pythonw.exe' '')
    $psi = New-JarvisServerStartInfo -Target $t
    Assert-True ($psi.FileName -like '*pythonw.exe') 'pythonw.exe'
    Assert-Equal 'Hidden' ([string]$psi.WindowStyle) 'ohne Fenster'
}

Invoke-Test 'Serverstart (Testmodus): Ordner mit [ ] wird nicht als Platzhalter gelesen' {
    $base = New-TestDir 'br'
    $t = Join-Path $base 'JARVIS [neu]'
    # Ein Ordner, auf den das Muster "JARVIS [neu]" passen wuerde (Start-Process -WorkingDirectory nahm ihn):
    [void][System.IO.Directory]::CreateDirectory((Join-Path $base 'JARVIS n'))
    $py = Set-TestFile $t '.venv/bin/python' "#!/bin/sh`necho `"`$@`" > args.txt`npwd > cwd.txt`necho gestartet`necho fehlerkanal >&2`n"
    & chmod +x $py
    $proc = Start-JarvisServer -Target $t
    Assert-True ($proc.WaitForExit(15000)) 'Prozess beendet'
    Assert-Equal $t (Get-TestFile $t 'cwd.txt').Trim() 'richtiger Arbeitsordner'
    Assert-Equal '-m app' (Get-TestFile $t 'args.txt').Trim() 'Argumente'
    Assert-Equal 'gestartet' (Get-TestFile $t 'state/logs/server-stdout.log').Trim() 'stdout im Log'
    Assert-Equal 'fehlerkanal' (Get-TestFile $t 'state/logs/server-stderr.log').Trim() 'stderr im Log'
    Assert-False (Test-TestPath (Join-Path $base 'JARVIS n') 'cwd.txt') 'nicht im falschen Ordner gestartet'
}

Invoke-Test 'Startprobleme aus dem Log fuer Laien uebersetzen' {
    $d = New-TestDir 'log'
    $lines = @(1..20 | ForEach-Object { 'INFO: Zeile ' + $_ }) + "ERROR: [Errno 98] error while attempting to bind on address ('0.0.0.0', 8765): address already in use"
    $log = Set-TestFile $d 'server.log' (($lines -join "`n") + "`n")
    $tail = @(Get-JarvisLogTail -Path $log -Lines 5)
    Assert-Equal 5 $tail.Count 'fuenf Zeilen'
    Assert-True ($tail[-1] -like '*Errno 98*') 'letzte Zeile'
    Assert-Equal 0 @(Get-JarvisLogTail -Path (Join-Path $d 'fehlt.log')).Count 'fehlende Datei'
    Assert-True ((Get-JarvisStartProblem -LogLines $tail -Port 8765) -like '*Port 8765 ist belegt*anderen Port*') 'Linux: Port belegt'
    $win = "ERROR: [Errno 10048] error while attempting to bind on address ('0.0.0.0', 8765): only one usage of each socket address (protocol/network address/port) is normally permitted"
    Assert-True ((Get-JarvisStartProblem -LogLines @($win) -Port 9000) -like '*Port 9000 ist belegt*') 'Windows: Port belegt'
    Assert-True ((Get-JarvisStartProblem -LogLines @('JARVIS kann nicht starten:', 'config.yaml ist ungueltig') -Port 1) -like '*config.yaml*') 'Config-Fehler'
    Assert-True ((Get-JarvisStartProblem -LogLines @(('JARVIS l' + [char]0x00E4 + 'uft bereits (PID 42, gestartet x).')) -Port 1) -like '*laeuft bereits*') 'zweite Instanz'
    Assert-True ($null -eq (Get-JarvisStartProblem -LogLines @('irgendwas') -Port 1)) 'unbekannt -> nichts'
}

Invoke-Test 'Anderer laufender JARVIS-Server: Ordner wird genannt (nur lesen)' {
    $t = New-TestDir 'target'
    $other = 'C:\Users\Max\Repo\jarvis'
    $now = [DateTime]::UtcNow
    $script:procs = @(
        [pscustomobject]@{ Id = 101; Name = 'pythonw.exe'; Path = 'C:\Python312\pythonw.exe'; CommandLine = '"C:\Python312\pythonw.exe" -m app'; ParentPath = ($other + '\.venv\Scripts\pythonw.exe'); StartTimeUtc = $now },
        [pscustomobject]@{ Id = 102; Name = 'python'; Path = (Join-Path $t '.venv/bin/python'); CommandLine = ((Join-Path $t '.venv/bin/python') + ' -m app'); ParentPath = $null; StartTimeUtc = $now },
        [pscustomobject]@{ Id = 103; Name = 'python'; Path = '/usr/bin/python3'; CommandLine = '/usr/bin/python3 -m pip list'; ParentPath = $null; StartTimeUtc = $now })
    function Get-JarvisPythonProcessDetails { return $script:procs }
    $found = @(Get-JarvisOtherServers -Target $t)
    Assert-Equal 1 $found.Count 'nur der fremde Server (eigener und Nicht-Server ausgenommen)'
    Assert-Equal 101 $found[0].Id 'PID'
    Assert-Equal $other $found[0].Folder 'Ordner ueber dem .venv-Starter'
}

Invoke-Test 'Rechte einschraenken nur unter Windows' {
    $d = New-TestDir 'acl'
    Assert-False (Set-JarvisOwnerOnlyAcl -Path $d) 'ohne Windows nichts tun'
    Assert-False (Set-JarvisOwnerOnlyAcl -Path (Join-Path $d 'fehlt')) 'fehlender Pfad'
}

Invoke-Test 'Sicherer Arbeitsordner liegt nie im Ziel' {
    $t = New-TestDir 'target'
    $safe = Get-JarvisSafeWorkingDir -Target $t
    Assert-True ($null -ne $safe) 'gefunden'
    Assert-False (Test-JarvisPathUnder -Path $safe -Root $t -AllowEqual) 'ausserhalb des Ziels'
}

Complete-Tests
