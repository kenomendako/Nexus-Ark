# Nexus Ark: repair the dependency downloader before it performs HTTPS requests.
# Only the SHA256-pinned official Windows x64 archive is accepted.
[CmdletBinding()]
param(
    [string]$InstallDirectory = (Join-Path $env:USERPROFILE '.local\bin'),
    [string]$ArchivePath = '',
    [switch]$CheckOnly
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$env:PATHEXT = '.COM;.EXE;.BAT;.CMD'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$MinimumVersion = [version]'0.13.4'
$PinnedVersion = '0.13.4'
$ArchiveHash = 'ccec7508a07d9933d7cb1261880633efcf1f30d6e6eddab21823f1dae6f23225'
$ArchiveUrl = 'https://github.com/astral-sh/uv/releases/download/0.13.4/uv-x86_64-pc-windows-msvc.zip'
$OwnedTemporary = ''
$InstallLock = $null
$Changed = @()
$BackupDirectory = ''

function Assert-RegularPath([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'A download-tool path is a link. Stopping without changing it.'
            }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
}

function Complete-PythonPreparation([string]$UvExecutable) {
    # CheckOnly remains a local check of the downloader itself.
    if ($CheckOnly) { return }
    $helper = Join-Path $PSScriptRoot 'EnsureSecurePython.ps1'
    Assert-RegularPath $helper
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
        throw 'The secure Python preparation tool is missing. Use the complete new ZIP.'
    }
    $powershell = Join-Path $PSHOME 'powershell.exe'
    & $powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $helper -UvExecutable $UvExecutable
    if ($LASTEXITCODE -ne 0) { throw 'Secure Python preparation failed. The application has not been started.' }
}

function Read-UvVersion([string]$Executable) {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $null }
    Assert-RegularPath $Executable
    $output = & $Executable --version
    if ($LASTEXITCODE -ne 0) { return $null }
    $match = [regex]::Match(($output -join "`n"), '^uv ([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)')
    if (-not $match.Success) { return $null }
    return [version]$match.Groups[1].Value
}

try {
    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
        throw 'This repair tool supports Windows x64 only.'
    }
    $InstallDirectory = [IO.Path]::GetFullPath($InstallDirectory)
    Assert-RegularPath $InstallDirectory
    $Executable = Join-Path $InstallDirectory 'uv.exe'
    $version = Read-UvVersion $Executable
    if ($null -ne $version -and $version -ge $MinimumVersion) {
        Write-Host "[Nexus Ark] Download tool ready: uv $version ($Executable)"
        Complete-PythonPreparation $Executable
        exit 0
    }
    if ($CheckOnly) { throw 'uv 0.13.4 or newer is required before downloading dependencies.' }
    New-Item -ItemType Directory -Path $InstallDirectory -Force | Out-Null
    Assert-RegularPath $InstallDirectory
    $InstallLock = [IO.File]::Open((Join-Path $InstallDirectory '.nexus-uv-update.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $OwnedTemporary = Join-Path ([IO.Path]::GetTempPath()) ('nexus-uv-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $OwnedTemporary | Out-Null
    if ($ArchivePath) {
        $archive = [IO.Path]::GetFullPath($ArchivePath)
        Assert-RegularPath $archive
    } else {
        $archive = Join-Path $OwnedTemporary 'uv.zip'
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Write-Host '[Nexus Ark] Downloading the official certificate-fixed update tool...'
        Invoke-WebRequest -UseBasicParsing -Uri $ArchiveUrl -OutFile $archive
    }
    $actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $ArchiveHash) { throw 'Official download SHA256 mismatch. Nothing has been installed.' }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    try {
        $expected = @('uv.exe', 'uvx.exe', 'uvw.exe')
        if ($zip.Entries.Count -ne $expected.Count) { throw 'Unexpected official archive layout.' }
        foreach ($entry in $zip.Entries) {
            if ($entry.FullName -cnotin $expected) { throw 'Unexpected archive entry.' }
        }
    } finally { $zip.Dispose() }
    $expanded = Join-Path $OwnedTemporary 'expanded'
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $expanded)
    $newVersion = Read-UvVersion (Join-Path $expanded 'uv.exe')
    if ($null -eq $newVersion -or $newVersion.ToString() -ne $PinnedVersion) {
        throw 'Official download version mismatch. Nothing has been installed.'
    }
    $BackupDirectory = Join-Path $InstallDirectory ('.nexus-uv-backup-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $BackupDirectory | Out-Null
    foreach ($name in @('uv.exe', 'uvx.exe', 'uvw.exe')) {
        $destination = Join-Path $InstallDirectory $name
        Assert-RegularPath $destination
        $existed = Test-Path -LiteralPath $destination -PathType Leaf
        if ($existed) {
            Copy-Item -LiteralPath $destination -Destination (Join-Path $BackupDirectory $name)
        }
        $next = Join-Path $InstallDirectory ($name + '.nexus-next-' + [guid]::NewGuid().ToString('N'))
        try {
            Copy-Item -LiteralPath (Join-Path $expanded $name) -Destination $next
            if ($existed) { [IO.File]::Replace($next, $destination, (Join-Path $BackupDirectory ($name + ".replaced"))) }
            else { [IO.File]::Move($next, $destination) }
            $Changed += [pscustomobject]@{ Name = $name; Existed = $existed }
        } finally {
            if (Test-Path -LiteralPath $next) { Remove-Item -LiteralPath $next -Force }
        }
    }
    $version = Read-UvVersion $Executable
    if ($null -eq $version -or $version -lt $MinimumVersion) { throw 'Installed update tool could not be verified.' }
    Write-Host "[Nexus Ark] Download tool repaired: uv $version ($Executable)"
    Write-Host "[Nexus Ark] Previous tools preserved: $BackupDirectory"
    # Downloader replacement is complete. A later Python failure must not roll
    # the fixed downloader back to an unsafe version.
    $Changed = @()
    Complete-PythonPreparation $Executable
} catch {
    $failure = $_.Exception.Message
    foreach ($change in $Changed) {
        $destination = Join-Path $InstallDirectory $change.Name
        try {
            if ($change.Existed) {
                Copy-Item -LiteralPath (Join-Path $BackupDirectory $change.Name) -Destination $destination -Force
            } elseif (Test-Path -LiteralPath $destination) {
                Remove-Item -LiteralPath $destination -Force
            }
        } catch { Write-Warning "Previous tool could not be restored automatically. Keep backup: $BackupDirectory" }
    }
    Write-Host "[Nexus Ark] Update tool repair stopped: $failure"
    Write-Host '[Nexus Ark] Do not continue the application update. Keep the current app and contact support.'
    exit 1
} finally {
    if ($null -ne $InstallLock) { $InstallLock.Dispose() }
    if ($OwnedTemporary -and (Test-Path -LiteralPath $OwnedTemporary)) {
        Remove-Item -LiteralPath $OwnedTemporary -Recurse -Force -ErrorAction SilentlyContinue
    }
}
