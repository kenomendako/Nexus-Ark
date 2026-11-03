<# Fresh installation data import. Source is read-only; never rename it. #>
[CmdletBinding()]
param()
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:ImportDotSourced = ($MyInvocation.InvocationName -eq '.')
. (Join-Path $PSScriptRoot 'PrepareExistingUpdate.ps1')

function Invoke-FreshDataImport {
    param([Parameter(Mandatory=$true)][string]$OldRoot,
          [Parameter(Mandatory=$true)][hashtable]$Package)
    $old = Assert-RegularDirectory $OldRoot '引き継ぎ元'
    if ((Split-Path -Leaf $old) -eq 'app' -and (Test-Path -LiteralPath (Join-Path $old 'nexus_ark.py') -PathType Leaf)) {
        $old = Split-Path -Parent $old
    }
    if (Test-SameOrNestedPath -Left $old -Right $Package.Root) {
        throw '引き継ぎ元と新版は、別々の場所にしてください。'
    }
    $oldApp = $old
    if (Test-Path -LiteralPath (Join-Path $old 'app\nexus_ark.py') -PathType Leaf) {
        $oldApp = Assert-RegularDirectory (Join-Path $old 'app') '引き継ぎ元のapp'
    }
    Assert-RegularFile (Join-Path $oldApp 'config.json') '引き継ぎ元の設定' | Out-Null
    Assert-NexusLockClear -Path (Join-Path $oldApp 'nexus_ark.lock')
    Assert-NexusLockClear -Path (Join-Path $Package.App 'nexus_ark.lock')
    foreach ($root in @($old, $Package.Root)) {
        Assert-TargetProcessesStopped -Root $root
    }
    Set-PreparationStatus '更新用ツールと修正済みPythonを確認しています…'
    $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    $tools = Join-Path $env:TEMP ('NexusArk-Import-{0}' -f [guid]::NewGuid().ToString('N'))
    Assert-LegacyBootstrapSafeAncestors $tools | Out-Null
    New-Item -ItemType Directory -Path $tools -ErrorAction Stop | Out-Null
    [IO.File]::WriteAllText((Join-Path $tools 'empty.netrc'), '', (New-Object Text.UTF8Encoding($false)))
    New-Item -ItemType Directory -Path (Join-Path $tools 'uv-cache') -ErrorAction Stop | Out-Null
    $environment = Get-LegacyBootstrapEnvironment -UvExecutable $uv -ToolsRoot $tools
    foreach ($boundary in @($old, $Package.Root)) {
        if (Test-LegacyBootstrapSameOrChildPath -Root $boundary -Child $tools) { throw 'ツール一時領域を引き継ぎ元・先の外にしてください。' }
        foreach ($name in @('UV_PYTHON_INSTALL_DIR', 'UV_CACHE_DIR', 'TEMP', 'TMP')) {
            if ($environment.ContainsKey($name) -and (Test-LegacyBootstrapSameOrChildPath -Root $boundary -Child $environment[$name])) { throw 'Python・キャッシュ・一時ファイルの保存先を引き継ぎ元・先の外にしてください。' }
        }
    }
    $environment['UV_PYTHON_DOWNLOADS_JSON_URL'] = Get-LegacyBootstrapPythonDownloadsCatalog -SourceApp $Package.App
    $environment['PYTHONDONTWRITEBYTECODE'] = '1'
    # uv修復を含め、旧版のPython／uv設定を継承せずに起動する。
    $repairArgs = @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', $Package.Repair, '-InstallDirectory', (Split-Path -Parent $uv))
    $repairQuoted = @($repairArgs | ForEach-Object { Quote-LegacyBootstrapArgument ([string]$_) }) -join ' '
    $repairPsi = New-LegacyBootstrapProcessStartInfo -FileName (Join-Path $PSHOME 'powershell.exe') -Arguments $repairQuoted -WorkingDirectory $Package.Root -Environment $environment
    $repairProcess = [Diagnostics.Process]::Start($repairPsi)
    if ($null -eq $repairProcess) { throw '更新用ツールの準備を開始できません。' }
    try { Wait-PreparationProcess -Process $repairProcess; $repairCode = $repairProcess.ExitCode }
    finally { $repairProcess.Dispose() }
    if ($repairCode -ne 0) { throw '更新用ツールとPythonを安全に準備できませんでした。元のフォルダーを残してください。' }
    Get-LegacyBootstrapUvVersion -UvExecutable $uv -WorkingDirectory $Package.Root -Environment $environment | Out-Null
    # ダウンロード待ちの間に起動された場合も、コピー前にもう一度確認する。
    Assert-NexusLockClear -Path (Join-Path $oldApp 'nexus_ark.lock')
    Assert-NexusLockClear -Path (Join-Path $Package.App 'nexus_ark.lock')
    foreach ($root in @($old, $Package.Root)) { Assert-TargetProcessesStopped -Root $root }
    $arguments = @('run', '--no-config', '--no-project', '--managed-python', '--python', 'cpython@3.12.16',
        'python', '-I', '-B', '-X', 'utf8', (Join-Path $Package.Release 'import_user_data.py'), '--source', $old, '--destination', $Package.Root)
    $quoted = @($arguments | ForEach-Object { Quote-LegacyBootstrapArgument ([string]$_) }) -join ' '
    $psi = New-LegacyBootstrapProcessStartInfo -FileName $uv -Arguments $quoted -WorkingDirectory $Package.Root -Environment $environment -RedirectOutput
    Set-PreparationStatus '会話・記憶・設定をコピーして照合しています。元のNexus Arkは起動せずにお待ちください…'
    $process = [Diagnostics.Process]::Start($psi)
    if ($null -eq $process) { throw '移行プロセスを起動できません。' }
    try {
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        Wait-PreparationProcess -Process $process
        $code = $process.ExitCode
        $outputText = $stdoutTask.GetAwaiter().GetResult()
        $errorText = $stderrTask.GetAwaiter().GetResult()
        if (-not [string]::IsNullOrWhiteSpace($outputText)) { Write-Host $outputText }
        if (-not [string]::IsNullOrWhiteSpace($errorText)) { Write-Host $errorText }
    }
    finally { $process.Dispose() }
    if ($code -ne 0) { throw 'コピーと照合が完了しませんでした。元のフォルダーを保持し、新版は起動せず、ZIPを別の新しいフォルダーへ展開し直してください。' }
}

function Invoke-FreshImportMain {
    Add-Type -AssemblyName System.Windows.Forms
    $package = Get-PackageSource
    $prompt = "現在使っているNexus Arkを通常終了してから進んでください。`r`nこの新版へコピーし、元のフォルダーは変更しません。`r`n旧版と新版は同時に使用しないでください。"
    if ([System.Windows.Forms.MessageBox]::Show($prompt, 'Nexus Ark — 新版へのデータ引き継ぎ', 'OKCancel', 'Information') -ne 'OK') { return }
    $picker = New-Object System.Windows.Forms.FolderBrowserDialog
    $picker.Description = '現在使っている旧版（またはバックアップ）のNexus Arkフォルダーを選択してください。新版ではありません。'
    $picker.ShowNewFolderButton = $false
    try {
        if ($picker.ShowDialog() -ne 'OK') { return }
        $selected = $picker.SelectedPath
    } finally { $picker.Dispose() }
    $confirm = "引き継ぎ元（現在使っているフォルダー）：`r`n$selected`r`n`r`n引き継ぎ先（この新版）：`r`n$($package.Root)`r`n`r`nこの内容でコピーと照合を開始しますか？"
    if ([System.Windows.Forms.MessageBox]::Show($confirm, '引き継ぎ元・先の確認', 'OKCancel', 'Information') -ne 'OK') { return }
    try {
        Open-PreparationProgress -TargetRoot $selected
        $script:PreparationProgress.Form.Text = 'Nexus Ark — データのコピー・照合中'
        $script:PreparationProgress.NoteLabel.Text = "数分かかることがあります。このままお待ちください。`r`n完了後は、新版フォルダーの Start.bat から起動します。"
        Invoke-FreshDataImport -OldRoot $selected -Package $package
    } finally { Close-PreparationProgress }
    [System.Windows.Forms.MessageBox]::Show("コピーと照合が完了しました。`r`nこの新版フォルダーの Start.bat から起動し、会話・記憶・設定を確認してください。`r`n元のフォルダーとバックアップは、確認が済むまで残してください。", 'データの引き継ぎ完了', 'OK', 'Information') | Out-Null
}

if (-not $script:ImportDotSourced) {
    try { Invoke-FreshImportMain; exit 0 }
    catch {
        Write-Host ('[Nexus Ark] ' + $_.Exception.Message)
        Close-PreparationProgress
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '引き継ぎを完了できませんでした', 'OK', 'Warning') | Out-Null
        exit 1
    }
}
