<#
    Nexus Ark Package 0 bridge

    このスクリプトは、旧形式の配布フォルダーを新しい原子更新ランチャーへ一度だけ
    移行するためのものです。ダウンロード・展開は行わず、このスクリプト自身が入って
    いる完全 ZIP の内容だけを移行元として使います。

    安全側の原則:
      * 移行元・移行先・必須ファイルを先に検査する。
      * app のロック／更新トランザクション／bridge ロックがあれば変更しない。
      * 旧 Start.bat を backup に保存してから updater を準備する。
      * updater.next と Start.bat の一時ファイルは、検証完了後にだけ rename/replace。
      * 失敗時に updater.next や backup を削除しない（再調査できるよう残す）。
#>

[CmdletBinding()]
param(
    # テストおよびコマンドライン利用時は移行先を直接指定できる。
    [Parameter(Mandatory = $false)]
    [string]$TargetRoot = ""
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$RequiredUpdateHostFiles = @(
    "__init__.py",
    "contracts.py",
    "transaction.py",
    "trial.py",
    "supervisor.py"
)

function Write-BridgeMessage {
    param([string]$Message)
    Write-Host ("[Nexus Ark] " + $Message)
}

function Get-FullPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Test-SamePath {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )
    $leftFull = (Get-FullPath $Left).TrimEnd([char[]]@('\', '/'))
    $rightFull = (Get-FullPath $Right).TrimEnd([char[]]@('\', '/'))
    return [StringComparer]::OrdinalIgnoreCase.Equals($leftFull, $rightFull)
}

function Assert-Directory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw ("{0} が見つかりません。完全 ZIP を展開し直してください。" -f $Label)
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw ("{0} が通常のフォルダーではありません。安全のため停止します。" -f $Label)
    }
    return (Get-FullPath $Path)
}

function Assert-File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw ("{0} が見つかりません。完全 ZIP を展開し直してください。" -f $Label)
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw ("{0} が通常のファイルではありません。安全のため停止します。" -f $Label)
    }
    return $item
}

function Read-Version {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    Assert-File $Path $Label | Out-Null
    try {
        $value = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw ("{0} の JSON を読み取れません。" -f $Label)
    }
    if ($null -eq $value -or [String]::IsNullOrWhiteSpace([string]$value.version)) {
        throw ("{0} に version がありません。" -f $Label)
    }
    return [string]$value.version
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-SourceBundle {
    param([Parameter(Mandatory = $true)][string]$SourceRoot)

    $sourceStart = Join-Path $SourceRoot "Start.bat"
    Assert-File $sourceStart "完全 ZIP の Start.bat" | Out-Null
    $sourceApp = Assert-Directory (Join-Path $SourceRoot "app") "完全 ZIP の app"
    Assert-File (Join-Path $SourceRoot "pyproject.toml") "完全 ZIP の pyproject.toml" | Out-Null
    Assert-File (Join-Path $SourceRoot "uv.lock") "完全 ZIP の uv.lock" | Out-Null
    Assert-File (Join-Path $sourceApp "pyproject.toml") "完全 ZIP の app/pyproject.toml" | Out-Null
    Assert-File (Join-Path $sourceApp "uv.lock") "完全 ZIP の app/uv.lock" | Out-Null
    Read-Version (Join-Path $sourceApp "version.json") "完全 ZIP の app/version.json" | Out-Null

    $sourceCurrent = Assert-Directory (Join-Path $SourceRoot "updater\current") "完全 ZIP の updater/current"
    $sourceHost = Assert-Directory (Join-Path $sourceCurrent "update_host") "完全 ZIP の updater/current/update_host"
    foreach ($name in $RequiredUpdateHostFiles) {
        Assert-File (Join-Path $sourceHost $name) ("完全 ZIP の updater/current/update_host/{0}" -f $name) | Out-Null
    }

    # 旧形式の Start.bat を誤って移行元にしない。新版 shim は stable host を呼ぶ。
    $startText = Get-Content -LiteralPath $sourceStart -Raw -Encoding UTF8
    if ($startText -notmatch "update_host\.supervisor") {
        throw "完全 ZIP の Start.bat が保護された更新hostを呼び出していません。新版 ZIP を使用してください。"
    }
    if ($startText -match "(?i)robocopy") {
        throw "完全 ZIP の Start.bat が旧式 robocopy 更新を含んでいます。新版 ZIP を使用してください。"
    }
    return @{
        Start = $sourceStart
        Version = Read-Version (Join-Path $sourceApp "version.json") "完全 ZIP の app/version.json"
        Current = $sourceCurrent
        Host = $sourceHost
    }
}

function Assert-LegacyTarget {
    param([Parameter(Mandatory = $true)][string]$Root)

    Assert-File (Join-Path $Root "Start.bat") "旧配布の Start.bat" | Out-Null
    $oldApp = Assert-Directory (Join-Path $Root "app") "旧配布の app"
    $oldVersion = Read-Version (Join-Path $oldApp "version.json") "旧配布の app/version.json"
    Assert-File (Join-Path $oldApp "nexus_ark.py") "旧配布の app/nexus_ark.py" | Out-Null
    # 新hostを起動するため、app側のlock／project定義は必須。root側が
    # 欠けている場合だけ後段でapp側から補い、存在するrootファイルは触らない。
    Assert-File (Join-Path $oldApp "pyproject.toml") "旧配布の app/pyproject.toml" | Out-Null
    Assert-File (Join-Path $oldApp "uv.lock") "旧配布の app/uv.lock" | Out-Null

    # 既に新しい host がある場合は、部分適用を推測して上書きしない。
    $existingCurrent = Join-Path $Root "updater\current"
    if (Test-Path -LiteralPath $existingCurrent -PathType Container) {
        throw "旧配布に updater/current が既にあります。完全 ZIP から再導入するか手動復旧してください。"
    }
    $existingNext = Join-Path $Root "updater.next"
    if (Test-Path -LiteralPath $existingNext) {
        throw "旧配布に updater.next が残っています。中断した移行を確認してから再実行してください。"
    }
    return @{
        App = $oldApp
        Version = $oldVersion
        Start = Join-Path $Root "Start.bat"
    }
}

function Get-PidState {
    param([Parameter(Mandatory = $true)][int]$Pid)

    if ($Pid -le 0) {
        return "unknown"
    }
    try {
        # Get-Process だけでなく CIM でも実体を確認する。権限不足などで
        # command line が読めない場合は、安全のため unknown とする。
        $process = Get-Process -Id $Pid -ErrorAction Stop
        if ($null -eq $process) {
            return "stale"
        }
        $details = Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId={0}" -f $Pid) -ErrorAction Stop
        if ($null -eq $details) {
            return "stale"
        }
        if ([String]::IsNullOrWhiteSpace([string]$details.CommandLine)) {
            return "unknown"
        }
        # PIDの再利用で別プロセスを誤ってNexus Arkと見なさない。
        if ([string]$details.CommandLine -notmatch "(?i)nexus_ark(\.py|\.exe)") {
            return "unknown"
        }
        return "running"
    }
    catch [System.Management.Automation.ItemNotFoundException] {
        return "stale"
    }
    catch {
        return "unknown"
    }
}

function Assert-LockFileClear {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $false)][bool]$AnyFileBlocks = $false
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }

    $state = "unknown"
    try {
        $value = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        $pidValue = 0
        if ($null -eq $value.pid -or -not [Int32]::TryParse([string]$value.pid, [ref]$pidValue)) {
            throw "PID がありません"
        }
        $state = Get-PidState $pidValue
    }
    catch {
        $state = "unknown"
    }

    # transaction.lock は stale でも消さず、未解決トランザクションとして停止する。
    if ($AnyFileBlocks -or $state -eq "running" -or $state -eq "unknown") {
        throw ("{0} が見つかりました（状態: {1}）。アプリを終了し、手動確認後に再実行してください。" -f $Label, $state)
    }
    # nexus_ark.lock の stale PID は旧アプリ自身が次回起動時に安全に整理する。
    Write-BridgeMessage ("{0} は stale PID のため残します。" -f $Label)
}

function Acquire-BridgeLock {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$OperationId
    )
    $stream = $null
    $recovery = Join-Path $Root "update_recovery"
    New-Item -ItemType Directory -Path $recovery -Force | Out-Null
    $path = Join-Path $recovery "bridge.lock"
    $payload = (@{
        schema_version = 1
        operation_id = $OperationId
        pid = $PID
        purpose = "legacy_launcher_bridge"
        created_at = [DateTime]::UtcNow.ToString("o")
    } | ConvertTo-Json -Compress)
    try {
        $stream = New-Object System.IO.FileStream(
            $path,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        $bytes = [Text.Encoding]::UTF8.GetBytes($payload + "`n")
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
        $stream.Dispose()
        return @{
            Path = $path
            OperationId = $OperationId
            Owned = $true
        }
    }
    catch {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
        throw "bridge専用ロックを取得できません。別の移行が実行中か、前回の中断を確認してください。"
    }
}

function Copy-And-VerifyFile {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Label
    )
    Assert-File $Source $Label | Out-Null
    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
    Assert-File $Destination ("移行先の {0}" -f $Label) | Out-Null
    $sourceHash = Get-Sha256 $Source
    $destinationHash = Get-Sha256 $Destination
    if ($sourceHash -ne $destinationHash) {
        throw ("{0} のコピー検証に失敗しました。旧 Start.bat は変更していません。" -f $Label)
    }
}

function Replace-FileAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    try {
        # NTFS上の通常ケース（既存ファイルの置換）。
        [IO.File]::Replace($Source, $Destination, $null, $true)
        return
    }
    catch {
        # WSL共有パスや古い.NETではFile.Replaceのbackup引数が拒否されることが
        # あるため、同じWindowsボリューム内のMoveFileExへフォールバックする。
        # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH は単一rename置換であり、
        # Move-Item -Force の非原子的なcopy/deleteには戻さない。
        if (-not ([System.Management.Automation.PSTypeName]"NexusArkNativeMethods").Type) {
            Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class NexusArkNativeMethods {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool MoveFileEx(string existingFileName, string newFileName, uint flags);
}
"@
        }
        $moved = [NexusArkNativeMethods]::MoveFileEx($Source, $Destination, [uint32]0x00000009)
        if (-not $moved) {
            $win32Error = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            throw ("Start.bat の原子的置換に失敗しました（Win32 error {0}）。" -f $win32Error)
        }
    }
}

function Write-BridgeRecord {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][hashtable]$Values
    )
    $json = $Values | ConvertTo-Json -Depth 4
    $json | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Invoke-Bridge {
    param([Parameter(Mandatory = $true)][string]$DestinationRoot)

    $sourceRoot = Get-FullPath $PSScriptRoot
    $destination = Assert-Directory $DestinationRoot "移行先フォルダー"
    if (Test-SamePath $sourceRoot $destination) {
        throw "完全 ZIP のフォルダーと旧 Nexus Ark フォルダーが同じです。別の旧フォルダーを選択してください。"
    }

    # 変更を開始する前に、source と legacy target の全検査を完了する。
    $sourceInfo = Assert-SourceBundle $sourceRoot
    $targetInfo = Assert-LegacyTarget $destination

    $operationId = ([Guid]::NewGuid()).ToString("N")
    $bridgeLock = $null
    $backupRoot = $null
    $current = $null
    $startReplaced = $false
    $updaterMoved = $false
    try {
        $bridgeLock = Acquire-BridgeLock $destination $operationId

        # lock 取得後にもう一度確認する（検査と適用の間に起動される競合を止める）。
        Assert-LockFileClear (Join-Path $targetInfo.App "nexus_ark.lock") "旧アプリの nexus_ark.lock" $false
        Assert-LockFileClear (Join-Path $destination "update_recovery\transaction.lock") "更新トランザクション lock" $true

        $backupRoot = Join-Path $destination ("update_recovery\bridge-backups\{0}" -f $operationId)
        New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
        Copy-And-VerifyFile $targetInfo.Start (Join-Path $backupRoot "Start.bat") "旧 Start.bat"
        $rootDependencyRecords = @{}
        foreach ($dependency in @("pyproject.toml", "uv.lock")) {
            $rootPath = Join-Path $destination $dependency
            $appPath = Join-Path $targetInfo.App $dependency
            if (Test-Path -LiteralPath $rootPath -PathType Leaf) {
                Assert-File $rootPath ("旧配布の root/{0}" -f $dependency) | Out-Null
                $rootDependencyRecords[$dependency] = @{
                    present = $true
                    sha256 = Get-Sha256 $rootPath
                }
            }
            else {
                $rootDependencyRecords[$dependency] = @{
                    present = $false
                    source_sha256 = Get-Sha256 $appPath
                }
            }
        }
        Write-BridgeRecord (Join-Path $backupRoot "bridge.json") @{
            schema_version = 1
            operation_id = $operationId
            old_version = $targetInfo.Version
            new_version = $sourceInfo.Version
            old_start_sha256 = Get-Sha256 $targetInfo.Start
            source_start_sha256 = Get-Sha256 $sourceInfo.Start
            root_dependency_files = $rootDependencyRecords
        }

        # legacy root に無い依存定義だけを、app側から検証済みコピーする。
        # いったん操作ID付き一時ファイルへ書き、rootに同名が現れていないことを
        # 再確認してから同一ボリューム内でmoveする。既存rootは上書きしない。
        foreach ($dependency in @("pyproject.toml", "uv.lock")) {
            $rootPath = Join-Path $destination $dependency
            if (-not (Test-Path -LiteralPath $rootPath -PathType Leaf)) {
                $appPath = Join-Path $targetInfo.App $dependency
                $rootTemporary = Join-Path $destination ("{0}.bridge-{1}.tmp" -f $dependency, $operationId)
                Copy-And-VerifyFile $appPath $rootTemporary ("root/{0}（appから補完）" -f $dependency)
                if (Test-Path -LiteralPath $rootPath) {
                    throw ("移行中に root/{0} が作成されました。競合のため停止します。" -f $dependency)
                }
                [IO.File]::Move($rootTemporary, $rootPath)
                if ((Get-Sha256 $rootPath) -ne (Get-Sha256 $appPath)) {
                    throw ("root/{0} の補完後検証に失敗しました。" -f $dependency)
                }
            }
        }

        $updaterParent = Join-Path $destination "updater"
        $updaterNext = Join-Path $destination "updater.next"
        if (Test-Path -LiteralPath $updaterNext) {
            throw "移行先に updater.next が既にあります。中断の証跡を削除せず手動確認してください。"
        }
        New-Item -ItemType Directory -Path $updaterNext -Force | Out-Null
        # updater.next 自体が current の中身になる（current/current にはしない）。
        $nextHost = Join-Path $updaterNext "update_host"
        # current 配下は将来 host が複数ファイルへ分割されても取りこぼさないよう、
        # symlink/reparse point を拒否しながら全通常ファイルをコピーする。
        $sourceCurrentFiles = Get-ChildItem -LiteralPath $sourceInfo.Current -File -Recurse -Force
        foreach ($sourceFile in $sourceCurrentFiles) {
            if (($sourceFile.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw ("完全 ZIP の updater/current に symlink が含まれています: {0}" -f $sourceFile.FullName)
            }
            $relative = $sourceFile.FullName.Substring($sourceInfo.Current.Length).TrimStart([char[]]@('\', '/'))
            Copy-And-VerifyFile $sourceFile.FullName (Join-Path $updaterNext $relative) ("updater/current/{0}" -f $relative)
        }
        foreach ($name in $RequiredUpdateHostFiles) {
            Assert-File (Join-Path $nextHost $name) ("検証済み updater.next/current/update_host/{0}" -f $name) | Out-Null
        }

        $current = Join-Path $updaterParent "current"
        if (Test-Path -LiteralPath $current) {
            throw "移行先に updater/current が現れました。競合のため Start.bat は変更していません。"
        }
        New-Item -ItemType Directory -Path $updaterParent -Force | Out-Null
        # 同一ボリューム上の Directory.Move は、updater.next を current へ原子的に切り替える。
        [IO.Directory]::Move($updaterNext, $current)
        $updaterMoved = $true

        $temporaryStart = Join-Path $destination ("Start.bat.bridge-{0}.tmp" -f $operationId)
        Copy-And-VerifyFile $sourceInfo.Start $temporaryStart "新 Start.bat（一時ファイル）"
        if (-not (Test-SamePath $temporaryStart $targetInfo.Start)) {
            # File.Replace は同一ボリューム上の既存ファイルを原子的に置き換える。
            Replace-FileAtomic $temporaryStart $targetInfo.Start
        }
        $startReplaced = $true
        Write-BridgeMessage ("移行が完了しました（旧版 {0} → 新版 {1}）。" -f $targetInfo.Version, $sourceInfo.Version)
        Write-BridgeMessage "次回から Start.bat は保護された更新hostを使用します。backup は削除せず保持しています。"
    }
    catch {
        if ($updaterMoved -and -not $startReplaced -and $null -ne $backupRoot -and $null -ne $current) {
            # Start.batのreplace前に失敗した場合、new hostをbackupへ退避して
            # 旧Start + 旧レイアウトへ戻す。削除はせず、失敗証跡を残す。
            $failedCurrentBackup = Join-Path $backupRoot "updater-current"
            if ((Test-Path -LiteralPath $current -PathType Container) -and -not (Test-Path -LiteralPath $failedCurrentBackup)) {
                try {
                    [IO.Directory]::Move($current, $failedCurrentBackup)
                    Write-BridgeMessage "Start.bat切替前の失敗を検出し、新hostをbackupへ戻しました。"
                }
                catch {
                    Write-BridgeMessage "新hostのbackup退避にも失敗しました。削除せず手動復旧してください。"
                }
            }
        }
        if (-not $startReplaced) {
            Write-BridgeMessage "移行を中止しました。旧 Start.bat は変更していません。"
        }
        throw
    }
    finally {
        if ($null -ne $bridgeLock -and $bridgeLock.Owned) {
            # bridge lock 自体は操作の排他用で、成功／失敗後に解放する。
            try { Remove-Item -LiteralPath $bridgeLock.Path -Force -ErrorAction SilentlyContinue } catch { }
        }
    }
}

function Select-TargetFolder {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = "旧 Nexus Ark フォルダー（Start.bat と app がある場所）を選択してください。"
    $dialog.ShowNewFolderButton = $false
    $result = $dialog.ShowDialog()
    if ($result -ne [System.Windows.Forms.DialogResult]::OK -or [String]::IsNullOrWhiteSpace($dialog.SelectedPath)) {
        throw "移行先が選択されませんでした。変更は行っていません。"
    }
    return $dialog.SelectedPath
}

try {
    Write-BridgeMessage "旧配布から保護された更新ランチャーへ移行します。"
    if ([String]::IsNullOrWhiteSpace($TargetRoot)) {
        $TargetRoot = Select-TargetFolder
    }
    Invoke-Bridge (Get-FullPath $TargetRoot)
    exit 0
}
catch {
    Write-Host ("[Nexus Ark] 移行できませんでした: " + $_.Exception.Message) -ForegroundColor Yellow
    Write-Host "旧 Start.bat は変更していません。完全 ZIP からの再導入も利用できます。" -ForegroundColor Yellow
    exit 1
}
