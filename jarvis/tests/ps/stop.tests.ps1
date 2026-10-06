# Echte Prozesse: findet und beendet stop.ps1 nur den Server der eigenen Installation?
# Fake-Installation = Ordner mit .venv/bin/python (Link auf python3) und app/__main__.py (schlaeft).
. (Join-Path $PSScriptRoot 'TestHelpers.ps1')

if (Test-JarvisWindows) { Skip-AllTests 'Fake-venv mit Symlinks nur unter Linux/macOS' }
$python3 = Get-Command -Name 'python3' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $python3) { Skip-AllTests 'python3 fehlt' }

function New-FakeInstall {
    param([string]$Name)
    $t = New-TestDir $Name
    [void][System.IO.Directory]::CreateDirectory((Join-Path $t '.venv/bin'))
    [void](New-Item -ItemType SymbolicLink -Path (Join-Path $t '.venv/bin/python') -Target $python3.Path)
    [void](Set-TestFile $t 'app/__init__.py' '')
    [void](Set-TestFile $t 'app/__main__.py' "import time`ntime.sleep(120)`n")
    return $t
}

$script:started = New-Object System.Collections.Generic.List[object]
function Start-Fake {
    param([string]$Target, [string[]]$Arguments)
    $logs = New-TestDir 'logs'
    $p = Start-Process -FilePath (Join-Path $Target '.venv/bin/python') -ArgumentList (ConvertTo-JarvisArgString $Arguments) `
        -WorkingDirectory $Target -PassThru -RedirectStandardOutput (Join-Path $logs 'out.log') -RedirectStandardError (Join-Path $logs 'err.log')
    $script:started.Add($p)
    return $p
}

function Wait-Exec {
    # Wartet, bis die Prozesse ihre Kommandozeile haben (nach exec).
    param($Processes)
    $deadline = (Get-Date).AddSeconds(10)
    foreach ($p in $Processes) {
        while ((Get-Date) -lt $deadline) {
            $d = Get-JarvisProcessDetails -Id $p.Id
            if ($d -and $d.CommandLine -match 'python') { break }
            Start-Sleep -Milliseconds 100
        }
    }
}

try {
    $mine = New-FakeInstall 'mine'
    $other = New-FakeInstall 'other'
    $server = Start-Fake $mine @('-m', 'app')
    $decoyOther = Start-Fake $other @('-m', 'app')
    $decoyCmd = Start-Fake $mine @('-c', 'import time; time.sleep(120)')
    Wait-Exec @($server, $decoyOther, $decoyCmd)

    Invoke-Test 'Erkennung ohne PID-Datei: nur der eigene Server' {
        $found = @(Find-JarvisServerProcesses -Target $mine)
        Assert-Equal @($server.Id) @($found | ForEach-Object { $_.Id }) 'gefundene PIDs'
        Assert-Equal @($decoyOther.Id) @(Find-JarvisServerProcesses -Target $other | ForEach-Object { $_.Id }) 'andere Installation'
    }

    Invoke-Test 'PID-Datei mit fremder PID und alter Startzeit wird ignoriert' {
        $old = [DateTime]::UtcNow.AddHours(-3).ToString('yyyy-MM-ddTHH:mm:ss') + '+00:00'
        [void](Set-TestFile $mine 'state/server.pid' ('{{"pid": {0}, "executable": "{1}", "started": "{2}"}}' -f $decoyOther.Id, (Join-Path $mine '.venv/bin/python'), $old))
        Assert-Equal @($server.Id) @(Find-JarvisServerProcesses -Target $mine | ForEach-Object { $_.Id }) 'gefundene PIDs'
    }

    Invoke-Test 'PID-Datei des eigenen Servers: kein Doppeltreffer' {
        $now = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss') + '+00:00'
        [void](Set-TestFile $mine 'state/server.pid' ('{{"pid": {0}, "executable": "{1}", "started": "{2}"}}' -f $server.Id, (Join-Path $mine '.venv/bin/python'), $now))
        Assert-Equal @($server.Id) @(Find-JarvisServerProcesses -Target $mine | ForEach-Object { $_.Id }) 'gefundene PIDs'
    }

    Invoke-Test 'stop.ps1 beendet nur den eigenen Server' {
        $r = Invoke-PwshScript -Script (Join-Path $script:ScriptsDir 'stop.ps1') -Arguments @('-Target', $mine)
        Assert-Equal 0 $r.ExitCode ('Exitcode: ' + $r.Output)
        Assert-True ($r.Output -match 'JARVIS beendet') ('Ausgabe: ' + $r.Output)
        Assert-True ($server.WaitForExit(5000)) 'Server beendet'
        Assert-False $decoyOther.HasExited 'Server der anderen Installation laeuft weiter'
        Assert-False $decoyCmd.HasExited 'anderer Prozess aus derselben venv laeuft weiter'
        Assert-True ([System.IO.File]::Exists((Join-Path $mine 'state/server.pid'))) 'state/ wird nicht angefasst'
        $r = Invoke-PwshScript -Script (Join-Path $script:ScriptsDir 'stop.ps1') -Arguments @('-Target', $mine)
        Assert-Equal 0 $r.ExitCode 'zweiter Aufruf'
        Assert-True ($r.Output -match 'JARVIS laeuft nicht') ('Ausgabe: ' + $r.Output)
    }
} finally {
    foreach ($p in $script:started) { try { if (-not $p.HasExited) { $p.Kill() } } catch { } }
}

Complete-Tests
