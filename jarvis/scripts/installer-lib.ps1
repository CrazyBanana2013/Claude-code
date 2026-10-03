# installer-lib.ps1 - gemeinsame Funktionen fuer install.ps1, uninstall.ps1, stop.ps1 und
# install_autostart.ps1. Wird per Dot-Sourcing geladen:
#     . (Join-Path $PSScriptRoot 'installer-lib.ps1')
#
# Regeln fuer alle Skripte in diesem Ordner:
# - Reines ASCII (Windows PowerShell 5.1 liest UTF-8 ohne BOM als ANSI). Umlaute, die wirklich
#   sichtbar sein muessen (Verknuepfungsnamen), werden mit [char]0x00F6 usw. gebaut.
# - Lauffaehig unter Windows PowerShell 5.1 und PowerShell 7: keine Operatoren wie
#   Null-Koaleszenz, Null-Bedingung, Ternaer oder Pipeline-Ketten; Invoke-WebRequest immer mit
#   -UseBasicParsing.
# - Keine Adminrechte, keine Aenderung an Systemeinstellungen, Firewall oder Aufgabenplanung.
#   Firewall-Befehle werden nur angezeigt.
# - Geloescht wird nur, was nachweislich zur Installation gehoert, und nie ausserhalb des
#   Installationsordners (Ausnahme: die eigenen Verknuepfungen im Startmenue/Autostart).
# - Windows-spezifische Schritte stecken in eigenen Funktionen (Test-JarvisWindows,
#   New-JarvisShortcut, Get-JarvisStartMenuDir, ...). Die Tests unter tests/ps ersetzen sie
#   durch Neudefinition, damit die Logik auch unter pwsh auf Linux pruefbar ist.

# ---------------------------------------------------------------------------------------------
# Grundlagen: Plattform, Ausgabe, Rueckfragen
# ---------------------------------------------------------------------------------------------

function Test-JarvisWindows {
    return ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT)
}

function Test-JarvisElevated {
    # True, wenn der Prozess mit Administratorrechten (erhoeht) laeuft.
    if (-not (Test-JarvisWindows)) { return $false }
    try {
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
        return [bool]$principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Write-JarvisStep {
    param([int]$Number, [int]$Total, [string]$Title)
    Write-Host ''
    Write-Host ('[{0}/{1}] {2}' -f $Number, $Total, $Title) -ForegroundColor Cyan
}

function Write-JarvisInfo {
    param([string]$Message)
    Write-Host ('      ' + $Message)
}

function Write-JarvisOk {
    param([string]$Message)
    Write-Host ('      OK: ' + $Message) -ForegroundColor Green
}

function Write-JarvisWarn {
    param([string]$Message)
    Write-Host ('      WARNUNG: ' + $Message) -ForegroundColor Yellow
}

function Write-JarvisErr {
    param([string]$Message)
    Write-Host ('FEHLER: ' + $Message) -ForegroundColor Red
}

function Get-JarvisErrorMessage {
    # Lesbare Fehlermeldung: .NET-Aufrufe liefern sonst 'Exception calling "Copy" with "3" argument(s): ...'.
    param($ErrorRecord)
    $ex = $ErrorRecord
    if ($ErrorRecord -is [System.Management.Automation.ErrorRecord]) { $ex = $ErrorRecord.Exception }
    while ($ex -is [System.Management.Automation.MethodInvocationException] -and $ex.InnerException) { $ex = $ex.InnerException }
    if ($null -eq $ex) { return 'Unbekannter Fehler' }
    return [string]$ex.Message
}

function Stop-JarvisAbort {
    # Bricht ab, weil der Benutzer es so wollte (Exitcode 1 in install/uninstall).
    param([string]$Message)
    throw (New-Object System.OperationCanceledException $Message)
}

function Read-JarvisAnswer {
    # Einzige Stelle, die von der Konsole liest (Tests ersetzen diese Funktion).
    param([string]$Prompt)
    return (Read-Host -Prompt $Prompt)
}

function Read-JarvisYesNo {
    # Ja/Nein-Frage. -AssumeDefault (z. B. bei -Yes) nimmt ohne Rueckfrage den Standardwert.
    param([string]$Prompt, [bool]$Default = $false, [switch]$AssumeDefault)
    $hint = '(j/N)'
    if ($Default) { $hint = '(J/n)' }
    if ($AssumeDefault) {
        $word = 'nein'
        if ($Default) { $word = 'ja' }
        Write-Host ('      {0} {1} -> {2} (automatisch)' -f $Prompt, $hint, $word)
        return $Default
    }
    for ($i = 0; $i -lt 5; $i++) {
        $answer = $null
        try {
            $answer = Read-JarvisAnswer ('      {0} {1}' -f $Prompt, $hint)
        } catch {
            return $Default
        }
        if ($null -eq $answer) { return $Default }
        $answer = ([string]$answer).Trim().ToLowerInvariant()
        if ($answer -eq '') { return $Default }
        if (@('j', 'ja', 'y', 'yes') -contains $answer) { return $true }
        if (@('n', 'nein', 'no') -contains $answer) { return $false }
        Write-Host '      Bitte j (ja) oder n (nein) eingeben.'
    }
    return $Default
}

function Assert-JarvisPlatform {
    param([switch]$AllowNonWindows)
    if (Test-JarvisWindows) { return }
    if ($AllowNonWindows) {
        Write-JarvisWarn 'Testmodus (-AllowNonWindows): kein Windows. Verknuepfungen, Autostart und Admin-Pruefung entfallen.'
        return
    }
    throw 'Dieses Skript ist fuer Windows gedacht. (Nur fuer Tests: -AllowNonWindows)'
}

function Assert-JarvisNotElevated {
    param([switch]$AllowAdmin)
    if (-not (Test-JarvisElevated)) { return }
    $why = 'Dieses Fenster laeuft mit Administratorrechten. JARVIS soll unter deinem normalen ' +
        'Benutzerkonto laufen: Sonst landen Verknuepfungen und Autostart beim falschen Konto und ' +
        'der Server haette unnoetig hohe Rechte.'
    if ($AllowAdmin) {
        Write-JarvisWarn ($why + ' Du hast -AllowAdmin angegeben - es geht trotzdem weiter.')
        return
    }
    throw ($why + ' Bitte Install.cmd bzw. Uninstall.cmd normal per Doppelklick starten ' +
        '(nicht "Als Administrator ausfuehren"). Wer es trotzdem will: Option -AllowAdmin.')
}

# ---------------------------------------------------------------------------------------------
# Pfade und Sicherheitspruefungen
# ---------------------------------------------------------------------------------------------

function Get-JarvisHomeDir {
    if ((Test-JarvisWindows) -and $env:USERPROFILE) { return $env:USERPROFILE }
    if ($env:HOME) { return $env:HOME }
    return $env:USERPROFILE
}

function Get-JarvisFullPath {
    # Absoluter, normalisierter Pfad ohne abschliessenden Trenner (ausser beim Stammverzeichnis).
    param([Parameter(Mandatory = $true)][string]$Path)
    if ($Path -eq '~' -or $Path.StartsWith('~/') -or $Path.StartsWith('~\')) {
        $Path = (Get-JarvisHomeDir) + $Path.Substring(1)
    }
    if (-not [System.IO.Path]::IsPathRooted($Path)) {
        $Path = Join-Path (Get-Location -PSProvider FileSystem).ProviderPath $Path
    }
    $full = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetPathRoot($full)
    if ($full.Length -gt $root.Length) {
        $full = $full.TrimEnd([char[]]@([char]'\', [char]'/'))
    }
    return $full
}

function Get-JarvisPathComparison {
    if (Test-JarvisWindows) { return [System.StringComparison]::OrdinalIgnoreCase }
    return [System.StringComparison]::Ordinal
}

function Test-JarvisSamePath {
    param([string]$A, [string]$B)
    if (-not $A -or -not $B) { return $false }
    return [string]::Equals((Get-JarvisFullPath $A), (Get-JarvisFullPath $B), (Get-JarvisPathComparison))
}

function Test-JarvisPathUnder {
    # True, wenn $Path innerhalb von $Root liegt (gleich zaehlt nur mit -AllowEqual).
    param([string]$Path, [string]$Root, [switch]$AllowEqual)
    if (-not $Path -or -not $Root) { return $false }
    try {
        $p = Get-JarvisFullPath $Path
        $r = Get-JarvisFullPath $Root
    } catch {
        return $false
    }
    $cmp = Get-JarvisPathComparison
    if ([string]::Equals($p, $r, $cmp)) { return [bool]$AllowEqual }
    $prefix = $r
    if (-not ($prefix.EndsWith('\') -or $prefix.EndsWith('/'))) {
        $prefix = $prefix + [System.IO.Path]::DirectorySeparatorChar
    }
    return $p.StartsWith($prefix, $cmp)
}

function Join-JarvisRelPath {
    # Verbindet einen Ordner mit einem relativen Pfad im Manifest-Format (Trenner '/').
    param([string]$Root, [string]$RelPath)
    $native = $RelPath -replace '[\\/]', [string][System.IO.Path]::DirectorySeparatorChar
    return [System.IO.Path]::Combine($Root, $native)
}

function Test-JarvisRelPathValid {
    # Nur einfache relative Pfade ohne '..', ohne Laufwerk und ohne fuehrenden Trenner.
    param([string]$RelPath)
    if ([string]::IsNullOrWhiteSpace($RelPath)) { return $false }
    if ($RelPath -match '^[A-Za-z]:' -or $RelPath.StartsWith('/') -or $RelPath.StartsWith('\')) { return $false }
    # Zeichen, die unter Windows in Dateinamen verboten sind (.NET Framework wirft sonst bei Path.Combine).
    if ($RelPath -match '[\x00-\x1f:*?"<>|]') { return $false }
    try {
        if ([System.IO.Path]::IsPathRooted($RelPath)) { return $false }
    } catch {
        return $false
    }
    foreach ($segment in ($RelPath -split '[\\/]')) {
        if ($segment -eq '' -or $segment -eq '.' -or $segment -eq '..') { return $false }
        # Windows schneidet Punkte/Leerzeichen am Ende ab ("config.yaml " = config.yaml) und kennt
        # 8.3-Kurznamen (CONFIG~1.YAM) - solche Namen koennten den Schutz der Benutzerdaten umgehen.
        if ($segment.EndsWith('.') -or $segment.EndsWith(' ') -or $segment.StartsWith(' ')) { return $false }
        if ($segment -match '~[0-9]') { return $false }
    }
    return $true
}

# Kleine Windows-API-Hilfen (nur unter Windows per Add-Type geladen, C# 5 fuer Windows PowerShell 5.1):
# - GetReparseTag: Reparse-Tag eines Eintrags (FindFirstFileW, WIN32_FIND_DATA.dwReserved0) - so lassen
#   sich Links/Junctions von OneDrive-Platzhaltern unterscheiden (wie IsReparsePointLikeSymlink in PS 7).
# - GetFinalPath: "echter" Pfad eines vorhandenen Ordners (Links, Junctions, subst, 8.3-Kurznamen aufgeloest).
$script:JarvisNativeSource = @'
using System;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace JarvisInstaller {
    public static class NativeFs {
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct FindData {
            public uint dwFileAttributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME ftCreationTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME ftLastAccessTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME ftLastWriteTime;
            public uint nFileSizeHigh;
            public uint nFileSizeLow;
            public uint dwReserved0;
            public uint dwReserved1;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string cFileName;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 14)] public string cAlternateFileName;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr FindFirstFileW(string lpFileName, out FindData lpFindFileData);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool FindClose(IntPtr hFindFile);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFileW(string lpFileName, uint dwDesiredAccess, uint dwShareMode,
            IntPtr lpSecurityAttributes, uint dwCreationDisposition, uint dwFlagsAndAttributes, IntPtr hTemplateFile);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandleW(SafeFileHandle hFile, StringBuilder lpszFilePath,
            uint cchFilePath, uint dwFlags);

        private static string LongPath(string path) {
            if (path.Length < 248) { return path; }
            if (path.StartsWith(@"\\?\")) { return path; }
            if (path.StartsWith(@"\\")) { return @"\\?\UNC\" + path.Substring(2); }
            return @"\\?\" + path;
        }

        // 0 = kein Reparse-Punkt, -1 = nicht lesbar, sonst der Tag (0 bis 0xFFFFFFFF).
        public static long GetReparseTag(string path) {
            FindData data;
            IntPtr handle = FindFirstFileW(LongPath(path), out data);
            if (handle == new IntPtr(-1)) { return -1; }
            FindClose(handle);
            if ((data.dwFileAttributes & 0x400) == 0) { return 0; }
            return (long)data.dwReserved0;
        }

        // null, wenn der Pfad nicht geoeffnet werden kann.
        public static string GetFinalPath(string path) {
            // Zugriff 0 (nur Attribute), Freigabe lesen/schreiben/loeschen, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS (Ordner).
            using (SafeFileHandle handle = CreateFileW(LongPath(path), 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {
                if (handle.IsInvalid) { return null; }
                StringBuilder sb = new StringBuilder(1024);
                uint n = GetFinalPathNameByHandleW(handle, sb, (uint)sb.Capacity, 0);
                if (n == 0) { return null; }
                if (n >= sb.Capacity) {
                    sb = new StringBuilder((int)n + 1);
                    n = GetFinalPathNameByHandleW(handle, sb, (uint)sb.Capacity, 0);
                    if (n == 0) { return null; }
                    if (n >= sb.Capacity) { return null; }
                }
                string s = sb.ToString();
                if (s.StartsWith(@"\\?\UNC\")) { return @"\\" + s.Substring(8); }
                if (s.StartsWith(@"\\?\")) { return s.Substring(4); }
                return s;
            }
        }
    }
}
'@
$script:JarvisNativeState = $null

function Initialize-JarvisNative {
    # $true, wenn die Windows-API-Hilfen verfuegbar sind. Sonst (kein Windows, Add-Type gesperrt) $false:
    # Die Aufrufer nehmen dann die sichere Seite (unbekannter Reparse-Punkt = Link).
    if ($null -ne $script:JarvisNativeState) { return [bool]$script:JarvisNativeState }
    $script:JarvisNativeState = $false
    if (-not (Test-JarvisWindows)) { return $false }
    try {
        if (-not ('JarvisInstaller.NativeFs' -as [type])) {
            Add-Type -TypeDefinition $script:JarvisNativeSource -ErrorAction Stop
        }
        $script:JarvisNativeState = $true
    } catch {
        $script:JarvisNativeState = $false
    }
    return [bool]$script:JarvisNativeState
}

function Get-JarvisReparseTag {
    # Reparse-Tag eines Eintrags mit dem Attribut ReparsePoint; $null = nicht lesbar. Ausserhalb von
    # Windows (nur Testmodus) haben nur symbolische Links dieses Attribut: IO_REPARSE_TAG_SYMLINK.
    param([string]$Path)
    if (-not (Test-JarvisWindows)) { return [long]2684354572 }
    if (-not (Initialize-JarvisNative)) { return $null }
    try {
        $tag = [long][JarvisInstaller.NativeFs]::GetReparseTag($Path)
    } catch {
        return $null
    }
    if ($tag -lt 0) { return $null }
    return $tag
}

function Test-JarvisLinkTag {
    # Link = Name-Surrogate-Bit 0x20000000 (symbolischer Link, Junction/Mount-Point) oder
    # IO_REPARSE_TAG_APPEXECLINK (0x8000001B). OneDrive-/Cloud-Platzhalter (0x9000x01A) und
    # Dedup-Dateien sind keine Links. Unbekannter Tag ($null) zaehlt als Link (sichere Seite).
    param($Tag)
    if ($null -eq $Tag) { return $true }
    $value = [long]$Tag
    if (($value -band [long]536870912) -ne 0) { return $true }
    return ($value -eq [long]2147483675)
}

function Test-JarvisLinkLike {
    # Ist der Eintrag (FileSystemInfo) ein Link/Junction? Solchen Eintraegen wird nie gefolgt.
    # OneDrive-Platzhalter tragen ebenfalls das Attribut ReparsePoint, sind aber normale Dateien/Ordner.
    param($Item)
    if ($null -eq $Item) { return $false }
    if (([int]$Item.Attributes -band [int][System.IO.FileAttributes]::ReparsePoint) -eq 0) { return $false }
    return (Test-JarvisLinkTag (Get-JarvisReparseTag $Item.FullName))
}

function Test-JarvisReparsePoint {
    # True, wenn $Path ein Link/Junction ist (siehe Test-JarvisLinkLike); OneDrive-Platzhalter: False.
    param([string]$Path)
    try {
        $item = New-Object System.IO.DirectoryInfo $Path
        if (-not $item.Exists) {
            $item = New-Object System.IO.FileInfo $Path
            if (-not $item.Exists) { return $false }
        }
        return (Test-JarvisLinkLike $item)
    } catch {
        return $false
    }
}

function Test-JarvisHasLinkAncestor {
    # Liegt $Path in einem Ordner (zwischen $Root und $Path), der ein Link/Junction ist? Dann zeigt der
    # Pfad in Wahrheit nach draussen - dort wird weder geloescht noch geschrieben.
    param([string]$Root, [string]$Path)
    $rootFull = Get-JarvisFullPath $Root
    $dir = [System.IO.Path]::GetDirectoryName((Get-JarvisFullPath $Path))
    while ($dir -and (Test-JarvisPathUnder -Path $dir -Root $rootFull)) {
        if (Test-JarvisReparsePoint $dir) { return $true }
        $dir = [System.IO.Path]::GetDirectoryName($dir)
    }
    return $false
}

function Resolve-JarvisUnixPath {
    # Nur Testmodus (Linux/macOS): loest symbolische Links in allen Teilen eines vorhandenen Pfads auf.
    param([string]$Path, [int]$Hops = 0)
    if ($Hops -gt 40) { return $null }
    $full = [System.IO.Path]::GetFullPath($Path)
    $parent = [System.IO.Path]::GetDirectoryName($full)
    if (-not $parent) { return $full }
    $resolvedParent = Resolve-JarvisUnixPath -Path $parent -Hops $Hops
    if (-not $resolvedParent) { return $null }
    $candidate = [System.IO.Path]::Combine($resolvedParent, [System.IO.Path]::GetFileName($full))
    $info = New-Object System.IO.FileInfo $candidate
    $attributes = [int]$info.Attributes
    if ($attributes -ne -1 -and ($attributes -band [int][System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        $linkTarget = $null
        try { $linkTarget = [string]$info.LinkTarget } catch { }
        if ($linkTarget) {
            if (-not $linkTarget.StartsWith('/')) { $linkTarget = [System.IO.Path]::Combine($resolvedParent, $linkTarget) }
            return (Resolve-JarvisUnixPath -Path $linkTarget -Hops ($Hops + 1))
        }
    }
    return $candidate
}

function Resolve-JarvisPhysicalPath {
    # Der "echte" Pfad: Links/Junctions, subst-Laufwerke und 8.3-Kurznamen aufgeloest (fuer den Teil,
    # der schon existiert; der Rest wird angehaengt). $null, wenn das nicht ermittelt werden kann.
    param([string]$Path)
    $full = Get-JarvisFullPath $Path
    $existing = $full
    $rest = New-Object System.Collections.Generic.List[string]
    while (-not ([System.IO.Directory]::Exists($existing) -or [System.IO.File]::Exists($existing))) {
        $parent = [System.IO.Path]::GetDirectoryName($existing)
        if (-not $parent -or $parent -eq $existing) { return $full }
        $rest.Insert(0, [System.IO.Path]::GetFileName($existing))
        $existing = $parent
    }
    $resolved = $null
    if (Test-JarvisWindows) {
        if (Initialize-JarvisNative) {
            try { $resolved = [JarvisInstaller.NativeFs]::GetFinalPath($existing) } catch { $resolved = $null }
        }
    } else {
        $resolved = Resolve-JarvisUnixPath -Path $existing
    }
    if (-not $resolved) { return $null }
    foreach ($segment in $rest) { $resolved = [System.IO.Path]::Combine($resolved, $segment) }
    return (Get-JarvisFullPath $resolved)
}

function Test-JarvisPathHasLink {
    # Ist der Pfad selbst oder einer seiner vorhandenen Elternordner ein Link/Junction?
    param([string]$Path)
    $current = Get-JarvisFullPath $Path
    while ($current) {
        if (Test-JarvisReparsePoint $current) { return $true }
        $parent = [System.IO.Path]::GetDirectoryName($current)
        if (-not $parent -or $parent -eq $current) { break }
        $current = $parent
    }
    return $false
}

function Get-JarvisProtectedDirs {
    # Ordner, die nie Installationsziel sein duerfen (und die das Ziel auch nicht enthalten darf).
    $list = New-Object System.Collections.Generic.List[string]
    foreach ($name in @('USERPROFILE', 'HOME', 'LOCALAPPDATA', 'APPDATA', 'ProgramData', 'ProgramFiles',
            'ProgramFiles(x86)', 'ProgramW6432', 'SystemRoot', 'windir', 'TEMP', 'TMP', 'PUBLIC',
            'ALLUSERSPROFILE', 'OneDrive')) {
        $value = [System.Environment]::GetEnvironmentVariable($name)
        if ($value) { $list.Add($value) }
    }
    foreach ($folder in @('Desktop', 'MyDocuments', 'Programs', 'Startup', 'StartMenu', 'MyMusic',
            'MyPictures', 'MyVideos')) {
        try {
            $value = [System.Environment]::GetFolderPath($folder)
            if ($value) { $list.Add($value) }
        } catch { }
    }
    return $list.ToArray()
}

function Assert-JarvisSafeTarget {
    # Lehnt Laufwerkswurzeln, Benutzerprofil, LOCALAPPDATA usw. (und deren Elternordner) ab - auch
    # dann, wenn das Ziel nur ueber einen Link/Junction, ein subst-Laufwerk oder einen 8.3-Kurznamen
    # dorthin zeigt. Netzwerk- und Geraetepfade (\\server\..., \\?\..., \\.\...) werden nicht angenommen.
    param([string]$Path)
    if (-not $Path) { throw 'Kein Zielordner angegeben.' }
    if ($Path -match '^[\\/]{2}') {
        throw ("Zielordner '{0}': Netzwerk- und Geraetepfade (\\server\..., \\?\..., \\.\...) werden nicht unterstuetzt - bitte einen lokalen Ordner angeben, z. B. %LOCALAPPDATA%\JARVIS." -f $Path)
    }
    $full = Get-JarvisFullPath $Path
    $forms = New-Object System.Collections.Generic.List[string]
    $forms.Add($full)
    $physical = Resolve-JarvisPhysicalPath $full
    if ($null -eq $physical) {
        # Ohne Aufloesung (z. B. Add-Type gesperrt) keine Links im Zielpfad zulassen.
        if (Test-JarvisPathHasLink $full) {
            throw ("Zielordner '{0}' liegt in einem Link/Junction (oder ist selbst einer) - abgelehnt. Bitte einen normalen Ordner angeben." -f $full)
        }
    } elseif (-not (Test-JarvisSamePath $physical $full)) {
        $forms.Add($physical)
    }
    foreach ($form in $forms) {
        $root = [System.IO.Path]::GetPathRoot($form)
        if ($form.Length -le $root.Length) {
            throw ("Zielordner '{0}' ist ein Laufwerks-Stammverzeichnis (bzw. zeigt dorthin) - abgelehnt." -f $full)
        }
    }
    foreach ($protected in @(Get-JarvisProtectedDirs)) {
        $protectedForms = New-Object System.Collections.Generic.List[string]
        $protectedForms.Add($protected)
        $p = $null
        try { $p = Resolve-JarvisPhysicalPath $protected } catch { $p = $null }
        if ($p) { $protectedForms.Add($p) }
        foreach ($form in $forms) {
            foreach ($pf in $protectedForms) {
                if (Test-JarvisPathUnder -Path $pf -Root $form -AllowEqual) {
                    $shown = $full
                    if ($form -ne $full) { $shown = '{0} (zeigt auf {1})' -f $full, $form }
                    throw ("Zielordner '{0}' ist ein geschuetzter Ordner (bzw. enthaelt '{1}') - abgelehnt. " -f $shown, $protected) +
                        'Bitte einen eigenen Unterordner verwenden, z. B. %LOCALAPPDATA%\JARVIS.'
                }
            }
        }
    }
    return $full
}

function Set-JarvisOwnerOnlyAcl {
    # Nur Windows: Zugriff nur noch fuer den aktuellen Benutzer, SYSTEM und Administratoren (ohne Vererbung
    # vom Elternordner; bei Ordnern fuer alles darin). Fuer Ziele ausserhalb des Benutzerprofils, wo sonst
    # jeder angemeldete Benutzer secrets.yaml lesen und den Programmcode aendern koennte. Braucht keine
    # Adminrechte (der Benutzer ist Besitzer) und aendert keine Systemeinstellung. $false = nicht moeglich.
    param([string]$Path)
    if (-not (Test-JarvisWindows)) { return $false }
    $isDir = [System.IO.Directory]::Exists($Path)
    if ($isDir) {
        $item = New-Object System.IO.DirectoryInfo $Path
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
        $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    } elseif ([System.IO.File]::Exists($Path)) {
        $item = New-Object System.IO.FileInfo $Path
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $inherit = [System.Security.AccessControl.InheritanceFlags]::None
    } else {
        return $false
    }
    $acl.SetAccessRuleProtection($true, $false)
    $sids = @([System.Security.Principal.WindowsIdentity]::GetCurrent().User,
        (New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-18'),
        (New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-32-544'))
    foreach ($sid in $sids) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid,
            [System.Security.AccessControl.FileSystemRights]::FullControl, $inherit,
            [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
    }
    # Direkt ueber .NET (schreibt nur die DACL); Set-Acl wuerde auch Besitzer/SACL anfassen wollen.
    if ($PSVersionTable.PSVersion.Major -ge 6) {
        # PowerShell 7 (.NET): Erweiterungsmethode. Typ ueber -as, damit Windows PowerShell 5.1 (und die
        # Kompatibilitaetspruefung) ihn nie aufloest.
        $ext = 'System.IO.FileSystemAclExtensions' -as [type]
        if (-not $ext) {
            try { Add-Type -AssemblyName 'System.IO.FileSystem.AccessControl' -ErrorAction Stop } catch { $ext = $null }
            $ext = 'System.IO.FileSystemAclExtensions' -as [type]
        }
        if (-not $ext) { return $false }
        $ext::SetAccessControl($item, $acl)
    } else {
        $item.SetAccessControl($acl)
    }
    return $true
}

function Get-JarvisDefaultTarget {
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA ist nicht gesetzt - bitte -Target <Ordner> angeben.' }
    return (Join-Path $env:LOCALAPPDATA 'JARVIS')
}

function Get-JarvisSafeWorkingDir {
    # Ein Arbeitsordner ausserhalb des Ziels (sonst kann Windows den Zielordner nicht loeschen).
    param([string]$Target)
    $candidates = @([System.IO.Path]::GetTempPath(), (Get-JarvisHomeDir))
    try { $candidates += [System.IO.Path]::GetDirectoryName((Get-JarvisFullPath $Target)) } catch { }
    foreach ($c in $candidates) {
        if (-not $c) { continue }
        if (-not [System.IO.Directory]::Exists($c)) { continue }
        if (Test-JarvisPathUnder -Path $c -Root $Target -AllowEqual) { continue }
        return (Get-JarvisFullPath $c)
    }
    return $null
}

function Test-JarvisDirNonEmpty {
    param([string]$Path)
    if (-not [System.IO.Directory]::Exists($Path)) { return $false }
    return ([System.IO.Directory]::GetFileSystemEntries($Path).Length -gt 0)
}

function Test-JarvisLooksLikeJarvis {
    # Alte Installation ohne Marker (z. B. von Hand kopiert)? Nicht nur der Projektname "jarvis" (den
    # tragen auch fremde Hobbyprojekte), sondern auch die eigenen Installer-Dateien muessen da sein.
    param([string]$Path)
    foreach ($rel in @('scripts/installer-lib.ps1', 'app/setup_wizard.py', 'config.example.yaml')) {
        if (-not [System.IO.File]::Exists((Join-JarvisRelPath $Path $rel))) { return $false }
    }
    $pyproject = Join-Path $Path 'pyproject.toml'
    if (-not [System.IO.File]::Exists($pyproject)) { return $false }
    $text = [System.IO.File]::ReadAllText($pyproject)
    return ($text -match '(?m)^\s*name\s*=\s*"jarvis"\s*$')
}

function Get-JarvisForeignEntries {
    # Namen im Zielordner, die weder Benutzerdaten von JARVIS noch der Installationsmarker sind.
    param([string]$Path)
    if (-not [System.IO.Directory]::Exists($Path)) { return @() }
    $protected = @(Get-JarvisProtectedNames)
    $marker = Get-JarvisMarkerName
    return @([System.IO.Directory]::GetFileSystemEntries($Path) | ForEach-Object { [System.IO.Path]::GetFileName($_) } |
            Where-Object { $_ -ne $marker -and ($protected -notcontains $_) -and -not (Test-JarvisUserDataFileName $_) } |
            Sort-Object)
}

function Test-JarvisOnlyUserData {
    # Enthaelt der Ordner nur Einstellungen einer frueheren Installation (config.yaml, secrets.yaml, state)?
    param([string]$Path)
    if (-not [System.IO.Directory]::Exists($Path)) { return $false }
    $entries = @([System.IO.Directory]::GetFileSystemEntries($Path) | ForEach-Object { [System.IO.Path]::GetFileName($_) })
    if ($entries.Count -eq 0) { return $false }
    foreach ($name in $entries) {
        if ((Get-JarvisProtectedNames) -notcontains $name) { return $false }
    }
    return $true
}

function Assert-JarvisSourceFolder {
    param([string]$Source)
    foreach ($rel in @('app/__main__.py', 'pyproject.toml', 'config.example.yaml', 'scripts/installer-lib.ps1')) {
        if (-not [System.IO.File]::Exists((Join-JarvisRelPath $Source $rel))) {
            throw ("Quellordner unvollstaendig: '{0}' fehlt in {1}. Bitte das ZIP-Archiv komplett entpacken." -f $rel, $Source)
        }
    }
}

function Get-JarvisRequiredProgramFiles {
    # Ohne diese Dateien in der Dateiliste ist die Installation unbrauchbar.
    return @('app/__main__.py', 'pyproject.toml', 'uv.lock', 'requirements.txt', 'scripts/installer-lib.ps1')
}

function Assert-JarvisProgramFileList {
    # Sicherheitsnetz vor dem Kopieren: fehlen Pflichtdateien in der Liste, waren sie beim Lesen nicht
    # greifbar (typisch: Quellordner in OneDrive, Dateien nur online bzw. als Platzhalter).
    param([string[]]$Files, [string]$Source)
    $have = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($f in @($Files)) { if ($f) { [void]$have.Add($f) } }
    $missing = @(Get-JarvisRequiredProgramFiles | Where-Object { -not $have.Contains($_) })
    if ($missing.Count -gt 0) {
        throw (("Im Quellordner {0} fehlen Programmdateien ({1}). Liegt der Ordner in OneDrive (Dateien nur " +
                'online bzw. als Platzhalter)? Bitte das ZIP z. B. nach C:\JARVIS-Setup entpacken (nicht in ' +
                'Desktop/Dokumente unter OneDrive) und Install.cmd dort starten.') -f $Source, ($missing -join ', '))
    }
}

# ---------------------------------------------------------------------------------------------
# Programmdateien: Auswahl, Kopieren, Loeschen
# ---------------------------------------------------------------------------------------------

function Get-JarvisMarkerName { return '.jarvis-install.json' }

function Get-JarvisUserDataFiles {
    # Dateien des Benutzers im Installationsordner. config.yaml.bak legt der Einrichtungsassistent
    # beim Aendern einer vorhandenen config.yaml an (enthaelt also auch Einstellungen).
    return @('config.yaml', 'config.yaml.bak', 'secrets.yaml')
}

function Get-JarvisProtectedNames {
    # Gehoeren dem Benutzer: werden von Installation/Update nie angefasst, nur mit -Purge geloescht.
    return @(Get-JarvisUserDataFiles) + @('state')
}

function Test-JarvisUserDataFileName {
    # Auch Zwischendateien des Assistenten (".config.yaml.XXXX.tmp") zaehlen dazu.
    param([string]$Name)
    if ((Get-JarvisUserDataFiles) -contains $Name) { return $true }
    foreach ($base in @('config.yaml', 'secrets.yaml')) {
        if ($Name -like ($base + '.*') -or $Name -like ('.' + $base + '.*')) { return $true }
    }
    return $false
}

function Get-JarvisMarkerTempName { return '.jarvis-install.json.tmp' }

function Test-JarvisProtectedRelPath {
    param([string]$RelPath)
    $parts = @($RelPath -split '[\\/]')
    # Wie Windows: Punkte/Leerzeichen am Ende zaehlen nicht ("config.yaml " = config.yaml).
    $first = $parts[0].TrimEnd([char[]]@([char]'.', [char]' '))
    if ((Get-JarvisProtectedNames) -contains $first) { return $true }
    if ($parts.Count -eq 1 -and (Test-JarvisUserDataFileName $first)) { return $true }
    return (@((Get-JarvisMarkerName), (Get-JarvisMarkerTempName)) -contains $first)
}

function Test-JarvisExcludedDir {
    param([string]$Name, [bool]$IsTop)
    if (@('.venv', '__pycache__', '.pytest_cache', '.git', 'node_modules', '.mypy_cache', '.ruff_cache') -contains $Name) {
        return $true
    }
    if ($IsTop -and (@('state', 'dist', 'tests', '.idea', '.vscode') -contains $Name)) { return $true }
    return $false
}

function Test-JarvisExcludedFile {
    param([string]$Name, [bool]$IsTop)
    if ($IsTop -and (@((Get-JarvisMarkerName), (Get-JarvisMarkerTempName), '.gitignore', '.gitattributes') -contains $Name)) { return $true }
    if ($IsTop -and (Test-JarvisUserDataFileName $Name)) { return $true }
    foreach ($pattern in @('*.pyc', '*.pyo')) {
        if ($Name -like $pattern) { return $true }
    }
    return $false
}

function Get-JarvisProgramFiles {
    # Alle zu installierenden Dateien als relative Pfade mit '/' (sortiert). Links/Junctions werden nie
    # verfolgt; OneDrive-Platzhalter (auch ReparsePoint) sind normale Dateien und Ordner.
    param([Parameter(Mandatory = $true)][string]$Source)
    $src = Get-JarvisFullPath $Source
    $result = New-Object System.Collections.Generic.List[string]
    $pending = New-Object System.Collections.Generic.Queue[string]
    $pending.Enqueue('')
    while ($pending.Count -gt 0) {
        $rel = $pending.Dequeue()
        $dir = $src
        if ($rel) { $dir = Join-JarvisRelPath $src $rel }
        foreach ($entry in (New-Object System.IO.DirectoryInfo $dir).GetFileSystemInfos()) {
            if (Test-JarvisLinkLike $entry) { continue }
            $childRel = $entry.Name
            if ($rel) { $childRel = $rel + '/' + $entry.Name }
            $isTop = -not $rel
            if ($entry -is [System.IO.DirectoryInfo]) {
                if (-not (Test-JarvisExcludedDir -Name $entry.Name -IsTop $isTop)) { $pending.Enqueue($childRel) }
            } elseif (-not (Test-JarvisExcludedFile -Name $entry.Name -IsTop $isTop)) {
                $result.Add($childRel)
            }
        }
    }
    $result.Sort([System.StringComparer]::Ordinal)
    return $result.ToArray()
}

function Copy-JarvisProgramFiles {
    # Kopiert die Dateien nach Target. config.yaml, secrets.yaml und state/ werden nie beschrieben.
    param([string]$Source, [string]$Target, [string[]]$Files)
    $src = Get-JarvisFullPath $Source
    $dst = Get-JarvisFullPath $Target
    $count = 0
    foreach ($rel in $Files) {
        if (-not (Test-JarvisRelPathValid $rel)) { throw ("Ungueltiger Dateipfad: {0}" -f $rel) }
        if (Test-JarvisProtectedRelPath $rel) { continue }
        $from = Join-JarvisRelPath $src $rel
        $to = Join-JarvisRelPath $dst $rel
        if (-not (Test-JarvisPathUnder -Path $to -Root $dst)) { throw ("Pfad ausserhalb des Ziels: {0}" -f $rel) }
        if (Test-JarvisHasLinkAncestor -Root $dst -Path $to) {
            throw ("'{0}' liegt im Zielordner hinter einem Ordner-Link/Junction - der Installer schreibt nie ausserhalb des Installationsordners. Bitte den Link entfernen (oder durch einen normalen Ordner ersetzen) und Install.cmd erneut starten." -f $rel)
        }
        $dir = [System.IO.Path]::GetDirectoryName($to)
        if (-not [System.IO.Directory]::Exists($dir)) { [void][System.IO.Directory]::CreateDirectory($dir) }
        $existing = New-Object System.IO.FileInfo $to
        # Ein Datei-Link am Zielort wird ersetzt, nie durch ihn hindurch geschrieben.
        if (Test-JarvisReparsePoint $to) { Remove-JarvisFileEntry $to }
        elseif ($existing.Exists -and $existing.IsReadOnly) { $existing.IsReadOnly = $false }
        [System.IO.File]::Copy($from, $to, $true)
        $count++
    }
    return $count
}

function Unblock-JarvisFiles {
    # Entfernt die Internet-Markierung (Mark-of-the-Web) aus dem heruntergeladenen ZIP.
    param([string]$Target, [string[]]$Files)
    if (-not (Test-JarvisWindows)) { return }
    foreach ($rel in $Files) {
        try { Unblock-File -LiteralPath (Join-JarvisRelPath $Target $rel) -ErrorAction Stop } catch { }
    }
}

function New-JarvisRemovalResult {
    # Removed = geloeschte Dateien (bzw. Verknuepfungen), RemovedDirs = danach leer geloeschte Ordner,
    # Skipped = ungueltiger/fremder Pfad oder Pfad hinter einem Ordner-Link,
    # Foreign = Verknuepfung einer anderen JARVIS-Installation.
    return [pscustomobject]@{
        Removed     = New-Object System.Collections.Generic.List[string]
        RemovedDirs = New-Object System.Collections.Generic.List[string]
        Failed      = New-Object System.Collections.Generic.List[string]
        Skipped     = New-Object System.Collections.Generic.List[string]
        Foreign     = New-Object System.Collections.Generic.List[string]
    }
}

function Add-JarvisRemovalResult {
    param($Into, $From)
    foreach ($x in $From.Removed) { $Into.Removed.Add($x) }
    foreach ($x in $From.RemovedDirs) { $Into.RemovedDirs.Add($x) }
    foreach ($x in $From.Failed) { $Into.Failed.Add($x) }
    foreach ($x in $From.Skipped) { $Into.Skipped.Add($x) }
    foreach ($x in $From.Foreign) { $Into.Foreign.Add($x) }
}

function Remove-JarvisFileEntry {
    # Loescht eine Datei oder einen Link (nie das Ziel eines Links).
    param([string]$Path)
    $fi = New-Object System.IO.FileInfo $Path
    if (($fi.Attributes -band [System.IO.FileAttributes]::ReadOnly) -ne 0 -and $fi.Exists) { $fi.IsReadOnly = $false }
    [System.IO.File]::Delete($Path)
}

function Remove-JarvisLinkEntry {
    # Entfernt einen Ordner-Link/Junction selbst, ohne den Inhalt dahinter anzufassen.
    param([string]$Path)
    try {
        [System.IO.Directory]::Delete($Path, $false)
    } catch {
        [System.IO.File]::Delete($Path)
    }
}

function Remove-JarvisTree {
    # Loescht einen Ordner rekursiv, folgt dabei nie Links/Junctions und bleibt immer unter $Root.
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Root)
    $full = Get-JarvisFullPath $Path
    if (-not (Test-JarvisPathUnder -Path $full -Root $Root)) {
        throw ("Sicherheitsstopp: '{0}' liegt nicht in '{1}'." -f $full, $Root)
    }
    $dir = New-Object System.IO.DirectoryInfo $full
    $isLink = (Test-JarvisReparsePoint $full)
    if ($isLink) {
        if ($dir.Exists) { Remove-JarvisLinkEntry $full } else { Remove-JarvisFileEntry $full }
        return
    }
    if (-not $dir.Exists) {
        if ([System.IO.File]::Exists($full)) { Remove-JarvisFileEntry $full }
        return
    }
    foreach ($entry in $dir.GetFileSystemInfos()) {
        # Links/Junctions nur selbst entfernen; OneDrive-Platzhalter sind normale Ordner/Dateien.
        $isEntryLink = Test-JarvisLinkLike $entry
        if ($entry -is [System.IO.DirectoryInfo]) {
            if ($isEntryLink) { Remove-JarvisLinkEntry $entry.FullName }
            else { Remove-JarvisTree -Path $entry.FullName -Root $Root }
        } else {
            Remove-JarvisFileEntry $entry.FullName
        }
    }
    [System.IO.Directory]::Delete($full, $false)
}

function Remove-JarvisEmptyDirs {
    # Entfernt (tiefste zuerst) leer gewordene Ordner; __pycache__ darin wird vorher geloescht.
    param([string]$Root, [string[]]$Dirs, $Result)
    $unique = New-Object System.Collections.Generic.List[string]
    foreach ($d in @($Dirs)) { if ($d -and -not $unique.Contains($d)) { $unique.Add($d) } }
    $sorted = @($unique.ToArray() | Sort-Object -Property Length -Descending)
    foreach ($d in $sorted) {
        if (-not (Test-JarvisPathUnder -Path $d -Root $Root)) { continue }
        if (-not [System.IO.Directory]::Exists($d) -or (Test-JarvisReparsePoint $d)) { continue }
        if (Test-JarvisHasLinkAncestor -Root $Root -Path $d) { continue }
        $cache = Join-Path $d '__pycache__'
        if ([System.IO.Directory]::Exists($cache)) {
            try { Remove-JarvisTree -Path $cache -Root $Root } catch { $Result.Failed.Add(('{0} ({1})' -f $cache, (Get-JarvisErrorMessage $_))) }
        }
        if ([System.IO.Directory]::GetFileSystemEntries($d).Length -eq 0) {
            try {
                [System.IO.Directory]::Delete($d, $false)
                $Result.RemovedDirs.Add($d)
            } catch {
                $Result.Failed.Add(('{0} ({1})' -f $d, (Get-JarvisErrorMessage $_)))
            }
        }
    }
}

function Remove-JarvisRelativeFiles {
    # Loescht genau die angegebenen Dateien (relativ zu Target) und danach leere Ordner.
    param([string]$Target, [string[]]$Files, [switch]$AllowProtected)
    $root = Get-JarvisFullPath $Target
    $result = New-JarvisRemovalResult
    $dirs = New-Object System.Collections.Generic.List[string]
    foreach ($rel in @($Files | Where-Object { $_ })) {
        if (-not (Test-JarvisRelPathValid $rel)) { $result.Skipped.Add($rel); continue }
        if ((Test-JarvisProtectedRelPath $rel) -and -not $AllowProtected) { $result.Skipped.Add($rel); continue }
        $full = Join-JarvisRelPath $root $rel
        if (-not (Test-JarvisPathUnder -Path $full -Root $root)) { $result.Skipped.Add($rel); continue }
        # Ein Ordner auf dem Weg ist ein Link/Junction: die Datei liegt in Wahrheit ausserhalb.
        if (Test-JarvisHasLinkAncestor -Root $root -Path $full) { $result.Skipped.Add($rel); continue }
        $parent = [System.IO.Path]::GetDirectoryName($full)
        while ($parent -and (Test-JarvisPathUnder -Path $parent -Root $root)) {
            if (-not $dirs.Contains($parent)) { $dirs.Add($parent) }
            $parent = [System.IO.Path]::GetDirectoryName($parent)
        }
        $fi = New-Object System.IO.FileInfo $full
        if (-not $fi.Exists -and -not (Test-JarvisReparsePoint $full)) { continue }
        if ([System.IO.Directory]::Exists($full) -and -not (Test-JarvisReparsePoint $full)) {
            $result.Skipped.Add($rel)
            continue
        }
        try {
            Remove-JarvisFileEntry $full
            $result.Removed.Add($rel)
        } catch {
            $result.Failed.Add(('{0} ({1})' -f $rel, (Get-JarvisErrorMessage $_)))
        }
    }
    Remove-JarvisEmptyDirs -Root $root -Dirs $dirs.ToArray() -Result $result
    return $result
}

function Remove-JarvisStaleFiles {
    # Beim Update: Dateien der alten Version, die es in der neuen nicht mehr gibt.
    param([string]$Target, [string[]]$OldFiles, [string[]]$NewFiles)
    $keep = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($f in @($NewFiles)) { if ($f) { [void]$keep.Add($f) } }
    $stale = @($OldFiles | Where-Object { $_ -and -not $keep.Contains($_) })
    return (Remove-JarvisRelativeFiles -Target $Target -Files $stale)
}

# ---------------------------------------------------------------------------------------------
# Manifest (.jarvis-install.json)
# ---------------------------------------------------------------------------------------------

function Get-JarvisManifestPath {
    param([string]$Target)
    return (Join-Path $Target (Get-JarvisMarkerName))
}

function Format-JarvisDate {
    # Zeitstempel aus dem Manifest lesbar ausgeben (PS 7 liefert DateTime, PS 5.1 den ISO-String).
    param($Value)
    if ($null -eq $Value) { return '?' }
    if ($Value -is [DateTime]) { return $Value.ToLocalTime().ToString('yyyy-MM-dd HH:mm', [System.Globalization.CultureInfo]::InvariantCulture) }
    $utc = ConvertTo-JarvisUtcDate $Value
    if ($utc) { return $utc.ToLocalTime().ToString('yyyy-MM-dd HH:mm', [System.Globalization.CultureInfo]::InvariantCulture) }
    return [string]$Value
}

function Get-JarvisTimestamp {
    $now = [DateTime]::UtcNow
    return ($now.ToString('yyyy-MM-ddTHH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture) + 'Z')
}

function Get-JarvisVersion {
    # Version aus [project] in pyproject.toml.
    param([string]$ProjectDir)
    $file = Join-Path $ProjectDir 'pyproject.toml'
    if (-not [System.IO.File]::Exists($file)) { return '0.0.0' }
    $inProject = $false
    foreach ($line in [System.IO.File]::ReadAllLines($file)) {
        $t = $line.Trim()
        if ($t -match '^\[([^\]]+)\]$') { $inProject = ($Matches[1].Trim() -eq 'project'); continue }
        if ($inProject -and $t -match '^version\s*=\s*"([^"]+)"') { return $Matches[1] }
    }
    return '0.0.0'
}

function ConvertTo-JarvisJsonString {
    # JSON-String; alles ausserhalb von ASCII wird als \uXXXX geschrieben (Datei bleibt ASCII).
    param([AllowEmptyString()][string]$Value)
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    foreach ($ch in $Value.ToCharArray()) {
        $code = [int]$ch
        if ($code -eq 34) { [void]$sb.Append('\"') }
        elseif ($code -eq 92) { [void]$sb.Append('\\') }
        elseif ($code -lt 32 -or $code -gt 126) { [void]$sb.Append(('\u{0:x4}' -f $code)) }
        else { [void]$sb.Append($ch) }
    }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function ConvertTo-JarvisJson {
    # Kleiner, in PS 5.1 und 7 identischer JSON-Serialisierer (Strings, Zahlen, Bool, null, Listen, Dictionaries).
    param($Value, [int]$Indent = 0)
    $pad = '  ' * ($Indent + 1)
    $padEnd = '  ' * $Indent
    if ($null -eq $Value) { return 'null' }
    if ($Value -is [bool]) {
        if ($Value) { return 'true' }
        return 'false'
    }
    if ($Value -is [int] -or $Value -is [long] -or $Value -is [double] -or $Value -is [decimal]) {
        return [string]::Format([System.Globalization.CultureInfo]::InvariantCulture, '{0}', $Value)
    }
    if ($Value -is [string]) { return (ConvertTo-JarvisJsonString $Value) }
    if ($Value -is [DateTime]) {
        # PS 7 macht aus ISO-Zeitstempeln beim Lesen DateTime-Objekte - wieder als ISO-8601 (UTC) schreiben.
        $utc = $Value.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture)
        return (ConvertTo-JarvisJsonString ($utc + 'Z'))
    }
    if ($Value -is [System.Collections.IDictionary]) {
        if ($Value.Count -eq 0) { return '{}' }
        $parts = New-Object System.Collections.Generic.List[string]
        foreach ($key in $Value.Keys) {
            $parts.Add($pad + (ConvertTo-JarvisJsonString ([string]$key)) + ': ' + (ConvertTo-JarvisJson -Value $Value[$key] -Indent ($Indent + 1)))
        }
        return "{`n" + ($parts.ToArray() -join ",`n") + "`n" + $padEnd + '}'
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        $items = New-Object System.Collections.Generic.List[string]
        foreach ($v in $Value) { $items.Add($pad + (ConvertTo-JarvisJson -Value $v -Indent ($Indent + 1))) }
        if ($items.Count -eq 0) { return '[]' }
        return "[`n" + ($items.ToArray() -join ",`n") + "`n" + $padEnd + ']'
    }
    return (ConvertTo-JarvisJsonString ([string]$Value))
}

function New-JarvisManifest {
    param(
        [string]$Version,
        [string]$Source,
        [string[]]$Files = @(),
        [string[]]$Shortcuts = @(),
        [string]$Autostart,
        [string]$PythonEnv,
        [string[]]$Preexisting = @()
    )
    $m = [ordered]@{}
    $m['version'] = $Version
    $m['installed_at'] = Get-JarvisTimestamp
    $m['source'] = $Source
    $m['shortcuts'] = [string[]]@($Shortcuts | Where-Object { $_ })
    $m['autostart'] = $null
    if ($Autostart) { $m['autostart'] = $Autostart }
    $m['files'] = [string[]]@($Files | Where-Object { $_ })
    $m['venv'] = '.venv'
    $m['python_env'] = $null
    if ($PythonEnv) { $m['python_env'] = $PythonEnv }
    # Benutzerdaten (config.yaml, secrets.yaml, state ...), die schon VOR der ersten Installation im
    # Ordner lagen: Sie gehoeren nicht dem Installer und werden auch mit -Purge nie geloescht.
    $m['preexisting'] = [string[]]@($Preexisting | Where-Object { $_ })
    return $m
}

function New-JarvisRemnantManifest {
    # Marker nach einer Deinstallation ohne -Purge: Programm, .venv und Verknuepfungen sind weg,
    # nur die Einstellungen des Benutzers (config.yaml, secrets.yaml, state\) bleiben. Der Rest-Marker
    # belegt, dass sie zu JARVIS gehoeren: Ein spaeteres "uninstall.ps1 -Purge" darf sie dann loeschen
    # (ausser denen unter "preexisting"), eine Neuinstallation uebernimmt sie. Er listet keine Dateien mehr.
    param($Manifest)
    $m = New-JarvisManifest -Version ([string]$Manifest.version) -Source ([string]$Manifest.source) `
        -Preexisting @(Get-JarvisManifestList $Manifest 'preexisting')
    if ($null -ne $Manifest.installed_at) { $m['installed_at'] = $Manifest.installed_at }
    $m['venv'] = $null
    $m['uninstalled_at'] = Get-JarvisTimestamp
    if ((Test-JarvisRemnantManifest $Manifest) -and $null -ne $Manifest.uninstalled_at) { $m['uninstalled_at'] = $Manifest.uninstalled_at }
    return $m
}

function Test-JarvisRemnantManifest {
    # True fuer den Rest-Marker einer Deinstallation ohne -Purge (siehe New-JarvisRemnantManifest).
    param($Manifest)
    if ($null -eq $Manifest) { return $false }
    return (@($Manifest.PSObject.Properties.Name) -contains 'uninstalled_at')
}

function Get-JarvisUserDataEntries {
    # Namen der Benutzerdaten direkt im Installationsordner (config.yaml, config.yaml.bak, secrets.yaml,
    # state, Zwischendateien des Assistenten), sortiert.
    param([string]$Target)
    if (-not [System.IO.Directory]::Exists($Target)) { return @() }
    $protected = @(Get-JarvisProtectedNames)
    return @([System.IO.Directory]::GetFileSystemEntries($Target) | ForEach-Object { [System.IO.Path]::GetFileName($_) } |
            Where-Object { ($protected -contains $_) -or (Test-JarvisUserDataFileName $_) } | Sort-Object)
}

function Write-JarvisManifest {
    # Atomar: erst eine Zwischendatei im selben Ordner, dann in einem Schritt ersetzen - ein Abbruch
    # mittendrin hinterlaesst nie einen halb geschriebenen Marker.
    param([string]$Target, $Manifest)
    $json = (ConvertTo-JarvisJson -Value $Manifest) + "`n"
    $path = Get-JarvisManifestPath $Target
    $tmp = Join-Path $Target (Get-JarvisMarkerTempName)
    [System.IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding($false)))
    try {
        if ([System.IO.File]::Exists($path)) {
            try {
                # NullString: ein $null wuerde PowerShell hier als "" uebergeben (= ungueltiger Sicherungspfad).
                [System.IO.File]::Replace($tmp, $path, [System.Management.Automation.Language.NullString]::Value)
            } catch {
                # Manche Dateisysteme koennen kein Replace - dann loeschen und umbenennen.
                [System.IO.File]::Delete($path)
                [System.IO.File]::Move($tmp, $path)
            }
        } else {
            [System.IO.File]::Move($tmp, $path)
        }
    } finally {
        if ([System.IO.File]::Exists($tmp)) { try { [System.IO.File]::Delete($tmp) } catch { } }
    }
}

function Read-JarvisManifest {
    # $null, wenn kein Marker existiert; Fehler, wenn er beschaedigt ist.
    param([string]$Target)
    $path = Get-JarvisManifestPath $Target
    if (-not [System.IO.File]::Exists($path)) { return $null }
    try {
        $manifest = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    } catch {
        throw ("Installationsmarker '{0}' ist beschaedigt: {1}" -f $path, $_.Exception.Message)
    }
    if ($null -eq $manifest -or -not (@($manifest.PSObject.Properties.Name) -contains 'version')) {
        throw ("'{0}' ist kein gueltiger JARVIS-Installationsmarker." -f $path)
    }
    return $manifest
}

function ConvertTo-JarvisManifestDict {
    # Gelesenes Manifest (PSCustomObject) zurueck in ein geordnetes Dictionary.
    param($Manifest)
    $m = [ordered]@{}
    foreach ($p in $Manifest.PSObject.Properties) {
        $value = $p.Value
        if ($value -is [System.Array]) { $value = [string[]]@($value | Where-Object { $null -ne $_ }) }
        $m[$p.Name] = $value
    }
    return $m
}

function Get-JarvisManifestList {
    # Liste aus dem Manifest ohne leere Eintraege (PS 5.1 und 7 liefern hier Unterschiedliches).
    param($Manifest, [string]$Name)
    if ($null -eq $Manifest) { return @() }
    if (-not (@($Manifest.PSObject.Properties.Name) -contains $Name)) { return @() }
    return @(@($Manifest.$Name) | Where-Object { $_ } | ForEach-Object { [string]$_ })
}

# ---------------------------------------------------------------------------------------------
# Prozesse starten (ohne die Eigenheiten von Start-Process und nativen Aufrufen in PS 5.1)
# ---------------------------------------------------------------------------------------------

function ConvertTo-JarvisArgString {
    # Baut eine Windows-Kommandozeile nach den Regeln von CommandLineToArgvW.
    param([string[]]$ArgumentList = @())
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($arg in $ArgumentList) {
        if ($null -eq $arg) { $arg = '' }
        if ($arg -ne '' -and $arg -notmatch '[\s"]') { $parts.Add($arg); continue }
        $sb = New-Object System.Text.StringBuilder
        [void]$sb.Append('"')
        $backslashes = 0
        foreach ($ch in $arg.ToCharArray()) {
            if ($ch -eq [char]92) { $backslashes++; continue }
            if ($ch -eq [char]34) {
                [void]$sb.Append(('\' * (2 * $backslashes + 1)) + '"')
                $backslashes = 0
                continue
            }
            if ($backslashes -gt 0) { [void]$sb.Append('\' * $backslashes); $backslashes = 0 }
            [void]$sb.Append($ch)
        }
        if ($backslashes -gt 0) { [void]$sb.Append('\' * (2 * $backslashes)) }
        [void]$sb.Append('"')
        $parts.Add($sb.ToString())
    }
    return ($parts.ToArray() -join ' ')
}

function Format-JarvisCommand {
    param([string]$FilePath, [string[]]$ArgumentList = @())
    return ((ConvertTo-JarvisArgString @($FilePath)) + ' ' + (ConvertTo-JarvisArgString $ArgumentList)).Trim()
}

function New-JarvisStartInfo {
    param([string]$FilePath, [string[]]$ArgumentList, [string]$WorkingDirectory, [hashtable]$Environment)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = ConvertTo-JarvisArgString $ArgumentList
    $psi.UseShellExecute = $false
    if ($WorkingDirectory) { $psi.WorkingDirectory = $WorkingDirectory }
    if ($Environment) {
        foreach ($key in $Environment.Keys) { $psi.EnvironmentVariables[[string]$key] = [string]$Environment[$key] }
    }
    return $psi
}

function Invoke-JarvisProcess {
    # Startet ein Programm im selben Konsolenfenster (Ein-/Ausgabe direkt), wartet, liefert den Exitcode.
    param([string]$FilePath, [string[]]$ArgumentList = @(), [string]$WorkingDirectory, [hashtable]$Environment)
    $psi = New-JarvisStartInfo -FilePath $FilePath -ArgumentList $ArgumentList -WorkingDirectory $WorkingDirectory -Environment $Environment
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
    } catch {
        throw ("Programm konnte nicht gestartet werden: {0} ({1})" -f $FilePath, $_.Exception.Message)
    }
    $proc.WaitForExit()
    return $proc.ExitCode
}

function Invoke-JarvisCapture {
    # Startet ein Programm und liest stdout/stderr (UTF-8). Liefert ExitCode, Output (Zeilen), ErrorText.
    param([string]$FilePath, [string[]]$ArgumentList = @(), [string]$WorkingDirectory, [int]$TimeoutSec = 60,
        [hashtable]$Environment)
    $envVars = @{ PYTHONIOENCODING = 'utf-8' }
    if ($Environment) { foreach ($k in $Environment.Keys) { $envVars[$k] = $Environment[$k] } }
    $psi = New-JarvisStartInfo -FilePath $FilePath -ArgumentList $ArgumentList -WorkingDirectory $WorkingDirectory -Environment $envVars
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
    } catch {
        return [pscustomobject]@{ ExitCode = -1; Output = @(); ErrorText = $_.Exception.Message }
    }
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        try { $proc.Kill() } catch { }
        return [pscustomobject]@{ ExitCode = -2; Output = @(); ErrorText = ('Zeitueberschreitung nach {0} s' -f $TimeoutSec) }
    }
    $proc.WaitForExit()
    $lines = @(($outTask.Result -split "`r?`n") | Where-Object { $_ -ne '' })
    return [pscustomobject]@{ ExitCode = $proc.ExitCode; Output = $lines; ErrorText = [string]$errTask.Result }
}

# ---------------------------------------------------------------------------------------------
# Python-Umgebung: uv oder python -m venv + pip
# ---------------------------------------------------------------------------------------------

function Get-JarvisVenvDir {
    param([string]$Target)
    return (Join-Path $Target '.venv')
}

function Get-JarvisVenvPython {
    # python.exe der venv; mit -Windowed pythonw.exe (Fallback python.exe, falls es fehlt).
    param([string]$Target, [switch]$Windowed)
    $venv = Get-JarvisVenvDir $Target
    if (Test-JarvisWindows) {
        $scripts = Join-Path $venv 'Scripts'
        if ($Windowed) {
            $pythonw = Join-Path $scripts 'pythonw.exe'
            if ([System.IO.File]::Exists($pythonw)) { return $pythonw }
        }
        return (Join-Path $scripts 'python.exe')
    }
    return (Join-Path (Join-Path $venv 'bin') 'python')
}

function Get-JarvisUvCandidates {
    # PATH, dann die Standardorte laut uv-Doku (docs/reference/storage.md, "Executable directory"):
    # %XDG_BIN_HOME%, %XDG_DATA_HOME%\..\bin, %USERPROFILE%\.local\bin; vor uv 0.5.0: ~\.cargo\bin.
    $list = New-Object System.Collections.Generic.List[string]
    $cmd = Get-Command -Name 'uv' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { $list.Add([string]$cmd.Path) }
    $exe = 'uv'
    if (Test-JarvisWindows) { $exe = 'uv.exe' }
    $dirs = New-Object System.Collections.Generic.List[string]
    if ($env:XDG_BIN_HOME) { $dirs.Add($env:XDG_BIN_HOME) }
    if ($env:XDG_DATA_HOME) { $dirs.Add([System.IO.Path]::Combine($env:XDG_DATA_HOME, '..', 'bin')) }
    $userHome = Get-JarvisHomeDir
    if ($userHome) {
        $dirs.Add([System.IO.Path]::Combine($userHome, '.local', 'bin'))
        $dirs.Add([System.IO.Path]::Combine($userHome, '.cargo', 'bin'))
    }
    foreach ($d in $dirs) { $list.Add((Join-Path $d $exe)) }
    return $list.ToArray()
}

function Find-JarvisUv {
    foreach ($candidate in @(Get-JarvisUvCandidates)) {
        if (-not $candidate -or -not [System.IO.File]::Exists($candidate)) { continue }
        $r = Invoke-JarvisCapture -FilePath $candidate -ArgumentList @('--version') -TimeoutSec 30
        if ($r.ExitCode -eq 0) {
            return [pscustomobject]@{ Path = $candidate; Version = (($r.Output -join ' ').Trim()) }
        }
    }
    return $null
}

function Get-JarvisUvInstallCommand {
    # Offizieller Befehl laut uv-Doku (docs/getting-started/installation.md, "Standalone installer").
    if (Test-JarvisWindows) {
        $ps = 'powershell'
        if ($env:SystemRoot) {
            $full = [System.IO.Path]::Combine($env:SystemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
            if ([System.IO.File]::Exists($full)) { $ps = $full }
        }
        return [pscustomobject]@{
            FilePath     = $ps
            ArgumentList = @('-ExecutionPolicy', 'ByPass', '-c', 'irm https://astral.sh/uv/install.ps1 | iex')
            Display      = 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
        }
    }
    return [pscustomobject]@{
        FilePath     = 'sh'
        ArgumentList = @('-c', 'curl -LsSf https://astral.sh/uv/install.sh | sh')
        Display      = 'curl -LsSf https://astral.sh/uv/install.sh | sh'
    }
}

function Install-JarvisUv {
    # Nur nach ausdruecklicher Zustimmung aufrufen! UV_NO_MODIFY_PATH=1 (laut uv-Doku): der
    # Installer aendert dann weder PATH noch Shell-Profile.
    $cmd = Get-JarvisUvInstallCommand
    Write-JarvisInfo ('Fuehre aus: ' + $cmd.Display)
    return (Invoke-JarvisProcess -FilePath $cmd.FilePath -ArgumentList $cmd.ArgumentList -Environment @{ UV_NO_MODIFY_PATH = '1' })
}

function Get-JarvisUvSyncArguments {
    return @('sync', '--frozen', '--no-dev')
}

function Invoke-JarvisUvSync {
    param([string]$Uv, [string]$Target)
    $envVars = @{ UV_PROJECT_ENVIRONMENT = (Get-JarvisVenvDir $Target) }
    return (Invoke-JarvisProcess -FilePath $Uv -ArgumentList (Get-JarvisUvSyncArguments) -WorkingDirectory $Target -Environment $envVars)
}

function Get-JarvisPythonCandidates {
    $list = New-Object System.Collections.Generic.List[object]
    if (Test-JarvisWindows) {
        $py = Get-Command -Name 'py' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($py) { $list.Add([pscustomobject]@{ Exe = [string]$py.Path; Args = @('-3') }) }
        foreach ($name in @('python', 'python3')) {
            $c = Get-Command -Name $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($c) { $list.Add([pscustomobject]@{ Exe = [string]$c.Path; Args = @() }) }
        }
        if ($env:LOCALAPPDATA) {
            # Standardort des python.org-Installers bei "nur fuer mich" (ohne PATH-Eintrag).
            $base = [System.IO.Path]::Combine($env:LOCALAPPDATA, 'Programs', 'Python')
            if ([System.IO.Directory]::Exists($base)) {
                $dirs = @(Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -like 'Python3*' } | Sort-Object -Property Name -Descending)
                foreach ($d in $dirs) {
                    $exe = Join-Path $d.FullName 'python.exe'
                    if ([System.IO.File]::Exists($exe)) { $list.Add([pscustomobject]@{ Exe = $exe; Args = @() }) }
                }
            }
        }
    } else {
        foreach ($name in @('python3', 'python')) {
            $c = Get-Command -Name $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($c) { $list.Add([pscustomobject]@{ Exe = [string]$c.Path; Args = @() }) }
        }
    }
    return $list.ToArray()
}

function Test-JarvisStorePythonPath {
    # Python aus dem Microsoft Store (MSIX-Paket): Schreibzugriffe nach %LOCALAPPDATA% landen dort in
    # ...\Packages\PythonSoftwareFoundation.Python.3.x_...\LocalCache - eine .venv waere fuer den
    # Installer (und Autostart/Startmenue) unsichtbar. Erkannt am gemeldeten Pfad (sys.executable bzw.
    # sys.base_prefix), nicht am Aufrufnamen: Runtimes des neuen "Python install manager" liegen normal.
    param([string]$Path)
    if (-not $Path) { return $false }
    $cmp = [System.StringComparison]::OrdinalIgnoreCase
    return ($Path.IndexOf('\WindowsApps\', $cmp) -ge 0 -or $Path.IndexOf('\Packages\PythonSoftwareFoundation.', $cmp) -ge 0)
}

function Find-JarvisPython {
    # Erstes Python >= 3.11 (ohne Microsoft-Store-Python); liefert den echten Pfad (sys.executable) und die Version.
    $code = 'import sys; print(sys.executable); print(sys.base_prefix); print(sys.version_info[0] * 100 + sys.version_info[1])'
    $storeSeen = New-Object System.Collections.Generic.List[string]
    foreach ($c in @(Get-JarvisPythonCandidates)) {
        $argList = @($c.Args) + @('-c', $code)
        $r = Invoke-JarvisCapture -FilePath $c.Exe -ArgumentList $argList -TimeoutSec 30
        if ($r.ExitCode -ne 0 -or @($r.Output).Count -lt 3) { continue }
        $number = 0
        if (-not [int]::TryParse(([string]$r.Output[-1]).Trim(), [ref]$number)) { continue }
        if ($number -lt 311) { continue }
        $exe = ([string]$r.Output[-3]).Trim()
        $base = ([string]$r.Output[-2]).Trim()
        if ((Test-JarvisStorePythonPath $exe) -or (Test-JarvisStorePythonPath $base)) {
            if (-not $storeSeen.Contains($exe)) {
                $storeSeen.Add($exe)
                Write-JarvisWarn (('Python aus dem Microsoft Store wird nicht verwendet ({0}): Windows legt dessen ' +
                        'Dateien unter AppData in einen eigenen, fuer den Installer unsichtbaren Ordner.') -f $exe)
            }
            continue
        }
        if (-not $exe) { $exe = $c.Exe }
        return [pscustomobject]@{ Path = $exe; Version = ('{0}.{1}' -f [math]::Floor($number / 100), ($number % 100)) }
    }
    return $null
}

function Get-JarvisPipCommands {
    # Fallback ohne uv: python -m venv + pip mit Hash-Pruefung (requirements.txt aus uv export).
    # Creates = Datei, die der Schritt anlegen muss (wird danach geprueft).
    param([string]$BasePython, [string]$Target)
    $venv = Get-JarvisVenvDir $Target
    $venvPython = Get-JarvisVenvPython -Target $Target
    $requirements = Join-Path $Target 'requirements.txt'
    $venvArgs = @('-m', 'venv', '--clear', $venv)
    $pipArgs = @('-m', 'pip', 'install', '--require-hashes', '--no-input', '--disable-pip-version-check', '-r', $requirements)
    return @(
        [pscustomobject]@{ FilePath = $BasePython; ArgumentList = $venvArgs; Display = (Format-JarvisCommand $BasePython $venvArgs); Creates = $venvPython },
        [pscustomobject]@{ FilePath = $venvPython; ArgumentList = $pipArgs; Display = (Format-JarvisCommand $venvPython $pipArgs); Creates = $null }
    )
}

function Get-JarvisNoPythonMessage {
    return ('Weder uv noch Python 3.11 (oder neuer) wurde gefunden. Zwei Moeglichkeiten: ' +
        '(1) am einfachsten: Install.cmd erneut starten und dem uv-Download zustimmen (oder Option -InstallUv) - ' +
        'uv bringt ein passendes Python mit. ' +
        '(2) Python 3.11 oder neuer nur fuer den eigenen Benutzer installieren: python.org-Installer, auf der ' +
        'ersten Seite den Haken bei "Use admin privileges when installing py.exe" entfernen, dann "Install Now" ' +
        '(oder der "Python install manager" von python.org) - danach Install.cmd erneut starten. ' +
        'Python aus dem Microsoft Store funktioniert hierfuer nicht.')
}

function Get-JarvisVenvHome {
    # Ordner des Basis-Interpreters der venv (Eintrag "home" in .venv\pyvenv.cfg), sonst $null. Unter
    # Windows lauscht dieser Interpreter (der venv-Starter in .venv\Scripts startet ihn als Kindprozess).
    param([string]$Target)
    $cfg = Join-Path (Get-JarvisVenvDir $Target) 'pyvenv.cfg'
    if (-not [System.IO.File]::Exists($cfg)) { return $null }
    try {
        foreach ($line in [System.IO.File]::ReadAllLines($cfg, [System.Text.Encoding]::UTF8)) {
            if ($line -match '^\s*home\s*=\s*(.+?)\s*$') { return $Matches[1] }
        }
    } catch { }
    return $null
}

function Initialize-JarvisPythonEnvironment {
    # Legt Target\.venv an. Liefert 'uv' oder 'pip'.
    param([string]$Target, [switch]$AssumeYes, [switch]$InstallUv, [switch]$NoUv)
    $venv = Get-JarvisVenvDir $Target
    if (Test-JarvisReparsePoint $venv) {
        throw ("'{0}' ist ein Link/Junction. Bitte selbst entfernen - der Installer veraendert nichts ausserhalb des Installationsordners." -f $venv)
    }
    $uv = $null
    $python = $null
    $pythonChecked = $false
    if ($NoUv) {
        Write-JarvisInfo 'uv wird nicht verwendet (-NoUv).'
    } else {
        $uv = Find-JarvisUv
        if ($uv) {
            Write-JarvisOk ('uv gefunden: {0} ({1})' -f $uv.Path, $uv.Version)
        } else {
            # Erst nachsehen, ob es ohne uv ueberhaupt geht - dann ist die Frage ehrlich.
            $python = Find-JarvisPython
            $pythonChecked = $true
            $cmd = Get-JarvisUvInstallCommand
            Write-JarvisInfo 'uv wurde nicht gefunden. uv richtet Python und alle Pakete fuer JARVIS ein'
            Write-JarvisInfo '(pro Benutzer nach %USERPROFILE%\.local\bin, ohne Adminrechte, PATH bleibt unveraendert;'
            Write-JarvisInfo 'fehlt ein passendes Python, laedt uv es von GitHub in den Ordner %APPDATA%\uv).'
            Write-JarvisInfo ('Dazu wird der offizielle Installer von astral.sh geladen: ' + $cmd.Display)
            if ($python) {
                Write-JarvisOk ('Python {0} gefunden: {1} - uv ist optional (ohne uv: python -m venv + pip).' -f $python.Version, $python.Path)
            } else {
                Write-JarvisWarn 'Kein Python 3.11 (oder neuer) gefunden - ohne uv kann JARVIS hier nicht eingerichtet werden.'
                Write-JarvisInfo 'Nein = abbrechen und Python selbst installieren (Hinweise folgen).'
            }
            $consent = [bool]$InstallUv
            if (-not $consent) {
                $consent = Read-JarvisYesNo 'uv jetzt herunterladen und installieren?' -Default $false -AssumeDefault:$AssumeYes
            }
            if ($consent) {
                $rc = Install-JarvisUv
                if ($rc -eq 0) { $uv = Find-JarvisUv }
                if ($uv) {
                    Write-JarvisOk ('uv installiert: {0}' -f $uv.Path)
                } else {
                    Write-JarvisWarn ('uv-Installation fehlgeschlagen (Exitcode {0}) - weiter mit python -m venv und pip.' -f $rc)
                }
            } elseif (-not $python) {
                # Ohne uv und ohne Python geht es nicht - gleich mit klarer Anleitung aufhoeren.
                throw (Get-JarvisNoPythonMessage)
            } else {
                Write-JarvisInfo 'Kein Download - weiter mit python -m venv und pip (requirements.txt mit Hashes).'
            }
        }
    }
    if ($uv) {
        Write-JarvisInfo 'uv sync --frozen --no-dev (Pakete aus uv.lock nach .venv; fehlt ein passendes Python, laedt uv es von GitHub) ...'
        $rc = Invoke-JarvisUvSync -Uv $uv.Path -Target $Target
        if ($rc -eq 0 -and [System.IO.File]::Exists((Get-JarvisVenvPython -Target $Target))) {
            Write-JarvisOk 'Python-Umgebung mit uv eingerichtet.'
            return 'uv'
        }
        Write-JarvisWarn ('uv sync ist fehlgeschlagen (Exitcode {0}) - versuche python -m venv und pip.' -f $rc)
    }
    $requirements = Join-Path $Target 'requirements.txt'
    if (-not [System.IO.File]::Exists($requirements)) {
        throw 'requirements.txt fehlt im Installationsordner - ohne uv koennen die Pakete nicht installiert werden.'
    }
    if (-not $pythonChecked) { $python = Find-JarvisPython }
    if (-not $python) { throw (Get-JarvisNoPythonMessage) }
    Write-JarvisOk ('Python {0} gefunden: {1}' -f $python.Version, $python.Path)
    foreach ($step in @(Get-JarvisPipCommands -BasePython $python.Path -Target $Target)) {
        Write-JarvisInfo ('> ' + $step.Display)
        $rc = Invoke-JarvisProcess -FilePath $step.FilePath -ArgumentList $step.ArgumentList -WorkingDirectory $Target
        if ($rc -ne 0) {
            throw (('Befehl fehlgeschlagen (Exitcode {0}): {1} - Meldung oben. Haeufige Ursachen: keine ' +
                    'Internetverbindung zu pypi.org, oder fuer Python {2} gibt es noch nicht alle Pakete als ' +
                    'fertigen Download. Abhilfe: Install.cmd erneut starten und dem uv-Download zustimmen ' +
                    '(uv nimmt ein passendes Python), oder Python 3.12 installieren.') -f $rc, $step.Display, $python.Version)
        }
        if ($step.Creates -and -not [System.IO.File]::Exists($step.Creates)) {
            throw (("'{0}' wurde nicht angelegt, obwohl der Befehl erfolgreich war. Das passiert mit Python aus dem " +
                    'Microsoft Store: Windows leitet dessen Schreibzugriffe nach AppData in einen eigenen Paketordner um. ' +
                    'Abhilfe: Install.cmd erneut starten und dem uv-Download zustimmen, oder Python von python.org ' +
                    'bzw. den "Python install manager" verwenden.') -f $step.Creates)
        }
    }
    Write-JarvisOk 'Python-Umgebung mit venv + pip eingerichtet.'
    return 'pip'
}

# ---------------------------------------------------------------------------------------------
# Einrichtungsassistent (python -m app.setup_wizard)
# ---------------------------------------------------------------------------------------------

function Get-JarvisWizardArguments {
    param([string]$Subcommand, [string[]]$Arguments = @())
    return @('-m', 'app.setup_wizard', $Subcommand) + @($Arguments)
}

function Invoke-JarvisWizard {
    # Laeuft interaktiv im selben Fenster. Exitcodes: 0 ok, 1 abgebrochen, 2 Fehler.
    param([string]$Python, [string]$Target, [string]$Subcommand, [string[]]$Arguments = @())
    $argList = Get-JarvisWizardArguments -Subcommand $Subcommand -Arguments $Arguments
    return (Invoke-JarvisProcess -FilePath $Python -ArgumentList $argList -WorkingDirectory $Target)
}

function Get-JarvisServerInfo {
    # {"bind","port","local_url","warnings"} aus "setup_wizard info"; $null bei Fehler.
    param([string]$Python, [string]$Target)
    $config = Join-Path $Target 'config.yaml'
    $argList = Get-JarvisWizardArguments -Subcommand 'info' -Arguments @('--config', $config)
    $r = Invoke-JarvisCapture -FilePath $Python -ArgumentList $argList -WorkingDirectory $Target -TimeoutSec 120
    if ($r.ExitCode -ne 0) {
        if ($r.ErrorText) { Write-JarvisWarn ($r.ErrorText.Trim()) }
        return $null
    }
    $line = @($r.Output | Where-Object { $_.TrimStart().StartsWith('{') }) | Select-Object -Last 1
    if (-not $line) { return $null }
    try { return ($line | ConvertFrom-Json) } catch { return $null }
}

# ---------------------------------------------------------------------------------------------
# Verknuepfungen: Startmenue und Autostart (nur Windows)
# ---------------------------------------------------------------------------------------------

function Get-JarvisStartMenuDir {
    return (Join-Path ([System.Environment]::GetFolderPath('Programs')) 'JARVIS')
}

function Get-JarvisStartupDir {
    return [System.Environment]::GetFolderPath('Startup')
}

function Get-JarvisShortcutRoots {
    # Nur hier (bzw. im Unterordner JARVIS) darf die Deinstallation Verknuepfungen loeschen.
    $roots = New-Object System.Collections.Generic.List[string]
    foreach ($folder in @('Programs', 'Startup')) {
        $value = [System.Environment]::GetFolderPath($folder)
        if ($value) { $roots.Add($value) }
    }
    return $roots.ToArray()
}

function Get-JarvisAutostartPath {
    return (Join-Path (Get-JarvisStartupDir) 'JARVIS.lnk')
}

function Get-JarvisWindowsPowerShellPath {
    if ($env:SystemRoot) {
        $full = [System.IO.Path]::Combine($env:SystemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
        if ([System.IO.File]::Exists($full)) { return $full }
    }
    return 'powershell.exe'
}

function New-JarvisShortcut {
    # .lnk ueber WScript.Shell. WindowStyle: 1 normal, 3 maximiert, 7 minimiert.
    param([string]$Path, [string]$TargetPath, [string]$Arguments = '', [string]$WorkingDirectory = '',
        [string]$Description = '', [int]$WindowStyle = 1)
    $shell = New-Object -ComObject WScript.Shell
    try {
        $shortcut = $shell.CreateShortcut($Path)
        $shortcut.TargetPath = $TargetPath
        $shortcut.Arguments = $Arguments
        if ($WorkingDirectory) { $shortcut.WorkingDirectory = $WorkingDirectory }
        $shortcut.WindowStyle = $WindowStyle
        if ($Description) { $shortcut.Description = $Description }
        $shortcut.Save()
    } finally {
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
    }
    return $Path
}

function Get-JarvisShortcutInfo {
    # Ziel, Argumente und Arbeitsordner einer vorhandenen .lnk (nur Windows), sonst $null.
    param([string]$Path)
    if (-not (Test-JarvisWindows) -or -not [System.IO.File]::Exists($Path)) { return $null }
    try {
        $shell = New-Object -ComObject WScript.Shell
        try {
            $lnk = $shell.CreateShortcut($Path)
            return [pscustomobject]@{
                TargetPath       = [string]$lnk.TargetPath
                Arguments        = [string]$lnk.Arguments
                WorkingDirectory = [string]$lnk.WorkingDirectory
            }
        } finally {
            [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
        }
    } catch {
        return $null
    }
}

function Get-JarvisShortcutTargetPath {
    # Ziel einer vorhandenen .lnk (nur Windows), sonst $null.
    param([string]$Path)
    $info = Get-JarvisShortcutInfo $Path
    if ($info) { return $info.TargetPath }
    return $null
}

function Test-JarvisShortcutOwnedBy {
    # Zeigt die .lnk (noch) auf diese Installation? Sonst gehoert sie inzwischen einer anderen
    # JARVIS-Installation (gleicher Name im Startmenue/Autostart) und bleibt liegen.
    # .url-Dateien und nicht lesbare .lnk gelten als eigene (sie stehen ja im Marker).
    param([string]$Path, [string]$Target)
    if (-not $Target) { return $true }
    if ([System.IO.Path]::GetExtension($Path) -ine '.lnk') { return $true }
    $info = Get-JarvisShortcutInfo $Path
    if (-not $info) { return $true }
    foreach ($p in @($info.TargetPath, $info.WorkingDirectory)) {
        if ($p -and (Test-JarvisPathUnder -Path $p -Root $Target -AllowEqual)) { return $true }
    }
    $full = Get-JarvisFullPath $Target
    foreach ($form in @(($full + '\'), ($full + '/'), ($full + '"'))) {
        if ($info.Arguments -and $info.Arguments.IndexOf($form, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
    }
    return $false
}

function New-JarvisUrlShortcut {
    param([string]$Path, [string]$Url)
    $content = "[InternetShortcut]`r`nURL=" + $Url + "`r`n"
    [System.IO.File]::WriteAllText($Path, $content, [System.Text.Encoding]::ASCII)
    return $Path
}

function Get-JarvisShortcutNames {
    $oe = [string][char]0x00F6
    return [pscustomobject]@{
        Open      = 'JARVIS ' + $oe + 'ffnen.url'
        Start     = 'JARVIS starten.lnk'
        Stop      = 'JARVIS beenden.lnk'
        Uninstall = 'JARVIS deinstallieren.lnk'
    }
}

function New-JarvisStartMenuShortcuts {
    # Legt den Startmenue-Ordner JARVIS an und liefert die Pfade der Verknuepfungen.
    param([string]$Target, [string]$LocalUrl)
    $dir = Get-JarvisStartMenuDir
    if (-not [System.IO.Directory]::Exists($dir)) { [void][System.IO.Directory]::CreateDirectory($dir) }
    $names = Get-JarvisShortcutNames
    $created = New-Object System.Collections.Generic.List[string]

    $path = Join-Path $dir $names.Open
    [void](New-JarvisUrlShortcut -Path $path -Url $LocalUrl)
    $created.Add($path)

    # Ueber scripts\start-hidden.ps1: startet den Server ohne Fenster, wartet auf /api/health, oeffnet
    # die Oberflaeche und zeigt bei Problemen die letzten Log-Zeilen (statt still nichts zu tun).
    $startScript = Join-Path (Join-Path $Target 'scripts') 'start-hidden.ps1'
    $path = Join-Path $dir $names.Start
    [void](New-JarvisShortcut -Path $path -TargetPath (Get-JarvisWindowsPowerShellPath) `
        -Arguments (Get-JarvisScriptShortcutArguments -Script $startScript) -WorkingDirectory $Target `
        -Description 'JARVIS-Server starten und die Oberflaeche oeffnen')
    $created.Add($path)

    $stopScript = Join-Path (Join-Path $Target 'scripts') 'stop.ps1'
    $path = Join-Path $dir $names.Stop
    [void](New-JarvisShortcut -Path $path -TargetPath (Get-JarvisWindowsPowerShellPath) `
        -Arguments (Get-JarvisStopShortcutArguments -StopScript $stopScript) -WorkingDirectory $Target `
        -Description 'JARVIS-Server beenden')
    $created.Add($path)

    $path = Join-Path $dir $names.Uninstall
    [void](New-JarvisShortcut -Path $path -TargetPath (Join-Path $Target 'Uninstall.cmd') `
        -WorkingDirectory (Get-JarvisHomeDir) -Description 'JARVIS deinstallieren')
    $created.Add($path)
    return $created.ToArray()
}

function Get-JarvisStopShortcutArguments {
    param([string]$StopScript)
    return ('-NoProfile -ExecutionPolicy Bypass -File "{0}" -Pause' -f $StopScript)
}

function Get-JarvisScriptShortcutArguments {
    param([string]$Script)
    return ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $Script)
}

function New-JarvisAutostart {
    # Verknuepfung im Autostart-Ordner: .venv\Scripts\pythonw.exe -m app (Fallback python.exe minimiert).
    param([string]$Target)
    $exe = Get-JarvisVenvPython -Target $Target -Windowed
    if (-not [System.IO.File]::Exists($exe)) {
        throw ("Keine virtuelle Umgebung gefunden ({0}). Zuerst Install.cmd ausfuehren." -f $exe)
    }
    $link = Get-JarvisAutostartPath
    $dir = [System.IO.Path]::GetDirectoryName($link)
    if (-not [System.IO.Directory]::Exists($dir)) { [void][System.IO.Directory]::CreateDirectory($dir) }
    [void](New-JarvisShortcut -Path $link -TargetPath $exe -Arguments '-m app' -WorkingDirectory $Target `
        -WindowStyle 7 -Description 'JARVIS - lokaler Assistent')
    return [pscustomobject]@{
        Path       = $link
        Executable = $exe
        Fallback   = ([System.IO.Path]::GetFileName($exe) -ieq 'python.exe')
    }
}

function Test-JarvisShortcutPathAllowed {
    # Nur .lnk/.url direkt im Startmenue-/Autostart-Ordner oder in deren Unterordner JARVIS.
    param([string]$Path)
    if (-not $Path) { return $false }
    try {
        $ext = [System.IO.Path]::GetExtension($Path).ToLowerInvariant()
        $full = Get-JarvisFullPath $Path
    } catch {
        return $false
    }
    if ($ext -ne '.lnk' -and $ext -ne '.url') { return $false }
    $parent = [System.IO.Path]::GetDirectoryName($full)
    foreach ($root in @(Get-JarvisShortcutRoots)) {
        if (Test-JarvisSamePath $parent $root) { return $true }
        if ([System.IO.Path]::GetFileName($parent) -eq 'JARVIS') {
            if (Test-JarvisSamePath ([System.IO.Path]::GetDirectoryName($parent)) $root) { return $true }
        }
    }
    return $false
}

function Remove-JarvisShortcutFiles {
    # Loescht die Verknuepfungen aus dem Manifest und danach einen leeren Startmenue-Ordner JARVIS.
    # Mit -Owner bleiben .lnk liegen, die inzwischen auf eine andere Installation zeigen.
    param([string[]]$Paths, [string]$Owner)
    $result = New-JarvisRemovalResult
    $dirs = New-Object System.Collections.Generic.List[string]
    foreach ($p in @($Paths | Where-Object { $_ })) {
        if (-not (Test-JarvisShortcutPathAllowed $p)) { $result.Skipped.Add($p); continue }
        $full = Get-JarvisFullPath $p
        $parent = [System.IO.Path]::GetDirectoryName($full)
        if ([System.IO.Path]::GetFileName($parent) -eq 'JARVIS' -and -not $dirs.Contains($parent)) { $dirs.Add($parent) }
        if (-not [System.IO.File]::Exists($full)) { continue }
        if ($Owner -and -not (Test-JarvisShortcutOwnedBy -Path $full -Target $Owner)) { $result.Foreign.Add($full); continue }
        try {
            [System.IO.File]::Delete($full)
            $result.Removed.Add($full)
        } catch {
            $result.Failed.Add(('{0} ({1})' -f $full, (Get-JarvisErrorMessage $_)))
        }
    }
    foreach ($d in $dirs) {
        if ([System.IO.Directory]::Exists($d) -and [System.IO.Directory]::GetFileSystemEntries($d).Length -eq 0) {
            try { [System.IO.Directory]::Delete($d, $false); $result.Removed.Add($d) }
            catch { $result.Failed.Add(('{0} ({1})' -f $d, (Get-JarvisErrorMessage $_))) }
        }
    }
    return $result
}

function Remove-JarvisAutostart {
    # Entfernt die Autostart-Verknuepfung (Standard: Autostart-Ordner\JARVIS.lnk), aber nur, wenn
    # sie auf -Owner (den Installationsordner) zeigt. Ergebnis: 'removed', 'missing' oder 'foreign'.
    param([string]$Path, [string]$Owner)
    if (-not $Path) { $Path = Get-JarvisAutostartPath }
    $r = Remove-JarvisShortcutFiles -Paths @($Path) -Owner $Owner
    if ($r.Failed.Count -gt 0) { throw ('Autostart-Eintrag konnte nicht entfernt werden: ' + $r.Failed[0]) }
    if ($r.Removed.Count -gt 0) { return 'removed' }
    if ($r.Foreign.Count -gt 0) { return 'foreign' }
    return 'missing'
}

# ---------------------------------------------------------------------------------------------
# Server finden, stoppen, starten
# ---------------------------------------------------------------------------------------------

function Get-JarvisStateDirs {
    param([string]$Target)
    $dirs = New-Object System.Collections.Generic.List[string]
    $dirs.Add((Join-Path $Target 'state'))
    if ($env:JARVIS_STATE) { $dirs.Add($env:JARVIS_STATE) }
    return $dirs.ToArray()
}

function ConvertTo-JarvisUtcDate {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [DateTime]) { return $Value.ToUniversalTime() }
    $parsed = [DateTimeOffset]::MinValue
    $styles = [System.Globalization.DateTimeStyles]::AssumeLocal
    if ([DateTimeOffset]::TryParse([string]$Value, [System.Globalization.CultureInfo]::InvariantCulture, $styles, [ref]$parsed)) {
        return $parsed.UtcDateTime
    }
    return $null
}

function ConvertFrom-JarvisEpoch {
    # Sekunden seit 1970 (z. B. psutil create_time) -> DateTime (UTC); $null, wenn ungueltig.
    param($Value)
    $seconds = 0.0
    if ($null -eq $Value -or -not [double]::TryParse([string]$Value, [System.Globalization.NumberStyles]::Float,
            [System.Globalization.CultureInfo]::InvariantCulture, [ref]$seconds)) { return $null }
    if ($seconds -le 0) { return $null }
    return [DateTimeOffset]::FromUnixTimeMilliseconds([long]($seconds * 1000)).UtcDateTime
}

function Read-JarvisPidFile {
    # state\server.pid (von python -m app geschrieben): {"pid", "executable", "started", "create_time"}.
    param([string]$Target)
    foreach ($dir in @(Get-JarvisStateDirs $Target)) {
        $file = Join-Path $dir 'server.pid'
        if (-not [System.IO.File]::Exists($file)) { continue }
        try { $data = [System.IO.File]::ReadAllText($file, [System.Text.Encoding]::UTF8) | ConvertFrom-Json } catch { continue }
        if ($null -eq $data) { continue }
        $id = 0
        if (-not [int]::TryParse([string]$data.pid, [ref]$id) -or $id -le 0) { continue }
        return [pscustomobject]@{
            Pid        = $id
            Executable = [string]$data.executable
            Started    = (ConvertTo-JarvisUtcDate $data.started)
            CreateTime = (ConvertFrom-JarvisEpoch $data.create_time)
            File       = $file
        }
    }
    return $null
}

function Get-JarvisProcessDetails {
    param([int]$Id)
    if (Test-JarvisWindows) {
        $p = Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId={0}' -f $Id) -ErrorAction SilentlyContinue
        if (-not $p) { return $null }
        $parentPath = $null
        if ($p.ParentProcessId) {
            $pp = Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId={0}' -f $p.ParentProcessId) -ErrorAction SilentlyContinue
            if ($pp) { $parentPath = $pp.ExecutablePath }
        }
        $start = $null
        if ($p.CreationDate) { $start = ([DateTime]$p.CreationDate).ToUniversalTime() }
        return [pscustomobject]@{
            Id = $Id; Name = [string]$p.Name; Path = [string]$p.ExecutablePath; CommandLine = [string]$p.CommandLine
            ParentPath = $parentPath; StartTimeUtc = $start
        }
    }
    $proc = Get-Process -Id $Id -ErrorAction SilentlyContinue
    if (-not $proc) { return $null }
    $commandLine = $null
    $parentPath = $null
    $start = $null
    try { $commandLine = [string]$proc.CommandLine } catch { }
    try { if ($proc.Parent) { $parentPath = [string]$proc.Parent.Path } } catch { }
    try { $start = $proc.StartTime.ToUniversalTime() } catch { }
    return [pscustomobject]@{
        Id = $Id; Name = [string]$proc.ProcessName; Path = [string]$proc.Path; CommandLine = $commandLine
        ParentPath = $parentPath; StartTimeUtc = $start
    }
}

function Get-JarvisPythonProcessDetails {
    # Alle laufenden Python-Prozesse (Windows: eine CIM-Abfrage, sonst Get-Process).
    $result = New-Object System.Collections.Generic.List[object]
    if (Test-JarvisWindows) {
        $all = @(Get-CimInstance -ClassName Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue)
        $paths = @{}
        foreach ($p in $all) { $paths[[int]$p.ProcessId] = [string]$p.ExecutablePath }
        foreach ($p in $all) {
            $parentPath = $null
            if ($paths.ContainsKey([int]$p.ParentProcessId)) { $parentPath = $paths[[int]$p.ParentProcessId] }
            $start = $null
            if ($p.CreationDate) { $start = ([DateTime]$p.CreationDate).ToUniversalTime() }
            $result.Add([pscustomobject]@{
                    Id = [int]$p.ProcessId; Name = [string]$p.Name; Path = [string]$p.ExecutablePath
                    CommandLine = [string]$p.CommandLine; ParentPath = $parentPath; StartTimeUtc = $start
                })
        }
    } else {
        foreach ($proc in @(Get-Process -Name 'python*' -ErrorAction SilentlyContinue)) {
            $d = Get-JarvisProcessDetails -Id $proc.Id
            if ($d) { $result.Add($d) }
        }
    }
    return $result.ToArray()
}

function Get-JarvisCommandLineExe {
    # Erstes Element einer Kommandozeile (Programmpfad), mit oder ohne Anfuehrungszeichen.
    param([string]$CommandLine)
    if (-not $CommandLine) { return $null }
    $c = $CommandLine.TrimStart()
    if ($c.StartsWith('"')) {
        $end = $c.IndexOf('"', 1)
        if ($end -gt 0) { return $c.Substring(1, $end - 1) }
        return $c.Substring(1)
    }
    $space = $c.IndexOf(' ')
    if ($space -gt 0) { return $c.Substring(0, $space) }
    return $c
}

function Test-JarvisServerCommandLine {
    param([string]$CommandLine)
    if (-not $CommandLine) { return $false }
    return ($CommandLine -match '(^|\s)-m\s+app(\s|$)')
}

function Test-JarvisOwnServerProcess {
    # Gehoert der Prozess zum JARVIS-Server dieses Ziels? Pruefung ueber den Programmpfad in der
    # venv (direkt, laut Kommandozeile oder ueber den venv-Starter als Elternprozess unter Windows)
    # bzw. ueber die PID-Datei mit passender Startzeit (Schutz gegen wiederverwendete PIDs).
    param($Details, [string]$Target, $PidInfo)
    if ($null -eq $Details) { return $false }
    if ([int]$Details.Id -eq $PID) { return $false }
    $venv = Get-JarvisVenvDir $Target
    if (Test-JarvisServerCommandLine $Details.CommandLine) {
        foreach ($exe in @($Details.Path, (Get-JarvisCommandLineExe $Details.CommandLine), $Details.ParentPath)) {
            if ($exe -and (Test-JarvisPathUnder -Path $exe -Root $venv)) { return $true }
        }
    }
    if ($PidInfo -and [int]$PidInfo.Pid -eq [int]$Details.Id -and $PidInfo.Executable -and
        (Test-JarvisPathUnder -Path $PidInfo.Executable -Root $venv)) {
        if ($Details.Name -notlike 'python*') { return $false }
        $createTime = $null
        if (@($PidInfo.PSObject.Properties.Name) -contains 'CreateTime') { $createTime = $PidInfo.CreateTime }
        if ($createTime -and $Details.StartTimeUtc) {
            # Genaue Startzeit des Prozesses (psutil) - muss auf 2 s passen.
            return ([Math]::Abs(($createTime - $Details.StartTimeUtc).TotalSeconds) -le 2)
        }
        if ($PidInfo.Started -and $Details.StartTimeUtc) {
            $delta = ($PidInfo.Started - $Details.StartTimeUtc).TotalSeconds
            if ($delta -ge -5 -and $delta -le 600) { return $true }
        }
    }
    return $false
}

function Find-JarvisServerProcesses {
    # PID-Datei zuerst, danach Suche ueber alle Python-Prozesse (z. B. ohne PID-Datei).
    param([string]$Target)
    $found = New-Object System.Collections.Generic.List[object]
    $ids = New-Object System.Collections.Generic.List[int]
    $pidInfo = Read-JarvisPidFile $Target
    if ($pidInfo -and (Test-JarvisProcessAlive $pidInfo.Pid)) {
        $d = Get-JarvisProcessDetails -Id $pidInfo.Pid
        if (Test-JarvisOwnServerProcess -Details $d -Target $Target -PidInfo $pidInfo) {
            $found.Add($d)
            $ids.Add([int]$d.Id)
        }
    }
    foreach ($d in @(Get-JarvisPythonProcessDetails)) {
        if ($ids.Contains([int]$d.Id)) { continue }
        if ((Test-JarvisOwnServerProcess -Details $d -Target $Target -PidInfo $null) -and (Test-JarvisProcessAlive $d.Id)) {
            $found.Add($d)
            $ids.Add([int]$d.Id)
        }
    }
    return $found.ToArray()
}

function Test-JarvisProcessAlive {
    param([int]$Id)
    $proc = Get-Process -Id $Id -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    if (-not (Test-JarvisWindows)) {
        # Zombies (beendet, aber vom Elternprozess noch nicht abgeholt) zaehlen als beendet.
        try {
            $stat = [System.IO.File]::ReadAllText(('/proc/{0}/stat' -f $Id))
            $close = $stat.LastIndexOf(')')
            if ($close -gt 0 -and $stat.Length -gt $close + 2 -and $stat[$close + 2] -eq [char]'Z') { return $false }
        } catch { }
    }
    return $true
}

function Stop-JarvisServer {
    # Beendet den Server dieses Ziels. Liefert die Zahl der beendeten Prozesse.
    param([string]$Target, [int]$TimeoutSec = 10)
    $procs = @(Find-JarvisServerProcesses -Target $Target)
    if ($procs.Count -eq 0) { return 0 }
    foreach ($d in $procs) {
        try {
            Stop-Process -Id $d.Id -Force -ErrorAction Stop
            Write-JarvisInfo ('Beendet: PID {0} ({1})' -f $d.Id, $d.Name)
        } catch {
            if (Test-JarvisProcessAlive $d.Id) {
                Write-JarvisWarn ('PID {0} konnte nicht beendet werden: {1}' -f $d.Id, $_.Exception.Message)
            }
        }
    }
    # Warten, bis die Prozesse weg sind - sonst sind Dateien in .venv noch gesperrt.
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    foreach ($d in $procs) {
        while ((Test-JarvisProcessAlive $d.Id) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
    }
    return $procs.Count
}

function Get-JarvisServerLogHint {
    param([string]$Target)
    $logs = Join-Path (Join-Path $Target 'state') 'logs'
    if (Test-JarvisWindows) { return (Join-Path $logs 'server.log') }
    return (Join-Path $logs 'server-stderr.log')
}

function New-JarvisServerStartInfo {
    # Windows: pythonw.exe -m app ohne Fenster (Fallback python.exe minimiert). ProcessStartInfo nimmt den
    # Arbeitsordner woertlich - Start-Process -WorkingDirectory wuerde [ ] als Platzhalter auswerten.
    param([string]$Target)
    $exe = Get-JarvisVenvPython -Target $Target -Windowed
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = '-m app'
    $psi.WorkingDirectory = $Target
    $psi.UseShellExecute = $true
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    if ([System.IO.Path]::GetFileName($exe) -ieq 'python.exe') { $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Minimized }
    return $psi
}

function Start-JarvisServer {
    # Windows: pythonw.exe -m app ohne Fenster. Testmodus: venv-Python im Hintergrund mit Log-Dateien
    # (ueber /bin/sh, damit Pfade mit [ ] woertlich bleiben und der Server die Ausgabe-Pipes des
    # Installers nicht erbt).
    param([string]$Target)
    if (Test-JarvisWindows) {
        return [System.Diagnostics.Process]::Start((New-JarvisServerStartInfo -Target $Target))
    }
    $exe = Get-JarvisVenvPython -Target $Target
    $logs = Join-Path (Join-Path $Target 'state') 'logs'
    if (-not [System.IO.Directory]::Exists($logs)) { [void][System.IO.Directory]::CreateDirectory($logs) }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = '/bin/sh'
    $psi.Arguments = ConvertTo-JarvisArgString @('-c', 'exec "$0" -m app <"/dev/null" >"$1" 2>"$2"', $exe,
        (Join-Path $logs 'server-stdout.log'), (Join-Path $logs 'server-stderr.log'))
    $psi.WorkingDirectory = $Target
    $psi.UseShellExecute = $false
    return [System.Diagnostics.Process]::Start($psi)
}

function Get-JarvisLogTail {
    # Die letzten Zeilen einer Log-Datei (auch wenn der Server sie noch offen hat); leer, wenn es sie nicht gibt.
    param([string]$Path, [int]$Lines = 10)
    if (-not [System.IO.File]::Exists($Path)) { return @() }
    try {
        $stream = New-Object System.IO.FileStream($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
            ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
        try {
            $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
            $all = @($reader.ReadToEnd() -split "`r?`n" | Where-Object { $_.Trim() -ne '' })
        } finally {
            $stream.Dispose()
        }
    } catch {
        return @()
    }
    if ($all.Count -le $Lines) { return $all }
    return @($all[($all.Count - $Lines)..($all.Count - 1)])
}

function Get-JarvisStartProblem {
    # Uebersetzt typische Fehler aus dem Server-Log in einen Hinweis fuer Laien ($null = nichts erkannt).
    param([string[]]$LogLines, [int]$Port)
    $text = (@($LogLines) -join "`n")
    if ($text -match 'Errno 10048|Errno 98|Errno 48|address already in use|Address already in use|only one usage of each socket address') {
        return ('Port {0} ist belegt (ein anderes Programm oder eine alte JARVIS-Version). Abhilfe: das andere ' +
            'Programm beenden - oder Install.cmd erneut starten, "Konfiguration jetzt anpassen?" mit j beantworten ' +
            'und im letzten Schritt einen anderen Port waehlen.') -f $Port
    }
    if ($text -match 'JARVIS kann nicht starten') {
        return 'config.yaml ist fehlerhaft (siehe Meldung oben). Datei korrigieren oder Install.cmd erneut starten.'
    }
    if ($text -match 'bereits \(PID') {
        return 'JARVIS laeuft bereits (aus diesem Ordner) - Startmenue > JARVIS > JARVIS beenden, dann neu starten.'
    }
    return $null
}

function Get-JarvisOtherServers {
    # Laufende "python -m app"-Prozesse, die NICHT zu diesem Ziel gehoeren (nur lesen). Liefert je Prozess
    # PID und vermuteten Ordner (der Ordner ueber .venv bzw. der Programmpfad).
    param([string]$Target)
    $result = New-Object System.Collections.Generic.List[object]
    foreach ($d in @(Get-JarvisPythonProcessDetails)) {
        if ([int]$d.Id -eq $PID) { continue }
        if (-not (Test-JarvisServerCommandLine $d.CommandLine)) { continue }
        if (Test-JarvisOwnServerProcess -Details $d -Target $Target -PidInfo $null) { continue }
        $folder = $null
        foreach ($exe in @($d.ParentPath, (Get-JarvisCommandLineExe $d.CommandLine), $d.Path)) {
            if (-not $exe) { continue }
            $idx = $exe.IndexOf('.venv', [System.StringComparison]::OrdinalIgnoreCase)
            if ($idx -gt 1) { $folder = $exe.Substring(0, $idx - 1); break }
        }
        if (-not $folder) { $folder = [string]$d.Path }
        $result.Add([pscustomobject]@{ Id = [int]$d.Id; Folder = $folder })
    }
    return $result.ToArray()
}

function Test-JarvisHealth {
    param([string]$Url, [int]$TimeoutSec = 2)
    $ProgressPreference = 'SilentlyContinue'
    $params = @{ Uri = $Url; UseBasicParsing = $true; TimeoutSec = $TimeoutSec; ErrorAction = 'Stop' }
    if ($PSVersionTable.PSVersion.Major -ge 6) { $params['NoProxy'] = $true }
    try {
        $response = Invoke-WebRequest @params
        return ([int]$response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Wait-JarvisHealth {
    param([string]$Url, [int]$TimeoutSec = 20, $Process)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-JarvisHealth -Url $Url) { return $true }
        if ($Process -and $Process.HasExited) { return $false }
        Start-Sleep -Milliseconds 500
    }
    return (Test-JarvisHealth -Url $Url)
}

# ---------------------------------------------------------------------------------------------
# Abschluss: Adressen, Firewall-Hinweise
# ---------------------------------------------------------------------------------------------

function Test-JarvisPrivateIPv4 {
    param([string]$Address)
    $ip = $null
    if (-not [System.Net.IPAddress]::TryParse($Address, [ref]$ip)) { return $false }
    if ($ip.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { return $false }
    $b = $ip.GetAddressBytes()
    if ($b[0] -eq 10) { return $true }
    if ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) { return $true }
    return ($b[0] -eq 192 -and $b[1] -eq 168)
}

function Test-JarvisTailscaleIPv4 {
    # 100.64.0.0/10 (Adressbereich von Tailscale, CGNAT).
    param([string]$Address)
    $ip = $null
    if (-not [System.Net.IPAddress]::TryParse($Address, [ref]$ip)) { return $false }
    if ($ip.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { return $false }
    $b = $ip.GetAddressBytes()
    return ($b[0] -eq 100 -and $b[1] -ge 64 -and $b[1] -le 127)
}

function Get-JarvisIPv4Interfaces {
    # IPv4-Adressen dieses PCs mit Adapternamen (nur lesen, nur Windows). Tests ersetzen diese Funktion.
    if (-not (Test-JarvisWindows)) { return @() }
    $list = New-Object System.Collections.Generic.List[object]
    try {
        foreach ($a in @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop)) {
            # Nur gueltige Adressen ("Preferred" = Wert 4); "Tentative", "Duplicate" usw. ueberspringen.
            $state = [string]$a.AddressState
            if ($state -and @('Preferred', '4') -notcontains $state) { continue }
            $list.Add([pscustomobject]@{ Address = [string]$a.IPAddress; Interface = [string]$a.InterfaceAlias })
        }
    } catch {
        # Fallback ohne das NetTCPIP-Modul.
        try {
            foreach ($nic in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
                if ($nic.OperationalStatus -ne [System.Net.NetworkInformation.OperationalStatus]::Up) { continue }
                foreach ($u in $nic.GetIPProperties().UnicastAddresses) {
                    $list.Add([pscustomobject]@{ Address = $u.Address.ToString(); Interface = [string]$nic.Name })
                }
            }
        } catch { }
    }
    return $list.ToArray()
}

function Get-JarvisLanAddresses {
    # Private IPv4-Adressen (10/8, 172.16/12, 192.168/16) mit Adapternamen, z. B. fuers Handy im WLAN.
    $seen = New-Object System.Collections.Generic.List[string]
    $result = New-Object System.Collections.Generic.List[object]
    foreach ($a in @(Get-JarvisIPv4Interfaces)) {
        if (-not (Test-JarvisPrivateIPv4 $a.Address) -or $seen.Contains($a.Address)) { continue }
        $seen.Add($a.Address)
        $result.Add($a)
    }
    return @($result.ToArray() | Sort-Object -Property Address)
}

function Get-JarvisTailscaleIp {
    # Nur lesend: "tailscale ip -4", falls das Tailscale-Programm vorhanden ist; sonst die
    # 100.x-Adresse des Tailscale-Adapters.
    $exe = $null
    $cmd = Get-Command -Name 'tailscale' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) {
        $exe = [string]$cmd.Path
    } elseif ((Test-JarvisWindows) -and $env:ProgramFiles) {
        $candidate = [System.IO.Path]::Combine($env:ProgramFiles, 'Tailscale', 'tailscale.exe')
        if ([System.IO.File]::Exists($candidate)) { $exe = $candidate }
    }
    if ($exe) {
        $r = Invoke-JarvisCapture -FilePath $exe -ArgumentList @('ip', '-4') -TimeoutSec 10
        if ($r.ExitCode -eq 0) {
            $ip = @($r.Output | ForEach-Object { $_.Trim() } | Where-Object { Test-JarvisTailscaleIPv4 $_ }) | Select-Object -First 1
            if ($ip) { return $ip }
        }
    }
    $fromAdapter = @(Get-JarvisIPv4Interfaces | Where-Object { $_.Interface -like '*Tailscale*' -and (Test-JarvisTailscaleIPv4 $_.Address) }) |
        Select-Object -First 1
    if ($fromAdapter) { return $fromAdapter.Address }
    return $null
}

function ConvertTo-JarvisPsLiteral {
    # Wert fuer '...' in einem angezeigten PowerShell-Befehl (einfache Anfuehrungszeichen verdoppelt).
    param([string]$Value)
    return ("'" + $Value.Replace("'", "''") + "'")
}

function Get-JarvisFirewallBlockCleanup {
    # Entfernt eingehende BLOCK-Regeln, die Windows beim ersten Start fuer Python anlegt, wenn im Dialog
    # "Zugriff zulassen?" Abbrechen geklickt wird (oder ohne Adminrechte). Block-Regeln gehen vor
    # Allow-Regeln - ohne das Aufraeumen hilft die Port-Regel nicht. Wird nur angezeigt, nie ausgefuehrt.
    param([string]$PythonHome)
    $pattern = '*\python*.exe'
    if ($PythonHome) {
        $pattern = [System.Management.Automation.WildcardPattern]::Escape($PythonHome.TrimEnd([char[]]@([char]'\', [char]'/'))) + '\python*.exe'
    }
    return ('Get-NetFirewallApplicationFilter | Where-Object {{ $_.Program -like {0} }} | Get-NetFirewallRule | ' +
        "Where-Object {{ `$_.Direction -eq 'Inbound' -and `$_.Action -eq 'Block' }} | Remove-NetFirewallRule") -f (ConvertTo-JarvisPsLiteral $pattern)
}

function Get-JarvisFirewallCommands {
    # Dieselben Befehle wie in README Abschnitt 5 - werden nur angezeigt, nie ausgefuehrt.
    # -PythonHome: Ordner des Python, das JARVIS wirklich ausfuehrt (aus .venv\pyvenv.cfg).
    param([int]$Port = 8765, [string]$PythonHome)
    return @(
        ('New-NetFirewallRule -DisplayName "JARVIS {0} (LAN)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort {0} -Profile Private -RemoteAddress LocalSubnet' -f $Port),
        ('New-NetFirewallRule -DisplayName "JARVIS {0} (Tailscale)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort {0} -InterfaceAlias "Tailscale" -RemoteAddress 100.64.0.0/10' -f $Port),
        (Get-JarvisFirewallBlockCleanup -PythonHome $PythonHome)
    )
}

function Get-JarvisPublicNetworks {
    # Nur lesen: Namen der Netzwerke mit Profil "Oeffentlich" (dort greift die LAN-Regel nicht). Nur Windows.
    if (-not (Test-JarvisWindows)) { return @() }
    try {
        return @(Get-NetConnectionProfile -ErrorAction Stop | Where-Object { [string]$_.NetworkCategory -eq 'Public' } |
                ForEach-Object { '{0} ({1})' -f $_.Name, $_.InterfaceAlias })
    } catch {
        return @()
    }
}
