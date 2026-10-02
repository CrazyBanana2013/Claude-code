# Mini-Testrahmen fuer die pwsh-Tests der Installer-Skripte (ohne Pester).
# Jede *.tests.ps1 dot-sourct diese Datei und scripts/installer-lib.ps1, ruft Invoke-Test auf und
# endet mit Complete-Tests (Exitcode = Zahl der Fehlschlaege; 77 = uebersprungen).
#
# Windows-spezifische Funktionen der Bibliothek werden in einem Test einfach neu definiert
# (z. B. function Test-JarvisWindows { $true }). PowerShell sucht Funktionen dynamisch ueber die
# Aufrufkette, daher sehen die Bibliotheksfunktionen die Ersatzfunktion - nur in diesem Test.

$ErrorActionPreference = 'Stop'
$script:JarvisRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..' | Join-Path -ChildPath '..'))
$script:ScriptsDir = Join-Path $script:JarvisRoot 'scripts'
$script:TestCount = 0
$script:TestFailures = 0
$script:TestBase = Join-Path ([System.IO.Path]::GetTempPath()) ('jarvis-ps-tests-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
[void][System.IO.Directory]::CreateDirectory($script:TestBase)

. (Join-Path $script:ScriptsDir 'installer-lib.ps1')

function Invoke-Test {
    param([string]$Name, [scriptblock]$Body)
    $script:TestCount++
    try {
        & $Body
        Write-Host ('PASS  ' + $Name)
    } catch {
        $script:TestFailures++
        $line = ''
        if ($_.InvocationInfo) { $line = ' (Zeile {0})' -f $_.InvocationInfo.ScriptLineNumber }
        Write-Host ('FAIL  {0}: {1}{2}' -f $Name, $_.Exception.Message, $line)
    }
}

function Complete-Tests {
    try { Remove-JarvisTree -Path $script:TestBase -Root ([System.IO.Path]::GetTempPath()) } catch {
        Write-Host ('Hinweis: Testordner nicht entfernt: ' + $_.Exception.Message)
    }
    Write-Host ('{0} Tests, {1} fehlgeschlagen' -f $script:TestCount, $script:TestFailures)
    exit $script:TestFailures
}

function Skip-AllTests {
    param([string]$Reason)
    Write-Host ('SKIP: ' + $Reason)
    try { Remove-JarvisTree -Path $script:TestBase -Root ([System.IO.Path]::GetTempPath()) } catch { }
    exit 77
}

function Assert-True {
    param($Condition, [string]$Message = 'Bedingung ist falsch')
    if (-not $Condition) { throw $Message }
}

function Assert-False {
    param($Condition, [string]$Message = 'Bedingung ist wahr')
    if ($Condition) { throw $Message }
}

function Assert-Equal {
    param($Expected, $Actual, [string]$Message = 'Werte verschieden')
    if (($Expected -is [System.Array]) -or ($Actual -is [System.Array])) {
        $e = (@($Expected) | ForEach-Object { [string]$_ }) -join '|'
        $a = (@($Actual) | ForEach-Object { [string]$_ }) -join '|'
        if ($e -cne $a) { throw ('{0}: erwartet <{1}>, erhalten <{2}>' -f $Message, $e, $a) }
        return
    }
    if ([string]$Expected -cne [string]$Actual) { throw ('{0}: erwartet <{1}>, erhalten <{2}>' -f $Message, $Expected, $Actual) }
}

function Assert-Throws {
    param([scriptblock]$Body, [string]$Like = '*')
    $thrown = $false
    try { & $Body } catch {
        $thrown = $true
        if ($_.Exception.Message -notlike $Like) { throw ('Falsche Fehlermeldung: ' + $_.Exception.Message) }
    }
    if (-not $thrown) { throw 'Es wurde kein Fehler ausgeloest.' }
}

function New-TestDir {
    param([string]$Name = 'd')
    $dir = Join-Path $script:TestBase ($Name + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6))
    [void][System.IO.Directory]::CreateDirectory($dir)
    return $dir
}

function Set-TestFile {
    # Legt eine Datei (samt Ordnern) mit Inhalt an. RelPath mit '/'.
    param([string]$Root, [string]$RelPath, [string]$Content = 'x')
    $path = Join-JarvisRelPath $Root $RelPath
    [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($path))
    [System.IO.File]::WriteAllText($path, $Content)
    return $path
}

function Get-TestFile {
    param([string]$Root, [string]$RelPath)
    return [System.IO.File]::ReadAllText((Join-JarvisRelPath $Root $RelPath))
}

function Test-TestPath {
    param([string]$Root, [string]$RelPath)
    $p = Join-JarvisRelPath $Root $RelPath
    return ([System.IO.File]::Exists($p) -or [System.IO.Directory]::Exists($p))
}

function Get-PwshPath {
    return (Get-Process -Id $PID).Path
}

function Invoke-PwshScript {
    # Startet ein Skript in einem eigenen pwsh-Prozess. Liefert ExitCode und Output (stdout+stderr).
    param([string]$Script, [string[]]$Arguments = @(), [string]$WorkingDirectory, [hashtable]$Environment,
        [string]$InputText, [int]$TimeoutSec = 600)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = Get-PwshPath
    $psi.Arguments = ConvertTo-JarvisArgString (@('-NoProfile', '-File', $Script) + $Arguments)
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $true
    if ($WorkingDirectory) { $psi.WorkingDirectory = $WorkingDirectory }
    foreach ($name in @('JARVIS_CONFIG', 'JARVIS_SECRETS', 'JARVIS_STATE', 'JARVIS_NOPAUSE')) {
        if ($psi.EnvironmentVariables.ContainsKey($name)) { $psi.EnvironmentVariables.Remove($name) }
    }
    if ($Environment) { foreach ($k in $Environment.Keys) { $psi.EnvironmentVariables[[string]$k] = [string]$Environment[$k] } }
    $proc = [System.Diagnostics.Process]::Start($psi)
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    if ($InputText) { $proc.StandardInput.Write($InputText) }
    $proc.StandardInput.Close()
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        try { $proc.Kill() } catch { }
        throw ('Zeitueberschreitung: ' + $Script)
    }
    $proc.WaitForExit()
    $output = $outTask.Result + $errTask.Result
    return [pscustomobject]@{ ExitCode = $proc.ExitCode; Output = $output }
}

function Write-TestOutput {
    param([string]$Title, [string]$Text)
    Write-Host ('----- {0} -----' -f $Title)
    Write-Host $Text
    Write-Host '-----'
}
