[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$Stage,
    [Parameter(Mandatory = $true)][int]$WaitPid,
    [Parameter(Mandatory = $false)][switch]$ShowProgress
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$script:ProgressEnabled = [bool]$ShowProgress
$script:Progress = $null
$script:ProgressCanClose = $false

function Open-ProgressWindow {
    param([Parameter(Mandatory = $true)][string]$TargetRoot)

    if (-not $script:ProgressEnabled) { return }
    $form = $null
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        Add-Type -AssemblyName System.Drawing -ErrorAction Stop
        $form = New-Object System.Windows.Forms.Form
        $form.Text = "Nexus Ark - 更新方式の移行"
        $form.ClientSize = New-Object System.Drawing.Size(640, 250)
        $form.StartPosition = "CenterScreen"
        $form.FormBorderStyle = "FixedDialog"
        $form.MaximizeBox = $false
        $form.MinimizeBox = $false
        # 処理中の閉じる操作でhelperを止めない。完了／失敗後は専用ボタンだけを有効にする。
        $form.ControlBox = $false

        $targetLabel = New-Object System.Windows.Forms.Label
        $targetLabel.SetBounds(20, 16, 600, 38)
        $targetLabel.Text = "対象フォルダー:`r`n$TargetRoot"
        $targetLabel.AutoEllipsis = $true

        $stageLabel = New-Object System.Windows.Forms.Label
        $stageLabel.SetBounds(20, 62, 600, 38)
        $stageLabel.Text = "移行を準備しています..."

        $detailLabel = New-Object System.Windows.Forms.Label
        $detailLabel.SetBounds(20, 104, 600, 42)
        $detailLabel.Text = "この画面を閉じずにお待ちください。"

        $bar = New-Object System.Windows.Forms.ProgressBar
        $bar.SetBounds(20, 154, 600, 18)
        $bar.Style = "Marquee"
        $bar.MarqueeAnimationSpeed = 30

        $elapsedLabel = New-Object System.Windows.Forms.Label
        $elapsedLabel.SetBounds(20, 180, 480, 24)
        $elapsedLabel.Text = "経過時間: 0秒"

        $closeButton = New-Object System.Windows.Forms.Button
        $closeButton.SetBounds(520, 176, 100, 30)
        $closeButton.Text = "閉じる"
        $closeButton.Enabled = $false

        $form.Controls.AddRange(@($targetLabel, $stageLabel, $detailLabel, $bar, $elapsedLabel, $closeButton))
        $form.Add_FormClosing({
            param($sender, $eventArgs)
            if (-not $script:ProgressCanClose) {
                $eventArgs.Cancel = $true
            }
        })
        $closeButton.Add_Click({
            param($sender, $eventArgs)
            $script:ProgressCanClose = $true
            if ($null -ne $script:Progress -and $null -ne $script:Progress.Form) {
                try { $script:Progress.Form.Close() } catch { }
            }
        })
        $script:Progress = [pscustomobject]@{
            Form = $form
            StageLabel = $stageLabel
            DetailLabel = $detailLabel
            ElapsedLabel = $elapsedLabel
            ProgressBar = $bar
            CloseButton = $closeButton
            Started = [Diagnostics.Stopwatch]::StartNew()
        }
        $form.Show()
        Update-ProgressWindow
    }
    catch {
        # UIの初期化失敗は移行処理の失敗に変換しない。headlessと同じ安全な処理を続ける。
        if ($null -ne $form) {
            try { $form.Dispose() } catch { }
        }
        $script:Progress = $null
    }
}

function Update-ProgressWindow {
    if ($null -eq $script:Progress) { return }
    try {
        $progress = $script:Progress
        if ($progress.Form.IsDisposed) { return }
        $progress.ElapsedLabel.Text = "経過時間: {0}秒" -f [int]$progress.Started.Elapsed.TotalSeconds
        [System.Windows.Forms.Application]::DoEvents()
    }
    catch {
        # 通知の失敗はファイル移行の成否を変更しない。
    }
}

function Set-ProgressWindowStatus {
    param(
        [Parameter(Mandatory = $true)][string]$StageText,
        [Parameter(Mandatory = $false)][string]$DetailText = ""
    )

    if ($null -eq $script:Progress) { return }
    try {
        if ($script:Progress.Form.IsDisposed) { return }
        $script:Progress.StageLabel.Text = $StageText
        if (-not [String]::IsNullOrWhiteSpace($DetailText)) {
            $script:Progress.DetailLabel.Text = $DetailText
        }
        Update-ProgressWindow
    }
    catch {
        # 通知の失敗はファイル移行の成否を変更しない。
    }
}

function Wait-ProgressWindowMilliseconds {
    param([Parameter(Mandatory = $true)][int]$Milliseconds)

    if ($null -eq $script:Progress) {
        Start-Sleep -Milliseconds $Milliseconds
        return
    }
    $deadline = [DateTime]::UtcNow.AddMilliseconds($Milliseconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        Update-ProgressWindow
        $remaining = [int][Math]::Max(1, ($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        Start-Sleep -Milliseconds ([Math]::Min(250, $remaining))
    }
    Update-ProgressWindow
}

function Set-ProgressWindowTerminal {
    param(
        [Parameter(Mandatory = $true)][bool]$Success,
        [Parameter(Mandatory = $true)][string]$DetailText
    )

    if ($null -eq $script:Progress) { return }
    try {
        $script:ProgressCanClose = $true
        if ($script:Progress.Form.IsDisposed) { return }
        # 端末状態へ遷移した時点で先に閉じる操作を許可する。以後のラベル更新や
        # DoEvents が例外になっても、画面が閉じられずhelperが残り続けないようにする。
        $script:Progress.CloseButton.Enabled = $true
        $script:Progress.Started.Stop()
        $script:Progress.ProgressBar.Style = "Continuous"
        $script:Progress.ProgressBar.Value = if ($Success) { 100 } else { 0 }
        $script:Progress.Form.Text = if ($Success) { "Nexus Ark - 移行完了" } else { "Nexus Ark - 移行に失敗しました" }
        $script:Progress.Form.ControlBox = $true
        $script:Progress.StageLabel.Text = if ($Success) { "移行が完了しました" } else { "移行に失敗しました" }
        $script:Progress.DetailLabel.Text = $DetailText
        Update-ProgressWindow
    }
    catch {
        # result.jsonはすでに確定しているため、UI例外で結果を上書きしない。
    }
}

function Wait-ProgressWindowClose {
    if ($null -eq $script:Progress) { return }
    try {
        while (-not $script:Progress.Form.IsDisposed -and $script:Progress.Form.Visible) {
            Update-ProgressWindow
            Start-Sleep -Milliseconds 100
        }
    }
    catch {
        # UIの終了失敗はhelperの終了状態を変更しない。
    }
}

function Close-ProgressWindow {
    if ($null -eq $script:Progress) { return }
    $progress = $script:Progress
    $script:Progress = $null
    $script:ProgressCanClose = $true
    try { $progress.Started.Stop() } catch { }
    try { $progress.Form.Close() } catch { }
    try { $progress.Form.Dispose() } catch { }
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
        }
        finally {
            $sha.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

function Assert-RegularFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw ("必要な移行ファイルがありません: {0}" -f $Path)
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw ("移行ファイルが通常ファイルではありません: {0}" -f $Path)
    }
}

function Replace-FileAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    try {
        [IO.File]::Replace($Source, $Destination, $null, $true)
    }
    catch {
        if (-not ([System.Management.Automation.PSTypeName]"NexusArkLegacyBridgeNative").Type) {
            Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class NexusArkLegacyBridgeNative {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool MoveFileEx(string existingFileName, string newFileName, uint flags);
}
"@
        }
        if (-not [NexusArkLegacyBridgeNative]::MoveFileEx($Source, $Destination, [uint32]0x00000009)) {
            throw ("Start.bat の切替に失敗しました（Win32 error {0}）。" -f [Runtime.InteropServices.Marshal]::GetLastWin32Error())
        }
    }
}

$rootPath = [IO.Path]::GetFullPath($Root).TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
)
$stagePath = [IO.Path]::GetFullPath($Stage).TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
)
$allowedParent = [IO.Path]::GetFullPath(
    (Join-Path $rootPath "update_recovery\legacy-host-migration")
).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
$allowedPrefix = $allowedParent + [IO.Path]::DirectorySeparatorChar
if (-not $stagePath.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "移行stagingの場所を確認できません。"
}

$currentMoved = $false
$startReplaced = $false
$backup = $null
$current = Join-Path $rootPath "updater\current"

Open-ProgressWindow -TargetRoot $rootPath
try {
    Set-ProgressWindowStatus -StageText "Nexus Arkの終了を待っています..." -DetailText "本体を通常の方法で終了してください。処理中はこの画面を閉じないでください。"
    $deadline = [DateTime]::UtcNow.AddSeconds(300)
    while ($true) {
        $waitingProcess = Get-Process -Id $WaitPid -ErrorAction SilentlyContinue
        if ($null -eq $waitingProcess) { break }
        if ([DateTime]::UtcNow -ge $deadline) {
            throw "Nexus Arkの終了を確認できませんでした。もう一度起動して移行をやり直してください。"
        }
        Update-ProgressWindow
        Start-Sleep -Milliseconds 250
    }
    # 旧Start.batがuv終了後の後片付けを読み終えるまで待つ。
    Set-ProgressWindowStatus -StageText "終了を確認しました。移行を開始しています..." -DetailText "旧Start.batの後片付けが終わるまで少し待ちます。"
    Wait-ProgressWindowMilliseconds -Milliseconds 5000

    Set-ProgressWindowStatus -StageText "移行ファイルを検証しています..." -DetailText "署名済みのstagingファイルとハッシュを確認しています。"
    $recordPath = Join-Path $stagePath "record.json"
    Assert-RegularFile $recordPath
    $record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$record.operation_id -ne (Split-Path -Leaf $stagePath)) {
        throw "移行記録を確認できません。"
    }
    foreach ($entry in $record.files) {
        $relative = [string]$entry.path
        if ([String]::IsNullOrWhiteSpace($relative) -or $relative.Contains("..") -or [IO.Path]::IsPathRooted($relative)) {
            throw "移行ファイル名を確認できません。"
        }
        $candidate = Join-Path $stagePath $relative
        Assert-RegularFile $candidate
        if ((Get-Sha256 $candidate) -ne [string]$entry.sha256) {
            throw ("移行ファイルの検証に失敗しました: {0}" -f $relative)
        }
    }

    $start = Join-Path $rootPath "Start.bat"
    Assert-RegularFile $start
    $next = Join-Path $rootPath "updater.next"
    if ((Test-Path -LiteralPath $current) -or (Test-Path -LiteralPath $next)) {
        throw "更新hostの配置が既にあります。上書きせず停止します。"
    }
    if (Test-Path -LiteralPath (Join-Path $rootPath "update_recovery\transaction.lock")) {
        throw "更新処理の記録があるため、上書きせず停止します。"
    }

    Set-ProgressWindowStatus -StageText "旧Start.batを保全しています..." -DetailText "失敗時に戻せるbackupを先に作成します。"
    $backup = Join-Path $rootPath ("update_recovery\bridge-backups\{0}" -f $record.operation_id)
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    Copy-Item -LiteralPath $start -Destination (Join-Path $backup "Start.bat")
    if ((Get-Sha256 $start) -ne (Get-Sha256 (Join-Path $backup "Start.bat"))) {
        throw "旧Start.batのbackupを確認できません。"
    }

    foreach ($name in @("pyproject.toml", "uv.lock")) {
        $destination = Join-Path $rootPath $name
        if (-not (Test-Path -LiteralPath $destination)) {
            [IO.File]::Move((Join-Path $stagePath $name), $destination)
        }
    }

    Set-ProgressWindowStatus -StageText "保護された更新hostを配置しています..." -DetailText "更新hostを原子的に切り替える準備をしています。"
    [IO.Directory]::Move((Join-Path $stagePath "updater.current"), $next)
    New-Item -ItemType Directory -Path (Join-Path $rootPath "updater") -Force | Out-Null
    [IO.Directory]::Move($next, $current)
    $currentMoved = $true

    $temporaryStart = Join-Path $rootPath ("Start.bat.bridge-{0}.tmp" -f $record.operation_id)
    [IO.File]::Move((Join-Path $stagePath "Start.bat"), $temporaryStart)
    Replace-FileAtomic $temporaryStart $start
    $startReplaced = $true

    @{ state = "complete"; operation_id = [string]$record.operation_id } |
        ConvertTo-Json -Compress |
        Set-Content -LiteralPath (Join-Path $stagePath "result.json") -Encoding UTF8
    Set-ProgressWindowTerminal -Success $true -DetailText "同じフォルダーのStart.batからNexus Arkを起動し、更新欄で移行済みか確認してください。"
    Wait-ProgressWindowClose
}
catch {
    $failureRecord = $_
    if ($currentMoved -and -not $startReplaced -and $null -ne $backup) {
        $failedHost = Join-Path $backup "updater-current"
        if ((Test-Path -LiteralPath $current -PathType Container) -and -not (Test-Path -LiteralPath $failedHost)) {
            try { [IO.Directory]::Move($current, $failedHost) } catch { }
        }
    }
    @{ state = "failed"; error_type = $_.Exception.GetType().Name } |
        ConvertTo-Json -Compress |
        Set-Content -LiteralPath (Join-Path $stagePath "result.json") -Encoding UTF8
    $failureMessage = "移行を完了できませんでした。update_recoveryとbackupを削除せず、同じStart.batからの再実行や復旧案内を確認してください。"
    Set-ProgressWindowTerminal -Success $false -DetailText $failureMessage
    Wait-ProgressWindowClose
    throw $failureRecord
}
finally {
    Close-ProgressWindow
}
