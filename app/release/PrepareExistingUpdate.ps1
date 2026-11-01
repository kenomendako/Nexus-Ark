<#
Nexus Ark: prepare an existing Windows installation for the normal updater.

After shutdown and backup confirmation, older installations are updated by an
independent, hash-pinned environment without launching the old app. TUF-verified
app, Python environment and runtime are prepared beside the original root.
Already-secure installations can retain the host/launcher maintenance path.
update_url is preserved. Only a verified installation is started through its
own Start.bat; interrupted work and backups are retained for recovery.

The script is normally run from app/release in a complete package.  Internal
parameters on the functions below exist for isolated tests after dot-sourcing;
the product entry point exposes no target-path or repair-bypass switches.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:IsDotSourced = ($MyInvocation.InvocationName -eq '.')
$script:PreparationProgress = $null
. (Join-Path $PSScriptRoot 'InvokeLegacySecureBootstrap.ps1')

function Open-PreparationProgress {
    param([Parameter(Mandatory = $true)][string]$TargetRoot)

    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    $form = New-Object System.Windows.Forms.Form
    try {
        $form.Text = 'Nexus Ark — 更新の準備中'
        $form.ClientSize = New-Object System.Drawing.Size(600, 220)
        $form.StartPosition = 'CenterScreen'
        $form.FormBorderStyle = 'FixedDialog'
        $form.ControlBox = $false
        $form.AutoScaleMode = 'Dpi'

        $target = New-Object System.Windows.Forms.Label
        $target.SetBounds(20, 16, 560, 42)
        $target.Text = "今まで使っているフォルダー：`r`n$TargetRoot"
        $target.AutoEllipsis = $true

        $stage = New-Object System.Windows.Forms.Label
        $stage.SetBounds(20, 68, 560, 38)
        $stage.Text = '選択したフォルダーを確認しています…'

        $bar = New-Object System.Windows.Forms.ProgressBar
        $bar.SetBounds(20, 116, 560, 18)
        $bar.Style = 'Marquee'
        $bar.MarqueeAnimationSpeed = 30

        $elapsed = New-Object System.Windows.Forms.Label
        $elapsed.SetBounds(20, 144, 560, 22)
        $elapsed.Text = '準備を開始しています…'

        $note = New-Object System.Windows.Forms.Label
        $note.SetBounds(20, 174, 560, 38)
        $note.Text = "準備には数分かかることがあります。このままお待ちください。`r`n完了すると、選択したフォルダーのStart.batを起動します。"

        $form.Controls.AddRange(@($target, $stage, $bar, $elapsed, $note))
        $script:PreparationProgress = @{
            Form = $form
            StageLabel = $stage
            ElapsedLabel = $elapsed
            ProgressBar = $bar
            NoteLabel = $note
            Started = [Diagnostics.Stopwatch]::StartNew()
        }
        $form.Show()
        Update-PreparationProgress
    }
    catch {
        $form.Dispose()
        $script:PreparationProgress = $null
        throw
    }
}

function Update-PreparationProgress {
    if ($null -eq $script:PreparationProgress) { return }
    $progress = $script:PreparationProgress
    if ($progress.Form.IsDisposed) { return }
    $progress.ElapsedLabel.Text = '準備開始から {0} 秒' -f [int]$progress.Started.Elapsed.TotalSeconds
    [System.Windows.Forms.Application]::DoEvents()
}

function Set-PreparationStatus {
    param([Parameter(Mandatory = $true)][string]$Text)

    Write-Host ('[Nexus Ark] {0}' -f $Text)
    if ($null -ne $script:PreparationProgress -and -not $script:PreparationProgress.Form.IsDisposed) {
        $script:PreparationProgress.StageLabel.Text = $Text
        Update-PreparationProgress
    }
}

function Close-PreparationProgress {
    if ($null -eq $script:PreparationProgress) { return }
    $progress = $script:PreparationProgress
    $script:PreparationProgress = $null
    $progress.Started.Stop()
    $progress.Form.Dispose()
}

function Wait-PreparationProcess {
    param([Parameter(Mandatory = $true)][Diagnostics.Process]$Process)

    $watch = [Diagnostics.Stopwatch]::StartNew()
    $nextNotice = 5
    try {
        while (-not $Process.WaitForExit(250)) {
            Update-PreparationProgress
            if ($watch.Elapsed.TotalSeconds -ge $nextNotice) {
                Write-Host ('[Nexus Ark] 更新用ツールを準備中です。このままお待ちください（{0} 秒経過）。' -f [int]$watch.Elapsed.TotalSeconds)
                $nextNotice += 5
            }
        }
        $Process.WaitForExit()
        Update-PreparationProgress
    }
    finally {
        $watch.Stop()
    }
}

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

function Test-ReparsePoint {
    param([Parameter(Mandatory = $true)]$Item)
    return (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Test-SafePathOrAbsent {
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = Get-NormalizedPath $Path
    $root = [IO.Path]::GetPathRoot($full)
    $tail = $full.Substring($root.Length)
    $parts = @($tail -split '[\\/]' | Where-Object { $_ })
    $cursor = $root
    foreach ($part in $parts) {
        $cursor = Join-Path $cursor $part
        $item = Get-Item -LiteralPath $cursor -Force -ErrorAction SilentlyContinue
        if ($null -eq $item) {
            return $false
        }
        if (Test-ReparsePoint $item) {
            throw ('リンクまたは再解析ポイントを含むため停止します: {0}' -f $cursor)
        }
    }
    return $true
}

function Assert-RegularDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-SafePathOrAbsent $Path)) {
        throw ('{0} が見つかりません。' -f $Label)
    }
    $item = Get-Item -LiteralPath (Get-NormalizedPath $Path) -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or (Test-ReparsePoint $item)) {
        throw ('{0} が通常のフォルダーではありません。' -f $Label)
    }
    return (Get-NormalizedPath $item.FullName)
}

function Assert-RegularFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-SafePathOrAbsent $Path)) {
        throw ('{0} が見つかりません。' -f $Label)
    }
    $item = Get-Item -LiteralPath (Get-NormalizedPath $Path) -Force -ErrorAction Stop
    if ($item.PSIsContainer -or (Test-ReparsePoint $item)) {
        throw ('{0} が通常のファイルではありません。' -f $Label)
    }
    return $item
}

function Test-SameOrNestedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )

    $leftFull = Get-NormalizedPath $Left
    $rightFull = Get-NormalizedPath $Right
    if ([StringComparer]::OrdinalIgnoreCase.Equals($leftFull, $rightFull)) {
        return $true
    }
    $leftPrefix = $leftFull.TrimEnd([char[]]@('\', '/')) + '\'
    $rightPrefix = $rightFull.TrimEnd([char[]]@('\', '/')) + '\'
    return (
        $leftFull.StartsWith($rightPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        $rightFull.StartsWith($leftPrefix, [StringComparison]::OrdinalIgnoreCase)
    )
}

function Read-VersionValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    Assert-RegularFile $Path $Label | Out-Null
    try {
        $value = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw ('{0} の JSON を読み取れません。' -f $Label)
    }
    if ($null -eq $value -or [string]::IsNullOrWhiteSpace([string]$value.version)) {
        throw ('{0} に version がありません。' -f $Label)
    }
    return [string]$value.version
}

function Get-PackageSource {
    param([string]$PackageRoot = '')

    if ([string]::IsNullOrWhiteSpace($PackageRoot)) {
        # PSScriptRoot is app/release in the final package.  Resolving from it
        # keeps this tool independent of the caller's current directory.
        $release = Get-NormalizedPath $PSScriptRoot
        $app = Get-NormalizedPath (Join-Path $release '..')
        $PackageRoot = Get-NormalizedPath (Join-Path $app '..')
    }
    $root = Assert-RegularDirectory $PackageRoot '配布パッケージのルート'
    $app = Assert-RegularDirectory (Join-Path $root 'app') '配布パッケージの app'
    $release = Assert-RegularDirectory (Join-Path $app 'release') '配布パッケージの app/release'
    $start = (Assert-RegularFile (Join-Path $root 'Start.bat') '配布パッケージの Start.bat').FullName
    $repair = (Assert-RegularFile (Join-Path $release 'RepairUpdateCertificates.ps1') '配布パッケージの修復スクリプト').FullName
    $secureLauncher = (Assert-RegularFile (Join-Path $release 'RefreshSecureLauncher.ps1') '配布パッケージの起動設定準備スクリプト').FullName
    $secureHost = (Assert-RegularFile (Join-Path $release 'RepairSecureUpdateHost.ps1') '配布パッケージの更新host準備スクリプト').FullName
    Assert-RegularFile (Join-Path $release 'SecureLauncherMaintenance.ps1') '配布パッケージの起動設定復旧スクリプト' | Out-Null
    Assert-RegularFile (Join-Path $release 'EnsureSecurePython.ps1') '配布パッケージのPython準備スクリプト' | Out-Null
    Assert-RegularFile (Join-Path $release 'PrepareExistingUpdate.ps1') '配布パッケージの準備スクリプト' | Out-Null
    Assert-RegularFile (Join-Path $app 'release_manifest.json') '配布パッケージの release_manifest.json' | Out-Null
    $version = Read-VersionValue (Join-Path $app 'version.json') '配布パッケージの app/version.json'

    $hostDirectory = Assert-RegularDirectory (Join-Path $root 'updater\current\update_host') '配布パッケージの保護host'
    foreach ($name in @('__init__.py', 'contracts.py', 'transaction.py', 'trial.py', 'supervisor.py')) {
        Assert-RegularFile (Join-Path $hostDirectory $name) ('配布パッケージの保護host/{0}' -f $name) | Out-Null
    }
    $startText = Get-Content -LiteralPath $start -Raw -Encoding UTF8
    if ($startText -notmatch 'update_host\.supervisor') {
        throw '配布パッケージの Start.bat が保護された更新hostを起動しません。'
    }
    if ($startText -match '(?i)robocopy') {
        throw '配布パッケージの Start.bat に旧式の robocopy 更新が含まれています。'
    }
    return @{
        Root = $root
        App = $app
        Release = $release
        Start = $start
        Repair = $repair
        SecureLauncher = $secureLauncher
        SecureHost = $secureHost
        SourceHost = (Split-Path -Parent $hostDirectory)
        Version = $version
    }
}

function Get-PidState {
    param([Parameter(Mandatory = $true)][int]$ProcessIdValue)

    if ($ProcessIdValue -le 0) { return 'unknown' }
    try {
        $process = Get-Process -Id $ProcessIdValue -ErrorAction Stop
        if ($null -eq $process) { return 'stale' }
        $details = Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId={0}' -f $ProcessIdValue) -ErrorAction Stop
        if ($null -eq $details) { return 'stale' }
        $commandLine = [string]$details.CommandLine
        if ([string]::IsNullOrWhiteSpace($commandLine)) { return 'unknown' }
        if ($commandLine -notmatch '(?i)nexus_ark(\.py|\.exe)') { return 'unknown' }
        return 'running'
    }
    catch {
        # Windows PowerShell has emitted several exception types for a PID
        # that has already exited (including ProcessCommandException and
        # ArgumentException).  Distinguish those from access/query failures.
        $typeName = [string]$_.Exception.GetType().FullName
        $message = [string]$_.Exception.Message
        if ($typeName -match '(?i)(ProcessCommandException|ItemNotFoundException)' -or $message -match '(?i)(cannot find a process|no process with the process identifier|process identifier .* was not found|process .* not found)') {
            return 'stale'
        }
        return 'unknown'
    }
}

function Assert-NexusLockClear {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-SafePathOrAbsent $Path)) { return }
    Assert-RegularFile $Path 'app/nexus_ark.lock' | Out-Null
    try {
        $payload = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        $processIdValue = 0
        if ($null -eq $payload.pid -or -not [int]::TryParse([string]$payload.pid, [ref]$processIdValue)) {
            throw 'PID がありません。'
        }
        $state = Get-PidState -ProcessIdValue $processIdValue
    }
    catch {
        throw 'app/nexus_ark.lock の形式または状態を確認できないため停止します。'
    }
    if ($state -eq 'running') {
        throw 'Nexus Ark が起動中です。終了してから再実行してください。'
    }
    if ($state -eq 'unknown') {
        throw 'app/nexus_ark.lock のPIDを安全に確認できないため停止します。'
    }
    # stale PID は旧アプリ自身が次回起動時に整理できるため削除しない。
}

function Assert-TargetProcessesStopped {
    param([Parameter(Mandatory = $true)][string]$Root)

    try {
        $processes = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
    }
    catch {
        throw '実行中プロセスを確認できないため停止します。'
    }
    $rootPrefix = (Get-NormalizedPath $Root).TrimEnd([char[]]@('\', '/'))
    # A supervisor command normally quotes --root when the path has spaces.
    # Treat that closing quote as a boundary while keeping `foo` distinct
    # from a sibling such as `foobar`.
    $rootPattern = [regex]::Escape($rootPrefix) + '(?:[\\/"'']|$)'
    $modulePattern = '(?i)(?:nexus_ark\.py|nexus_ark\.exe|update_host\.supervisor)'
    foreach ($process in $processes) {
        $name = [string]$process.Name
        $commandLine = [string]$process.CommandLine
        $executablePath = [string]$process.ExecutablePath
        $isNexusName = $name -match '(?i)^nexus_ark(\.exe)?$'
        if ($isNexusName -and [string]::IsNullOrWhiteSpace($commandLine)) {
            throw 'Nexus Ark の実行状態を確認できないため停止します。'
        }
        $commandTargetsRoot = $commandLine -match $rootPattern
        $executableTargetsRoot = $executablePath -match $rootPattern
        if (($commandTargetsRoot -or $executableTargetsRoot) -and [string]::IsNullOrWhiteSpace($commandLine)) {
            throw '選択したルートのプロセス詳細を確認できないため停止します。'
        }
        $isNexusModule = $commandLine -match $modulePattern
        if ($isNexusModule -and [string]::IsNullOrWhiteSpace($executablePath) -and -not $commandTargetsRoot) {
            throw 'Nexus Arkの実行元を確認できないため停止します。'
        }
        # Python launched from the selected root commonly receives a relative
        # `nexus_ark.py` command line.  Its executable path (for example
        # root/.venv/Scripts/python.exe) supplies the root boundary.  A
        # process from another root is ignored unless its command line names
        # this root explicitly.
        if (($commandTargetsRoot -or $executableTargetsRoot) -and $isNexusModule) {
            throw '選択したNexus Arkが起動中です。終了してから再実行してください。'
        }
    }
}

function Assert-TargetRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [hashtable]$Source = $null,
        [switch]$AllowRolledBackNext
    )

    $root = Assert-RegularDirectory $Root '選択したNexus Arkのルート'
    $app = Assert-RegularDirectory (Join-Path $root 'app') '選択したルートの app'
    $start = (Assert-RegularFile (Join-Path $root 'Start.bat') '選択したルートの Start.bat').FullName
    Assert-RegularFile (Join-Path $app 'version.json') '選択したルートの app/version.json' | Out-Null
    Assert-RegularFile (Join-Path $app 'nexus_ark.py') '選択したルートの app/nexus_ark.py' | Out-Null
    Assert-RegularFile (Join-Path $app 'pyproject.toml') '選択したルートの app/pyproject.toml' | Out-Null
    # July's public package has app/pyproject.toml but no uv.lock.  The
    # protected host can work with the package's existing dependency files;
    # uv.lock is therefore optional for this preflight.
    if (Test-SafePathOrAbsent (Join-Path $app 'uv.lock')) {
        Assert-RegularFile (Join-Path $app 'uv.lock') '選択したルートの app/uv.lock' | Out-Null
    }

    $startText = Get-Content -LiteralPath $start -Raw -Encoding UTF8
    $hasModernHost = $startText -match 'update_host\.supervisor' -and $startText -notmatch '(?i)robocopy'
    $requiresIndependentRebuild = $false
    $current = Join-Path $root 'updater\current'
    if (Test-SafePathOrAbsent $current) {
        $hostDirectory = Assert-RegularDirectory (Join-Path $current 'update_host') '選択したルートの保護host'
        Assert-RegularFile (Join-Path $hostDirectory 'supervisor.py') '選択したルートの保護host/supervisor.py' | Out-Null
        if (-not $hasModernHost) {
            throw '保護hostがあるのに旧式のStart.batです。部分更新を推測せず停止します。'
        }
    }
    elseif ($hasModernHost -and $null -eq $Source) {
        throw '新版Start.batに必要な保護hostがありません。完全な配布物を確認してください。'
    }
    # Inspect without writing before asking for backup confirmation or repairing
    # uv. Only the dedicated receipt protocol can justify a missing current.
    if ($null -ne $Source) {
        $hostState = Invoke-SecureHostMaintenance -Source $Source -TargetRoot $root -InspectOnly -AllowRolledBackNext:$AllowRolledBackNext
        if ($hostState.state -eq 'incomplete_089') {
            $targetVersion = Read-VersionValue (Join-Path $app 'version.json') '選択したルートの app/version.json'
            # Do not treat the signed current target as a newer release.
            # The normal TUF version comparison remains strict.
            if ($targetVersion -ne '0.5.0.89' -or [version]$Source.Version -le [version]$targetVersion) {
                throw '[legacy-bootstrap-secure-release-required]'
            }
            $requiresIndependentRebuild = $true
        }
        if ($hasModernHost -and -not (Test-SafePathOrAbsent $current) -and $hostState.state -ne 'repair_pending') {
            throw '更新hostの中断状態を検証できないため停止します。'
        }
    }

    foreach ($optional in @(
        (Join-Path $root 'updater.next'),
        (Join-Path $root 'updater'),
        (Join-Path $root 'runtime'),
        (Join-Path $root 'metadata'),
        (Join-Path $root 'update_recovery'),
        (Join-Path $app 'config.json')
    )) {
        Test-SafePathOrAbsent $optional | Out-Null
    }
    foreach ($optionalDirectory in @(
        (Join-Path $root 'updater'),
        (Join-Path $root 'runtime'),
        (Join-Path $root 'metadata'),
        (Join-Path $root 'update_recovery')
    )) {
        if (Test-SafePathOrAbsent $optionalDirectory) {
            $optionalItem = Get-Item -LiteralPath $optionalDirectory -Force -ErrorAction Stop
            if (-not $optionalItem.PSIsContainer) {
                throw ('{0} がフォルダーではないため停止します。' -f $optionalDirectory)
            }
        }
    }
    $optionalConfig = Join-Path $app 'config.json'
    if (Test-SafePathOrAbsent $optionalConfig) {
        $optionalConfigItem = Get-Item -LiteralPath $optionalConfig -Force -ErrorAction Stop
        if ($optionalConfigItem.PSIsContainer) {
            throw '選択したルートの app/config.json がファイルではないため停止します。'
        }
    }
    if (Test-SafePathOrAbsent (Join-Path $root 'updater.next')) {
        throw 'updater.next が残っているため停止します。中断した更新を確認してください。'
    }
    if (Test-SafePathOrAbsent (Join-Path $root 'update_recovery\transaction.lock')) {
        throw '更新トランザクションが残っているため停止します。手動確認後に再実行してください。'
    }
    if (Test-SafePathOrAbsent (Join-Path $root 'update_recovery\bridge.lock')) {
        throw '更新bridgeが残っているため停止します。手動確認後に再実行してください。'
    }
    Assert-NexusLockClear (Join-Path $app 'nexus_ark.lock')
    Assert-TargetProcessesStopped $root
    return @{
        Root = $root
        App = $app
        Start = $start
        Version = Read-VersionValue (Join-Path $app 'version.json') '選択したルートの app/version.json'
        RequiresIndependentRebuild = $requiresIndependentRebuild
    }
}

function Show-PreparationMessage {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [ValidateSet('Information', 'Warning', 'Error')][string]$Kind = 'Information',
        [ValidateSet('OK', 'YesNo')][string]$Buttons = 'OK'
    )

    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    $icon = [System.Windows.Forms.MessageBoxIcon]::$Kind
    $buttonValue = [System.Windows.Forms.MessageBoxButtons]::$Buttons
    $defaultButton = [System.Windows.Forms.MessageBoxDefaultButton]::Button1
    if ($Buttons -eq 'YesNo') { $defaultButton = [System.Windows.Forms.MessageBoxDefaultButton]::Button2 }
    if ($null -ne $script:PreparationProgress -and -not $script:PreparationProgress.Form.IsDisposed) {
        return [System.Windows.Forms.MessageBox]::Show($script:PreparationProgress.Form, $Text, 'Nexus Ark', $buttonValue, $icon, $defaultButton)
    }
    return [System.Windows.Forms.MessageBox]::Show($Text, 'Nexus Ark', $buttonValue, $icon, $defaultButton)
}

function Select-TargetRoot {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = '今までのNexus Arkフォルダーを選択。途中終了で見つからない場合は、隣の「.<元の名前>.nexus-secure-update」を選んでください。'
    $dialog.ShowNewFolderButton = $false
    try {
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            return $null
        }
        return (Get-NormalizedPath $dialog.SelectedPath)
    }
    finally {
        $dialog.Dispose()
    }
}

function Confirm-BackupAndShutdown {
    param([string]$TargetRoot = '')
    $message = @"
対象: $TargetRoot

選択したNexus Arkを完全に終了し、起動中の更新処理がないことを確認してください。
また、選択したフォルダー全体のバックアップを別の場所へ取得済みであることを確認してください。

「はい」を押すと、署名済み更新や更新用ツールを確認し、必要な場合は会話・設定を保管して更新します。
完了後、今までのフォルダーのStart.batから起動します。
このツールは作業前の全体バックアップを代わりに作成するものではありません。会話や個人設定を手作業で移す必要はありません。
"@
    $result = Show-PreparationMessage -Text $message -Kind Warning -Buttons YesNo
    return ($result -eq [System.Windows.Forms.DialogResult]::Yes)
}

function ConvertTo-WindowsArgument {
    param([Parameter(Mandatory = $true)][string]$Value)
    # Windows paths cannot contain a double quote.  Always quote so Japanese,
    # spaces, and cmd metacharacters stay data for PowerShell's -File parser.
    return ('"{0}"' -f $Value.Replace('"', '\"'))
}

function Invoke-UvRepair {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Source,
        [Parameter(Mandatory = $false)][string]$LocalBin = (Join-Path $env:USERPROFILE '.local\bin')
    )

    $localBinFull = Get-NormalizedPath $LocalBin
    $powerShell = Join-Path $PSHOME 'powershell.exe'
    Assert-RegularFile $powerShell 'Windows PowerShell' | Out-Null
    Assert-RegularFile $Source.Repair 'uv修復スクリプト' | Out-Null
    $arguments = @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', (ConvertTo-WindowsArgument $Source.Repair),
        '-InstallDirectory', (ConvertTo-WindowsArgument $localBinFull)
    ) -join ' '
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $powerShell
    $psi.Arguments = $arguments
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = $Source.Root
    $oldPath = [string]$env:PATH
    $psi.EnvironmentVariables['PATH'] = $localBinFull + ';' + $oldPath
    $psi.EnvironmentVariables['PATHEXT'] = '.COM;.EXE;.BAT;.CMD'
    $process = [Diagnostics.Process]::Start($psi)
    if ($null -eq $process) { throw 'uv修復プロセスを起動できません。' }
    try {
        Wait-PreparationProcess -Process $process
        $exitCode = $process.ExitCode
    }
    finally {
        $process.Dispose()
    }
    if ($exitCode -ne 0) {
        throw ('uv修復が失敗しました（終了コード {0}）。' -f $exitCode)
    }

    # Verify the exact executable that the subsequent Start.bat will prefer;
    # do not resolve uv through the caller's PATH.
    $uv = Join-Path $localBinFull 'uv.exe'
    Assert-RegularFile $uv '修復後のlocalbin/uv.exe' | Out-Null
    $versionPsi = New-Object System.Diagnostics.ProcessStartInfo
    $versionPsi.FileName = $uv
    $versionPsi.Arguments = '--version'
    $versionPsi.UseShellExecute = $false
    $versionPsi.CreateNoWindow = $true
    $versionPsi.RedirectStandardOutput = $true
    $versionPsi.RedirectStandardError = $true
    $versionPsi.EnvironmentVariables['PATHEXT'] = '.COM;.EXE;.BAT;.CMD'
    $versionProcess = $null
    try {
        $versionProcess = [Diagnostics.Process]::Start($versionPsi)
        if ($null -eq $versionProcess) { throw 'uvのバージョン確認プロセスを起動できません。' }
        $versionStdout = $versionProcess.StandardOutput.ReadToEnd()
        $versionStderr = $versionProcess.StandardError.ReadToEnd()
        $versionProcess.WaitForExit()
        $versionExitCode = $versionProcess.ExitCode
    }
    finally {
        if ($null -ne $versionProcess) { $versionProcess.Dispose() }
    }
    $versionText = [string]$versionStdout + "`n" + [string]$versionStderr
    $uvVersionMatch = [regex]::Match($versionText, '(?m)^uv ([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)')
    if ($versionExitCode -ne 0 -or -not $uvVersionMatch.Success -or [version]$uvVersionMatch.Groups[1].Value -lt [version]'0.13.4') {
        throw '修復後のlocalbin/uv.exeを検証できません。'
    }
    return $uv
}

function Invoke-SecureHostMaintenance {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Source,
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [switch]$InspectOnly,
        [switch]$AllowRolledBackNext
    )
    $arguments = @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', (ConvertTo-WindowsArgument $Source.SecureHost),
        '-TargetRoot', (ConvertTo-WindowsArgument $TargetRoot),
        '-SourceHostPath', (ConvertTo-WindowsArgument $Source.SourceHost)
    )
    if ($InspectOnly) { $arguments += '-InspectOnly' }
    if ($AllowRolledBackNext) { $arguments += '-AllowRolledBackNext' }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = Join-Path $PSHOME 'powershell.exe'
    $psi.Arguments = $arguments -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
    $psi.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
    $process = [Diagnostics.Process]::Start($psi)
    if ($null -eq $process) { throw '更新hostの安全確認プロセスを開始できません。' }
    try {
        # Drain both pipes while pumping the preparation progress window.
        $outputTask = $process.StandardOutput.ReadToEndAsync()
        $errorTask = $process.StandardError.ReadToEndAsync()
        Wait-PreparationProcess -Process $process
        $outputText = $outputTask.GetAwaiter().GetResult()
        $errorText = $errorTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            # Consume only the bounded public failure contract, never raw
            # stderr/internal exceptions that could disclose paths or tokens.
            $safeReasons = @{
                'start-hash-mismatch' = 'Start.batが対応する正規ファイルと一致しません。上書きやlock削除をせず、診断結果をお知らせください。'
                'process-running' = 'Nexus Arkまたは起動処理を検出しました。黒い起動ウィンドウを含め、完全に終了してから再試行してください。'
                'process-unknown' = '実行中プロセスの状態を安全に確認できません。lockを削除せず、診断結果をお知らせください。'
                'app-lock-active' = 'アプリのlockが実行中の本体を示しています。完全に終了してから再試行してください。'
                'app-lock-invalid' = 'アプリのlockの形式・状態を安全に確認できません。削除せず、診断結果をお知らせください。'
                'repair-lock-active' = '別の準備処理、または未解決の修復lockがあります。ほかの準備ツールを閉じ、続く場合は診断結果をお知らせください。'
                'pending-update' = '未完了の更新作業が残っています。既存フォルダーとbackupを残して、診断結果をお知らせください。'
                'receipt-or-recovery' = '更新hostの復旧記録を安全に確認できません。既存フォルダーとbackupを残して、診断結果をお知らせください。'
                'inventory-or-path' = '必要な更新ファイルまたはフォルダー構成を安全に確認できません。診断結果をお知らせください。'
                'validation' = '更新hostの安全確認を完了できません。診断結果をお知らせください。'
            }
            $kind = 'validation'
            try {
                $failure = $outputText | ConvertFrom-Json -ErrorAction Stop
                if ([string]$failure.state -eq 'blocked' -and $safeReasons.ContainsKey([string]$failure.failure_kind)) {
                    $kind = [string]$failure.failure_kind
                }
            } catch { }
            throw ('更新の準備を安全に完了できないため、Nexus Arkを起動していません。' + "`r`n" + $safeReasons[$kind] + "`r`n確認コード: " + $kind)
        }
        $result = $outputText | ConvertFrom-Json
        if ([string]$result.state -notin @('legacy', 'known_old', 'known_old8', 'known_077', 'known_085', 'known_088', 'known_new', 'repair_pending', 'incomplete_089')) {
            throw '更新host準備の結果を検証できません。'
        }
        if (-not $InspectOnly -and [string]$result.state -notin @('legacy', 'known_new')) {
            throw '更新hostの交換が完了していないため起動しません。'
        }
        return $result
    }
    finally { $process.Dispose() }
}

function Invoke-SecureLauncherRefresh {
    param([hashtable]$Source, [hashtable]$Target)
    $arguments = @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', (ConvertTo-WindowsArgument $Source.SecureLauncher),
        '-TargetRoot', (ConvertTo-WindowsArgument $Target.Root),
        '-TemplatePath', (ConvertTo-WindowsArgument $Source.Start)
    ) -join ' '
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = Join-Path $PSHOME 'powershell.exe'
    $psi.Arguments = $arguments
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $process = [Diagnostics.Process]::Start($psi)
    if ($null -eq $process) { throw '起動設定の準備プロセスを開始できません。' }
    try {
        Wait-PreparationProcess -Process $process
        if ($process.ExitCode -ne 0) {
            throw '起動設定の安全な更新を確認できません。既存フォルダーとbackupを残して、表示内容をお知らせください。'
        }
    }
    finally { $process.Dispose() }
}

function Start-SelectedRoot {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Target,
        [Parameter(Mandatory = $false)][string]$LocalBin = (Join-Path $env:USERPROFILE '.local\bin')
    )

    # Do not trust a caller-provided ComSpec.  Resolve cmd.exe from the
    # Windows system directory, then pass only the fixed /d /c Start.bat
    # command; the selected root is supplied as the working directory.
    $comSpec = [IO.Path]::Combine([Environment]::SystemDirectory, 'cmd.exe')
    Assert-RegularFile $comSpec 'Windows cmd.exe' | Out-Null
    $localBinFull = Get-NormalizedPath $LocalBin
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $comSpec
    $psi.Arguments = '/d /c Start.bat'
    $psi.WorkingDirectory = $Target.Root
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $false
    $psi.EnvironmentVariables['PATH'] = $localBinFull + ';' + [string]$env:PATH
    $process = [Diagnostics.Process]::Start($psi)
    if ($null -eq $process) { throw '選択したNexus Arkを起動できません。' }
    return $process
}

function Enter-PreparationMutex {
    param([Parameter(Mandatory = $true)][string]$Root)

    $normalizedRoot = Get-NormalizedPath $Root
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($normalizedRoot.ToUpperInvariant())
        $digest = $sha.ComputeHash($bytes)
    }
    finally {
        $sha.Dispose()
    }
    $suffix = (-join ($digest | ForEach-Object { $_.ToString('x2') }))
    $name = 'Local\NexusArk.Prepare.' + $suffix
    $createdNew = $false
    $mutex = [System.Threading.Mutex]::new($false, $name, [ref]$createdNew)
    if (-not $createdNew) {
        $mutex.Dispose()
        throw '同じNexus Arkの準備処理がすでに実行中です。完了を待ってから再実行してください。'
    }
    return $mutex
}

function Resolve-LegacyBootstrapSelection {
    param([Parameter(Mandatory=$true)][string]$Path)
    $full = Get-NormalizedPath $Path
    $candidate = $full
    if ([IO.Path]::GetFileName($candidate) -eq 'previous') {
        $parent = [IO.Path]::GetDirectoryName($candidate)
        if ([IO.Path]::GetFileName($parent) -match '^\.(.+)\.nexus-secure-update$') { $candidate = $parent }
    }
    $name = [IO.Path]::GetFileName($candidate)
    if ($name -match '^\.(.+)\.nexus-secure-update$') {
        $originalName = $Matches[1]
        if ($originalName -in @('.', '..')) { throw '初回更新の復旧フォルダー名が不正です。' }
        Assert-RegularDirectory $candidate '初回更新の復旧フォルダー' | Out-Null
        Assert-RegularFile (Join-Path $candidate 'receipt.json') '初回更新の復旧記録' | Out-Null
        return (Get-NormalizedPath (Join-Path ([IO.Path]::GetDirectoryName($candidate)) $originalName))
    }
    return $full
}

function Get-LegacyBootstrapWorkspace {
    param([Parameter(Mandatory=$true)][string]$Root)
    return (Join-Path ([IO.Path]::GetDirectoryName($Root)) ('.' + [IO.Path]::GetFileName($Root) + '.nexus-secure-update'))
}

function Test-NeedsIndependentSecureUpdate {
    param([Parameter(Mandatory=$true)][hashtable]$Target)
    if ($Target.ContainsKey('Recovery') -and $Target.Recovery) { return $true }
    if ($Target.ContainsKey('RequiresIndependentRebuild') -and $Target.RequiresIndependentRebuild) { return $true }
    # この版より前は既知protected Startでも旧本体のCA経路を起動しない。
    return ([version]$Target.Version -lt [version]'0.5.0.75')
}

function Invoke-PreparedTarget {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Source,
        [Parameter(Mandatory = $true)][string]$TargetRoot
    )

    $TargetRoot = Resolve-LegacyBootstrapSelection -Path $TargetRoot
    if (Test-SameOrNestedPath $Source.Root $TargetRoot) {
        throw '配布パッケージと選択した既存フォルダーが同一または内包関係にあるため停止します。'
    }
    $mutex = Enter-PreparationMutex $TargetRoot
    try {
        Set-PreparationStatus -Text '選択したフォルダーと、Nexus Arkが終了していることを確認しています…'
        $workspace = Get-LegacyBootstrapWorkspace -Root $TargetRoot
        $hasBootstrapWorkspace = Test-SafePathOrAbsent $workspace
        if (-not (Test-SafePathOrAbsent $TargetRoot) -and $hasBootstrapWorkspace) {
            Assert-RegularFile (Join-Path $workspace 'receipt.json') '初回更新の復旧記録' | Out-Null
            Assert-RegularDirectory (Join-Path $workspace 'previous') '初回更新の保全フォルダー' | Out-Null
            $target = @{ Root=$TargetRoot; App=(Join-Path $TargetRoot 'app'); Start=(Join-Path $TargetRoot 'Start.bat'); Version='recovery'; Recovery=$true }
        } else {
            $target = Assert-TargetRoot -Root $TargetRoot -Source $Source
        }
        Set-PreparationStatus -Text '停止とバックアップの確認画面で、内容をご確認ください。'
        if (-not (Confirm-BackupAndShutdown -TargetRoot $target.Root)) {
            Set-PreparationStatus -Text '準備を中止しました。Nexus Arkは起動していません。'
            return $false
        }
        $isLegacy = $target.ContainsKey('Recovery') -and $target.Recovery
        if (-not $isLegacy) {
            $legacyStart = Get-Content -LiteralPath $target.Start -Raw -Encoding UTF8
            $isLegacy = $legacyStart -match '(?i)\brobocopy\b'
        }
        $needsSecureUpdate = $isLegacy -or (Test-NeedsIndependentSecureUpdate $target)
        $pendingBootstrap = $hasBootstrapWorkspace
        if ($hasBootstrapWorkspace -and -not $needsSecureUpdate) {
            $receiptPath = Join-Path $workspace 'receipt.json'
            $receiptFile = Assert-RegularFile $receiptPath '初回更新の記録'
            if ($receiptFile.Length -gt 65536) { throw '初回更新の記録が不正です。' }
            $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
            # 通常更新後も初回backupは保持する。完了済みの古い取得targetへ
            # 再照合して、新しい正規更新を誤って復旧扱いにしない。
            if ([string]$receipt.state -eq 'committed') { $pendingBootstrap = $false }
        }
        if ($needsSecureUpdate -or $pendingBootstrap) {
            Set-PreparationStatus -Text '旧アプリを起動せず、安全な初回更新を準備しています…'
            $uv = Invoke-UvRepair $Source
            $bootstrap = Invoke-LegacySecureBootstrap -Source $Source -Target $target -UvExecutable $uv
            # Completeだけを根拠に旧Startへfallbackしない。切替後の保護hostと
            # Startをもう一度通常のread-only preflightで照合する。
            $target = Assert-TargetRoot -Root $TargetRoot -Source $Source
            $newStart = Get-Content -LiteralPath $target.Start -Raw -Encoding UTF8
            if ($newStart -notmatch 'update_host\.supervisor' -or $newStart -match '(?i)\brobocopy\b') {
                throw '初回更新の切り替えを確認できません。旧版を起動せず停止しました。確認コード: legacy-bootstrap-post-check'
            }
            Set-PreparationStatus -Text '安全な更新が完了しました。今までのフォルダーから新版を起動しています…'
            Start-SelectedRoot $target | Out-Null
            Show-PreparationMessage -Text ("安全な更新が完了しました。今までのフォルダーから新版の起動を開始しました。`r`n`r`n通常どおりNexus Arkをご利用ください。更新・移行操作をもう一度行う必要はありません。`r`n旧フォルダー全体は、隣の初回更新フォルダー内のpreviousに保管しています。") -Kind Information -Buttons OK | Out-Null
            return $true
        }
        Set-PreparationStatus -Text '更新用ツールと修正済みPythonを確認・準備しています。取得が必要な場合は数分かかることがあります。'
        Invoke-UvRepair $Source | Out-Null
        Set-PreparationStatus -Text '更新host全体を照合し、必要な場合はバックアップを残して安全に更新しています…'
        Invoke-SecureHostMaintenance -Source $Source -TargetRoot $target.Root | Out-Null
        Set-PreparationStatus -Text '修正済みPythonで起動するため、停止中の起動設定を確認しています…'
        Invoke-SecureLauncherRefresh -Source $Source -Target $target
        # The user may have started the old application while the repair was
        # running.  Recheck both process and lock state immediately before
        # handing control to cmd.exe.
        Set-PreparationStatus -Text 'ツールの準備が終わりました。起動前の最終確認をしています…'
        Assert-NexusLockClear (Join-Path $target.App 'nexus_ark.lock')
        Assert-TargetProcessesStopped $target.Root
        Set-PreparationStatus -Text '最終確認が終わりました。今までのフォルダーからNexus Arkを起動しています…'
        Start-SelectedRoot $target | Out-Null
        if ($null -ne $script:PreparationProgress) {
            $script:PreparationProgress.Form.Text = 'Nexus Ark — 準備完了'
            $script:PreparationProgress.ProgressBar.Style = 'Continuous'
            $script:PreparationProgress.ProgressBar.Value = 100
            $script:PreparationProgress.Started.Stop()
            $script:PreparationProgress.NoteLabel.Text = "準備ツールの処理は完了です。起動後は、通常の画面から自動更新してください。`r`n今後も、今まで使っているフォルダーのStart.batから起動します。"
        }
        Set-PreparationStatus -Text '準備が完了しました。選択したNexus Arkの起動を開始しました。'
        $completeMessage = "準備が完了しました。今まで使っているNexus Arkの起動を開始しました。`r`n`r`n起動するフォルダー：`r`n$($target.Root)`r`n`r`n起動後、通常の画面から自動更新を実行してください。今後もこのフォルダーのStart.batを使います。"
        Show-PreparationMessage -Text $completeMessage -Kind Information -Buttons OK | Out-Null
        return $true
    }
    finally {
        if ($null -ne $mutex) {
            $mutex.Dispose()
        }
    }
}

function Get-PreparationErrorText {
    param([Parameter(Mandatory=$true)][string]$Message)
    if ($Message -notmatch '^\[legacy-bootstrap-([a-z0-9-]+)\]$') { return $Message }
    $code = $Matches[1]
    $reasons = @{
        'secure-release-required' = '指定した更新先には、安全な初回更新に対応した新版がまだありません。対応する配布物の案内をお待ちください。'
        'insufficient-space' = '新版とデータを準備する空き容量が足りません。空きを確保してから再実行してください。'
        'process-or-lock' = '本体の実行状態またはlockを安全に確認できません。起動ウィンドウを含めて完全に終了し、続く場合は診断結果をお知らせください。'
        'data-changed' = '準備中に会話や設定が変わりました。本体を終了したまま再実行してください。'
        'trial-port-unavailable' = '起動確認に使うポートが使用中です。既存の本体が完全に終了しているか確認してください。'
        'trial-exited' = '新版が起動確認を完了できませんでした。確認コードをお知らせください。'
        'trial-timeout' = '新版の起動確認が時間内に完了しませんでした。確認コードをお知らせください。'
        'signature-verification-failed' = '更新の署名または有効期限を確認できません。安全のため停止しています。'
        'download-verification-failed' = '更新ファイルを完全に取得・検証できません。通信を確認してから再実行してください。'
    }
    $reason = '安全確認を完了できませんでした。確認コードをお知らせください。'
    if ($reasons.ContainsKey($code)) { $reason = $reasons[$code] }
    return ('安全な初回更新を完了できないため、本体を自動起動していません。' + "`r`n" + $reason + "`r`n既存フォルダー・backup・隣の復旧フォルダーを削除しないでください。`r`n確認コード: legacy-bootstrap-" + $code)
}

function Invoke-PreparationMain {
    param([string]$PackageRoot = '')

    try {
        Set-PreparationStatus -Text '準備ツールを確認しています…'
        $source = Get-PackageSource -PackageRoot $PackageRoot
        Set-PreparationStatus -Text 'フォルダー選択画面で、今まで使っているNexus Arkフォルダーを選んでください。'
        $targetRoot = Select-TargetRoot
        if ([string]::IsNullOrWhiteSpace([string]$targetRoot)) {
            return 0
        }
        Open-PreparationProgress -TargetRoot $targetRoot
        Invoke-PreparedTarget -Source $source -TargetRoot $targetRoot | Out-Null
        return 0
    }
    catch {
        try {
            if ($null -ne $script:PreparationProgress) {
                $script:PreparationProgress.Form.Text = 'Nexus Ark — 準備を完了できませんでした'
                $script:PreparationProgress.ProgressBar.Style = 'Continuous'
                $script:PreparationProgress.Started.Stop()
                $script:PreparationProgress.NoteLabel.Text = "準備ツールはここで停止しています。表示された案内をご確認ください。`r`n準備が完了するまでは、本体の自動更新へ進まないでください。"
            }
            Set-PreparationStatus -Text '準備を完了できませんでした。表示された案内をご確認ください。'
            Show-PreparationMessage -Text (Get-PreparationErrorText -Message $_.Exception.Message) -Kind Error -Buttons OK | Out-Null
        }
        catch {
            Write-Error $_.Exception.Message
        }
        return 1
    }
    finally {
        Close-PreparationProgress
    }
}

if (-not $script:IsDotSourced) {
    exit (Invoke-PreparationMain)
}
