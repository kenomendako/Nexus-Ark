<#
Nexus Ark: refresh an already protected installation's Start.bat.

Internal helper: the supported user entry is PrepareSecureUpdate.bat, whose
preflight checks the complete host/update state before invoking this file.
This file is not a standalone recovery procedure for an interrupted app update.

This helper is deliberately narrower than the normal updater.  It only
replaces a known, canonical protected launcher after the selected root is
stopped.  The stable updater/current host and every application/runtime file
are outside this operation.  Legacy robocopy launchers are left untouched so
the existing legacy migration remains the only path for them.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$TargetRoot,
    [Parameter(Mandatory = $true)][string]$TemplatePath
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'SecureLauncherMaintenance.ps1')
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

# Known protected launchers include the first distributed generation and
# 138d4054. Body updates preserve Start, so app version cannot identify it.
$KnownOldStartSha256 = @(
    # 0.61/0.62 protected launcher: normal app updates leave this stable file in place.
    'ba5c48ebae2dec33a4da3ee8b644f3e1f99322eb77a23a16ddb675d23b9eb5c7',
    '34529f95471c044bf199929cc24523f201f71588e066b74616e416523042770d', # Windows package CRLF
    'ca8b0cc1e38d4dcc40436f0744436e3c65bcc904cd894c2c263f3eaff02d53d1',
    'b58de9707250d4657d199025b75a1e3c50c315272362c1fe593470b2a163ec20', # Windows package CRLF
    '837c25b9c4b4c5fb30d7f2f6d6081ed8daa4f0bfcbe2aeb31cddca9ddeb24b39', # 3.12.15 LF
    'e3070fbc412577aff121d8a8aff33bb0bd15c790fce205580926569299de7c37' # 3.12.15 CRLF
)

# This is the canonical launcher produced by replacing only the host selector
# in the HEAD file with the managed CPython 3.12.16 selector.  Keep this list
# fixed: TemplatePath is an input location, not an authorization boundary.
$KnownNewStartSha256 = @(
    '3bafb0efb475fdd9bd9e686739a461018c1612dc092a356b9bf8a9ed2538684a', # 3.12.16 LF
    '184427c65309ebc1f45b2d2a2c5f71ee2ec1e70cb40c458a35449c1b06705241' # 3.12.16 CRLF
)

function Get-NormalizedPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw 'パスが空です。'
    }
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($full)
    if ($full.Length -gt $root.Length) {
        $full = $full.TrimEnd([char[]]@('\', '/'))
    }
    return $full
}

function Assert-NoReparseAncestors {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$AllowMissing
    )

    $cursor = Get-NormalizedPath $Path
    while ($true) {
        try {
            $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
        }
        catch {
            if (-not $AllowMissing) { throw ('{0} の親を確認できません: {1}' -f $Label, $cursor) }
            $item = $null
        }
        if ($null -ne $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw ('{0} にリンクまたは再解析ポイントが含まれるため停止します: {1}' -f $Label, $cursor)
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $cursor) {
            break
        }
        $cursor = $parent
    }
}

function Assert-RegularDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = Get-NormalizedPath $Path
    Assert-NoReparseAncestors -Path $full -Label $Label
    $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
    if (-not $item.PSIsContainer) {
        throw ('{0} が通常のフォルダーではありません。' -f $Label)
    }
    return $full
}

function Assert-RegularFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = Get-NormalizedPath $Path
    Assert-NoReparseAncestors -Path $full -Label $Label
    $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
    if ($item.PSIsContainer) {
        throw ('{0} が通常のファイルではありません。' -f $Label)
    }
    return $full
}

function Assert-ExistingPathIsSafe {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (Test-Path -LiteralPath $Path) {
        Assert-NoReparseAncestors -Path $Path -Label $Label
    }
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        }
        finally {
            $sha.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

function Read-Utf8Text {
    param([Parameter(Mandatory = $true)][string]$Path)

    $encoding = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList @($false, $true)
    try {
        return [IO.File]::ReadAllText($Path, $encoding)
    }
    catch {
        throw ('UTF-8のlauncherを読み取れません: {0}' -f $Path)
    }
}

function Assert-CanonicalTemplate {
    param([Parameter(Mandatory = $true)][string]$Path)

    $sha = Get-Sha256 $Path
    if ($KnownNewStartSha256 -notcontains $sha) {
        throw ('新しいStart.batが固定canonical SHAと一致しません: {0}' -f $sha)
    }
    $text = Read-Utf8Text $Path
    if ($text -match '(?i)robocopy' -or $text -match '(?i)uv\s+sync') {
        throw '新しいStart.batに旧式更新または直接依存同期が含まれています。'
    }
    if ($text -notmatch '(?i)--no-project\s+--managed-python\s+--python\s+cpython@3\.12\.16') {
        throw '新しいStart.batがmanaged CPython 3.12.16を固定していません。'
    }
    if ($text -notmatch '(?i)update_host\.supervisor') {
        throw '新しいStart.batが保護hostを起動しません。'
    }
    return $sha
}

function Assert-NoUpdateBlockers {
    param([Parameter(Mandatory = $true)][string]$Root, [switch]$OwnRefreshLock)

    Assert-NexusLauncherMaintenance -Root $Root -KnownStartHashes @($KnownOldStartSha256 + $KnownNewStartSha256) -OwnLease:$OwnRefreshLock
    # Any of these paths means another update or a prior partial launcher
    # operation may still own the tree.  Do not infer that it is stale.
    foreach ($relative in @(
        'update_recovery\transaction.lock',
        'update_recovery\bridge.lock',
        'updater.next',
        'app.next',
        '.venv.next',
        'Start.bat.lock'
    )) {
        if ($OwnRefreshLock -and $relative -eq 'update_recovery\secure-launcher-refresh.lock') { continue }
        $candidate = Join-Path $Root $relative
        if (Test-Path -LiteralPath $candidate) {
            Assert-NoReparseAncestors -Path $candidate -Label $relative
            throw ('更新またはStart.batのlockが残っているため停止します: {0}' -f $relative)
        }
    }
    Assert-ExistingPathIsSafe -Path (Join-Path $Root 'update_recovery') -Label 'update_recovery'
    $journal = Join-Path $Root 'update_recovery\transaction.json'
    if (Test-Path -LiteralPath $journal) {
        Assert-RegularFile -Path $journal -Label '更新transaction' | Out-Null
        $state = Get-Content -LiteralPath $journal -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([string]$state.phase -notin @('committed', 'rolled_back')) { throw '未完了の更新transactionが残っています。' }
    }
}

function Assert-AppLockClear {
    param([Parameter(Mandatory = $true)][string]$Root)

    $lock = Join-Path $Root 'app\nexus_ark.lock'
    if (-not (Test-Path -LiteralPath $lock)) { return }
    Assert-RegularFile -Path $lock -Label 'app/nexus_ark.lock' | Out-Null
    try {
        $payload = Get-Content -LiteralPath $lock -Raw -Encoding UTF8 | ConvertFrom-Json
        $pidValue = 0
        if ($null -eq $payload.pid -or -not [int]::TryParse([string]$payload.pid, [ref]$pidValue) -or $pidValue -le 0) {
            throw 'PIDがありません。'
        }
        $process = Get-Process -Id $pidValue -ErrorAction Stop
        if ($null -eq $process) { return }
        $details = Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId={0}' -f $pidValue) -ErrorAction Stop
        if ($null -eq $details -or [string]::IsNullOrWhiteSpace([string]$details.CommandLine)) {
            throw 'PIDの実行内容を確認できません。'
        }
        if ([string]$details.CommandLine -match '(?i)nexus_ark(\.py|\.exe)') {
            throw 'app/nexus_ark.lockの対象processが実行中です。'
        }
        # A stale PID reused by an unrelated process is not the old app.
    }
    catch {
        $typeName = [string]$_.Exception.GetType().FullName
        $message = [string]$_.Exception.Message
        if ($typeName -match '(?i)(ProcessCommandException|ItemNotFoundException)' -or
            $message -match '(?i)(cannot find a process|no process with the process identifier|process identifier .* was not found|process .* not found)') {
            return
        }
        if ($message -match 'app/nexus_ark\.lockの対象process') { throw }
        throw 'app/nexus_ark.lockの状態を安全に確認できないため停止します。'
    }
}

function Assert-TargetProcessesStopped {
    param([Parameter(Mandatory = $true)][string]$Root)

    try {
        $processes = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
    }
    catch {
        throw '実行中processを確認できないため停止します。'
    }
    $rootPrefix = (Get-NormalizedPath $Root).TrimEnd([char[]]@('\', '/'))
    $rootPattern = [regex]::Escape($rootPrefix) + '(?:[\\/"'']|$)'
    $targetPattern = '(?i)(?:nexus_ark(?:\.py|\.exe)?|update_host\.supervisor|Start\.bat)'
    foreach ($process in $processes) {
        if ([int]$process.ProcessId -eq $PID) { continue }
        $name = [string]$process.Name
        $commandLine = [string]$process.CommandLine
        $executablePath = [string]$process.ExecutablePath
        $targetsRoot = ($commandLine -match $rootPattern) -or ($executablePath -match $rootPattern)
        $targetsNexus = $commandLine -match $targetPattern
        $isNexusName = $name -match '(?i)^nexus_ark(\.exe)?$'
        if ($isNexusName -and [string]::IsNullOrWhiteSpace($commandLine)) {
            throw 'Nexus Arkのprocess詳細を確認できないため停止します。'
        }
        if ($targetsNexus -and [string]::IsNullOrWhiteSpace($executablePath) -and -not $targetsRoot) {
            throw 'Nexus Arkの実行元を確認できないため停止します。'
        }
        if ($targetsRoot -and $targetsNexus) {
            throw '選択したrootのNexus ArkまたはStart.batが実行中です。終了してから再実行してください。'
        }
        # A running cmd.exe may only expose `/c Start.bat`, without a working
        # directory in Win32_Process.  Treat every such launcher as a blocker
        # rather than risking a replacement while a batch file is executing.
        if ($name -match '(?i)^cmd(?:\.exe)?$' -and $commandLine -match '(?i)Start\.bat') {
            throw 'Start.batを実行するcmd.exeが残っているため停止します。'
        }
    }
}

function Open-RefreshLock {
    param([Parameter(Mandatory = $true)][string]$Root)

    $recovery = Join-Path $Root 'update_recovery'
    if (-not (Test-Path -LiteralPath $recovery)) {
        New-Item -ItemType Directory -Path $recovery -Force | Out-Null
    }
    Assert-RegularDirectory -Path $recovery -Label 'update_recovery' | Out-Null
    $lockPath = Join-Path $recovery 'secure-launcher-refresh.lock'
    Assert-NoReparseAncestors -Path $lockPath -Label 'launcher maintenance lease' -AllowMissing
    try {
        return [IO.File]::Open(
            $lockPath,
            [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
    }
    catch {
        throw '同じStart.batのrefresh、または未解決のlockが実行中です。'
    }
}

function Invoke-NexusLauncherReplace {
    param([string]$Temporary, [string]$Current, [string]$Previous)
    [IO.File]::Replace($Temporary, $Current, $Previous, $true)
}

function Invoke-Refresh {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Template
    )

    $rootPath = Assert-RegularDirectory -Path $Root -Label '対象root'
    $startPath = Assert-RegularFile -Path (Join-Path $rootPath 'Start.bat') -Label '対象Start.bat'
    $currentSha = Get-Sha256 $startPath
    $currentText = Read-Utf8Text $startPath

    # Legacy roots remain byte-for-byte untouched and continue through the
    # existing migration path.  Do this before requiring protected-package
    # files because legacy roots intentionally do not have them.
    if ($currentText -match '(?i)\brobocopy\b') {
        Write-Host '[Nexus Ark] legacy robocopy Start.batは変更せず、既存移行経路へ委譲します。'
        return
    }

    Assert-RegularFile -Path (Join-Path $rootPath 'updater\current\update_host\supervisor.py') -Label '保護更新host' | Out-Null
    $templatePath = Assert-RegularFile -Path $Template -Label '新しいStart.bat template'
    $templateSha = Assert-CanonicalTemplate $templatePath
    if ($KnownNewStartSha256 -contains $currentSha) {
        # Idempotent success still requires the canonical template, target
        # repair boundary, and stopped/clean root.  It performs no write and
        # creates no backup, but must not hide an active transaction.
        Assert-RegularFile -Path (Join-Path $rootPath 'app\release\RepairUpdateCertificates.ps1') -Label '対象app/release/RepairUpdateCertificates.ps1' | Out-Null
        Assert-NoUpdateBlockers -Root $rootPath
        Assert-AppLockClear -Root $rootPath
        Assert-TargetProcessesStopped -Root $rootPath
        Write-Host '[Nexus Ark] protected Start.batは既にmanaged CPython 3.12.16です。'
        return
    }
    if ($KnownOldStartSha256 -notcontains $currentSha) {
        throw ('既知canonicalでないprotectedまたは部分配置のStart.batです。上書きせず停止します: {0}' -f $currentSha)
    }

    # The new launcher will call this script on its next start.  Refuse to
    # install it if that repair boundary is not present in the target.
    Assert-RegularFile -Path (Join-Path $rootPath 'app\release\RepairUpdateCertificates.ps1') -Label '対象app/release/RepairUpdateCertificates.ps1' | Out-Null
    Assert-NoUpdateBlockers -Root $rootPath
    Assert-AppLockClear -Root $rootPath
    Assert-TargetProcessesStopped -Root $rootPath

    $lockStream = $null
    $backupDirectory = $null
    $temporaryStart = $null
    try {
        $lockStream = Open-RefreshLock -Root $rootPath
        # Re-check after acquiring the private lock; the preparation GUI may
        # have raced with an application start between the first inspection
        # and this point.
        Assert-NoUpdateBlockers -Root $rootPath -OwnRefreshLock
        Assert-AppLockClear -Root $rootPath
        Assert-TargetProcessesStopped -Root $rootPath
        if ((Get-Sha256 $startPath) -ne $currentSha) {
            throw 'Start.batが検査後に変更されたため停止します。'
        }

        $operationId = [guid]::NewGuid().ToString('N')
        $backupRoot = Join-Path $rootPath 'update_recovery\secure-launcher-refresh'
        Assert-ExistingPathIsSafe -Path $backupRoot -Label 'secure-launcher-refresh backup'
        if (-not (Test-Path -LiteralPath $backupRoot)) {
            New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
        }
        Assert-RegularDirectory -Path $backupRoot -Label 'secure-launcher-refresh backup' | Out-Null
        $backupDirectory = Join-Path $backupRoot $operationId
        New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
        Assert-RegularDirectory -Path $backupDirectory -Label '今回のStart.bat backup' | Out-Null

        $backupStart = Join-Path $backupDirectory 'Start.bat'
        [IO.File]::Copy($startPath, $backupStart, $false)
        Assert-RegularFile -Path $backupStart -Label 'Start.bat backup' | Out-Null
        if ((Get-Sha256 $backupStart) -ne $currentSha) {
            throw '旧Start.batのbackup検証に失敗しました。'
        }

        $record = [ordered]@{
            state = 'prepared'
            operation_id = $operationId
            target_root = $rootPath
            old_start_sha256 = $currentSha
            new_start_sha256 = $templateSha
        }
        ($record | ConvertTo-Json -Compress) | Set-Content -LiteralPath (Join-Path $backupDirectory 'record.json') -Encoding UTF8

        $temporaryStart = Join-Path $rootPath ('Start.bat.refresh-{0}.tmp' -f $operationId)
        [IO.File]::Copy($templatePath, $temporaryStart, $false)
        Assert-RegularFile -Path $temporaryStart -Label '一時Start.bat' | Out-Null
        if ((Get-Sha256 $temporaryStart) -ne $templateSha) {
            throw '一時Start.batのSHA検証に失敗しました。'
        }
        if ((Get-Sha256 $startPath) -ne $currentSha) {
            throw '差替え直前にStart.batが変更されたため停止します。'
        }

        # File.Replace is intentionally used without a fallback copy/move:
        # on failure the old Start.bat and the verified backup remain intact.
        Invoke-NexusLauncherReplace -Temporary $temporaryStart -Current $startPath -Previous (Join-Path $backupDirectory 'Start.bat.replaced')
        $temporaryStart = $null
        if ((Get-Sha256 $startPath) -ne $templateSha) {
            throw '差替え後のStart.batを検証できません。backupを保持しています。'
        }
        $record.state = 'complete'
        ($record | ConvertTo-Json -Compress) | Set-Content -LiteralPath (Join-Path $backupDirectory 'record.json') -Encoding UTF8
        Write-Host ('[Nexus Ark] protected Start.batを更新しました。backup: {0}' -f $backupDirectory)
    }
    finally {
        # Keep failed temporary bytes and their verified backup as evidence.
        if ($null -ne $lockStream) {
            $lockStream.Dispose()
        }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        Invoke-Refresh -Root $TargetRoot -Template $TemplatePath
        exit 0
    }
    catch {
        Write-Error $_.Exception.Message
        exit 1
    }
}
