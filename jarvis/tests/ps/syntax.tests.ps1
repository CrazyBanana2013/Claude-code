# Prueft ALLE .ps1- und .cmd-Dateien im Projekt:
# - reines ASCII ohne BOM (Windows PowerShell 5.1 liest UTF-8 ohne BOM als ANSI),
# - .ps1 ohne Parserfehler und ohne Syntax, die es erst ab PowerShell 7 gibt,
# - keine Parameter/Variablen, die Windows PowerShell 5.1 nicht kennt,
# - Invoke-WebRequest immer mit -UseBasicParsing,
# - .cmd-Wrapper rufen powershell.exe -NoProfile -ExecutionPolicy Bypass -File ... auf.
. (Join-Path $PSScriptRoot 'TestHelpers.ps1')

$skipDirs = @('.venv', 'node_modules', '.git', 'dist', '__pycache__', '.pytest_cache', 'state')

function Get-ProjectFiles {
    param([string[]]$Extensions)
    $result = New-Object System.Collections.Generic.List[string]
    $pending = New-Object System.Collections.Generic.Queue[string]
    $pending.Enqueue($script:JarvisRoot)
    while ($pending.Count -gt 0) {
        $dir = $pending.Dequeue()
        foreach ($entry in (New-Object System.IO.DirectoryInfo $dir).GetFileSystemInfos()) {
            if ($entry -is [System.IO.DirectoryInfo]) {
                if ($skipDirs -notcontains $entry.Name) { $pending.Enqueue($entry.FullName) }
            } elseif ($Extensions -contains $entry.Extension.ToLowerInvariant()) {
                $result.Add($entry.FullName)
            }
        }
    }
    return $result.ToArray()
}

function Get-RelName { param([string]$Path) return $Path.Substring($script:JarvisRoot.Length + 1) }

$psFiles = @(Get-ProjectFiles @('.ps1'))
$cmdFiles = @(Get-ProjectFiles @('.cmd'))

Invoke-Test 'Dateien gefunden' {
    foreach ($name in @('scripts/install.ps1', 'scripts/uninstall.ps1', 'scripts/installer-lib.ps1', 'scripts/stop.ps1',
            'scripts/install_autostart.ps1', 'scripts/start.ps1')) {
        Assert-True (@($psFiles | Where-Object { (Get-RelName $_) -replace '\\', '/' -eq $name }).Count -eq 1) ('fehlt: ' + $name)
    }
    Assert-Equal 2 $cmdFiles.Count 'Install.cmd und Uninstall.cmd'
}

foreach ($file in @($psFiles + $cmdFiles)) {
    $rel = Get-RelName $file
    Invoke-Test ('ASCII ohne BOM: ' + $rel) {
        $bytes = [System.IO.File]::ReadAllBytes($file)
        $bad = @()
        for ($i = 0; $i -lt $bytes.Length; $i++) {
            if ($bytes[$i] -gt 127) { $bad += $i; if ($bad.Count -ge 3) { break } }
        }
        if ($bad.Count -gt 0) {
            $text = [System.Text.Encoding]::UTF8.GetString($bytes)
            $line = ($text.Substring(0, [Math]::Min($text.Length, $bad[0])) -split "`n").Count
            throw ('Nicht-ASCII-Zeichen ab Byte {0} (etwa Zeile {1})' -f $bad[0], $line)
        }
        foreach ($b in $bytes) { if ($b -eq 0) { throw 'NUL-Byte (UTF-16?)' } }
    }
}

$forbiddenTokens = @('AndAnd', 'OrOr', 'QuestionQuestion', 'QuestionQuestionEquals', 'QuestionDot', 'QuestionLBracket', 'QuestionMark')
$forbiddenVariables = @('IsWindows', 'IsLinux', 'IsMacOS', 'IsCoreCLR')
# Parameter, die es in Windows PowerShell 5.1 nicht gibt (Cmdlet => Parameter).
$forbiddenParams = @{
    '*'                 = @('Parallel', 'AdditionalChildPath', 'SkipCertificateCheck', 'NoProxy', 'AsByteStream', 'SkipHttpErrorCheck')
    'ConvertFrom-Json'  = @('AsHashtable', 'Depth', 'NoEnumerate')
    'ConvertTo-Json'    = @('AsArray', 'EnumsAsStrings', 'EscapeHandling')
    'Start-Process'     = @('Environment')
    'Get-Content'       = @('AsByteStream')
    'Set-Content'       = @('AsByteStream')
    'Invoke-WebRequest' = @('Authentication', 'Resume', 'SslProtocol')
    'Invoke-RestMethod' = @('Authentication', 'Resume', 'SslProtocol', 'StatusCodeVariable')
}
# Zusammengesetzt, damit diese Datei sich nicht selbst meldet.
$forbiddenText = @(('utf8' + 'NoBOM'), ('GetRelative' + 'Path'), ('$' + 'PSStyle'))

function Get-AllTokens {
    param($Tokens)
    foreach ($t in $Tokens) {
        $t
        if ($t -is [System.Management.Automation.Language.StringExpandableToken] -and $t.NestedTokens) {
            Get-AllTokens $t.NestedTokens
        }
    }
}

function Get-CompatProblems {
    # Liefert alle Stellen, die unter Windows PowerShell 5.1 nicht funktionieren wuerden.
    param([string]$File)
    $problems = New-Object System.Collections.Generic.List[string]
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($File, [ref]$tokens, [ref]$errors)
    foreach ($e in $errors) { $problems.Add(('Parserfehler Zeile {0}: {1}' -f $e.Extent.StartLineNumber, $e.Message)) }
    foreach ($t in @(Get-AllTokens $tokens)) {
        if ($forbiddenTokens -contains $t.Kind.ToString()) {
            $problems.Add(('PS7-Operator {0} in Zeile {1}: {2}' -f $t.Kind, $t.Extent.StartLineNumber, $t.Text))
        }
        if ($t -is [System.Management.Automation.Language.VariableToken] -and $forbiddenVariables -contains $t.Name) {
            $problems.Add(('${0} gibt es in PS 5.1 nicht (Zeile {1})' -f $t.Name, $t.Extent.StartLineNumber))
        }
    }
    $text = [System.IO.File]::ReadAllText($File)
    foreach ($needle in $forbiddenText) {
        if ($text.Contains($needle)) { $problems.Add(('nicht PS-5.1-tauglich: ' + $needle)) }
    }
    $commands = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)
    foreach ($c in $commands) {
        $name = $c.GetCommandName()
        $line = $c.Extent.StartLineNumber
        $params = @($c.CommandElements | Where-Object { $_ -is [System.Management.Automation.Language.CommandParameterAst] } | ForEach-Object { $_.ParameterName })
        $bad = @($forbiddenParams['*'])
        if ($name -and $forbiddenParams.ContainsKey($name)) { $bad += $forbiddenParams[$name] }
        foreach ($p in $params) {
            if ($bad -contains $p) { $problems.Add(('-{0} bei {1} gibt es in PS 5.1 nicht (Zeile {2})' -f $p, $name, $line)) }
        }
        if ($name -eq 'Join-Path') {
            $positional = @($c.CommandElements | Select-Object -Skip 1 | Where-Object { $_ -isnot [System.Management.Automation.Language.CommandParameterAst] })
            if ($positional.Count -gt 2) { $problems.Add(('Join-Path mit mehr als 2 Pfadteilen (PS 7) in Zeile {0}' -f $line)) }
        }
        if (@('Invoke-WebRequest', 'iwr', 'Invoke-RestMethod', 'irm') -contains $name) {
            $splatted = @($c.CommandElements | Where-Object { $_ -is [System.Management.Automation.Language.VariableExpressionAst] -and $_.Splatted }).Count -gt 0
            $ok = ($params -contains 'UseBasicParsing') -or ($splatted -and $text -match 'UseBasicParsing\s*=\s*\$true')
            if (-not $ok) { $problems.Add(('{0} ohne -UseBasicParsing in Zeile {1}' -f $name, $line)) }
        }
        if (@('?', 'where', '%', 'foreach') -contains $name) {
            $problems.Add(('Alias {0} statt ausgeschriebenem Cmdlet in Zeile {1}' -f $name, $line))
        }
    }
    return $problems.ToArray()
}

foreach ($file in $psFiles) {
    $rel = Get-RelName $file
    Invoke-Test ('PS 5.1-tauglich: ' + $rel) {
        $problems = @(Get-CompatProblems $file)
        if ($problems.Count -gt 0) { throw ($problems -join '; ') }
    }
}

Invoke-Test 'Pruefung erkennt PS7-Syntax wirklich (Negativtest)' {
    $dir = New-TestDir 'bad'
    $q = [string][char]63
    $amp = [string][char]38
    $bar = [string][char]124
    $samples = [ordered]@{
        'QuestionQuestion'       = ('$a = $b {0}{0} 1' -f $q)
        'QuestionQuestionEquals' = ('$a {0}{0}= 1' -f $q)
        'QuestionDot'            = ('$x = ${{a}}{0}.Name' -f $q)
        'QuestionLBracket'       = ('$x = ${{a}}{0}[0]' -f $q)
        'QuestionMark'           = ('$x = $true {0} 1 : 2' -f $q)
        'AndAnd'                 = ('Get-Date {0}{0} Get-Date' -f $amp)
        'OrOr'                   = ('Get-Date {0}{0} Get-Date' -f $bar)
        'IsWindows'              = 'if ($IsWindows) { 1 }'
        'Parallel'               = '1..2 | ForEach-Object -Parallel { $_ }'
        'AsHashtable'            = '$x = "{}" | ConvertFrom-Json -AsHashtable'
        'Join-Path mit mehr'     = '$x = Join-Path a b c'
        'UseBasicParsing'        = 'Invoke-WebRequest -Uri http://127.0.0.1/'
        'Alias'                  = '1 | % { $_ }'
        'NestedQuestionQuestion' = ('$s = "wert: $($a {0}{0} 1)"' -f $q)
    }
    foreach ($key in $samples.Keys) {
        $file = Join-Path $dir 'bad.ps1'
        [System.IO.File]::WriteAllText($file, $samples[$key] + "`n")
        $problems = @(Get-CompatProblems $file)
        $expect = $key -replace '^Nested', ''
        Assert-True (@($problems | Where-Object { $_ -like ('*' + $expect + '*') }).Count -gt 0) ('nicht erkannt: {0} -> [{1}]' -f $key, ($problems -join '; '))
    }
    [System.IO.File]::WriteAllText((Join-Path $dir 'gut.ps1'), "`$x = if (`$a) { 1 } else { 2 }`nInvoke-WebRequest -Uri http://127.0.0.1/ -UseBasicParsing`n")
    Assert-Equal 0 @(Get-CompatProblems (Join-Path $dir 'gut.ps1')).Count 'gueltiger PS-5.1-Code ohne Meldung'
}

Invoke-Test 'Install.cmd / Uninstall.cmd: Aufruf, Pause, Exitcode' {
    $install = [System.IO.File]::ReadAllText((Join-Path $script:JarvisRoot 'Install.cmd'))
    Assert-True ($install.Contains('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*')) 'install.ps1-Aufruf'
    Assert-True ($install.Contains('if not "%JARVIS_NOPAUSE%"=="1" pause')) 'Pause abschaltbar'
    Assert-True ($install.Contains('set "JARVIS_RC=%ERRORLEVEL%"') -and $install.Contains('exit /b %JARVIS_RC%')) 'Exitcode weiterreichen'
    $uninstall = [System.IO.File]::ReadAllText((Join-Path $script:JarvisRoot 'Uninstall.cmd'))
    Assert-True ($uninstall.Contains('set "JARVIS_UNINSTALL_PS1=%~dp0scripts\uninstall.ps1"')) 'uninstall.ps1-Pfad'
    Assert-True ($uninstall.Contains('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "!JARVIS_UNINSTALL_PS1!" !JARVIS_ARGS!')) 'uninstall.ps1-Aufruf'
    # %* nur merken, solange DelayedExpansion aus ist (sonst verschluckt cmd ein "!" in den Argumenten).
    $capture = $uninstall.IndexOf('set JARVIS_ARGS=%*')
    $enable = $uninstall.IndexOf('setlocal EnableDelayedExpansion')
    Assert-True ($capture -gt 0 -and $capture -lt $enable) 'Argumente vor EnableDelayedExpansion gemerkt'
    $afterEnable = @($uninstall.Substring($enable) -split "`r?`n" | Where-Object { $_.Trim() -notlike 'rem *' })
    Assert-Equal 0 @($afterEnable | Where-Object { $_.Contains('%*') }).Count 'kein %* nach EnableDelayedExpansion'
    Assert-True ($uninstall.Contains('cd /d "%TEMP%"')) 'verlaesst den Installationsordner'
    Assert-True ($uninstall.Contains('exit /b !JARVIS_RC!')) 'Exitcode weiterreichen'
    # Nach der oeffnenden Klammer darf nichts mehr ausserhalb des Blocks folgen (Datei wird geloescht).
    $afterBlock = $uninstall.Substring($uninstall.LastIndexOf(')') + 1).Trim()
    Assert-Equal '' $afterBlock 'nichts nach dem Block'
    foreach ($text in @($install, $uninstall)) {
        Assert-True ($text.TrimStart().StartsWith('@echo off')) '@echo off'
        Assert-False ($text -match '(?m)^\s*:') 'keine Sprungmarken (robust auch bei LF-Zeilenenden)'
    }
}

Invoke-Test 'Keine Aufrufe, die System-/Firewall-Einstellungen aendern (nur als Text angezeigt)' {
    $forbidden = @('New-NetFirewallRule', 'Remove-NetFirewallRule', 'Set-NetFirewallRule', 'Set-NetFirewallProfile',
        'Enable-NetFirewallRule', 'Disable-NetFirewallRule', 'Set-NetConnectionProfile', 'Register-ScheduledTask',
        'Set-ItemProperty', 'New-ItemProperty', 'Set-ExecutionPolicy', 'netsh', 'schtasks', 'reg', 'Set-Acl')
    foreach ($file in @($psFiles | Where-Object { (Get-RelName $_) -replace '\\', '/' -notlike 'tests/*' })) {
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$errors)
        foreach ($c in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
            $name = $c.GetCommandName()
            if ($name -and ($forbidden -contains $name -or $forbidden -contains ($name -replace '\.exe$', ''))) {
                throw ('{0}: Aufruf von {1} in Zeile {2}' -f (Get-RelName $file), $name, $c.Extent.StartLineNumber)
            }
        }
    }
}

Invoke-Test '.gitattributes: CRLF fuer .ps1 und .cmd' {
    $attr = [System.IO.File]::ReadAllText((Join-Path $script:JarvisRoot '.gitattributes'))
    Assert-True ($attr -match '(?m)^\*\.ps1\s+.*eol=crlf') '*.ps1 eol=crlf'
    Assert-True ($attr -match '(?m)^\*\.cmd\s+.*eol=crlf') '*.cmd eol=crlf'
}

Complete-Tests
