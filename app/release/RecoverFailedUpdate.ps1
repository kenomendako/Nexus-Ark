<#
Explicit recovery for an update that has already rolled back. Inspection never
changes the selected installation. Recovery requires a complete approved package,
a stopped target, backup confirmation, and the original complete staged release.
No process is terminated and no generation or lock is deleted.
#>
[CmdletBinding()]
param([switch]$InspectOnly)

$script:IsRecoveryDotSourced = ($MyInvocation.InvocationName -eq '.')
. (Join-Path $PSScriptRoot 'PrepareExistingUpdate.ps1')
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)

function Get-UpdateDeleteAccessProbe {
    param([Parameter(Mandatory = $true)][string]$Path)
    # DELETE権限のhandleを開いて即時閉じるだけ。削除・rename・書込みはしない。
    # 子file全件のlockを検査するものではなく、成功も更新成功の保証にはしない。
    if (-not ('NexusArkReadOnlyProbe' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class NexusArkReadOnlyProbe {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern SafeFileHandle CreateFileW(string path, uint access, uint share,
        IntPtr security, uint disposition, uint flags, IntPtr template);
    public static int Check(string path) {
        using (SafeFileHandle handle = CreateFileW(path, 0x10000, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {
            return handle.IsInvalid ? Marshal.GetLastWin32Error() : 0;
        }
    }
}
'@
    }
    $errorNumber = [NexusArkReadOnlyProbe]::Check($Path)
    return [ordered]@{ opened = ($errorNumber -eq 0); winerror = $errorNumber }
}

function Get-FailedUpdateDiagnostic {
    param([Parameter(Mandatory = $true)][string]$TargetRoot)
    $root = Assert-RegularDirectory $TargetRoot 'Nexus Arkフォルダー'
    $path = Join-Path $root 'update_recovery\transaction.json'
    $file = Assert-RegularFile $path '更新記録'
    if ($file.Length -gt 65536) { throw '更新記録が大きすぎるため停止しました。' }
    $journal = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    $result = [ordered]@{ schema = 1; tool = 'Nexus Ark 更新失敗の読み取り診断'; read_only = $true }
    foreach ($name in @('phase', 'failure', 'target_version')) {
        $value = [string]$journal.$name
        if ($value -and $value -notmatch '^[a-zA-Z0-9_.-]{1,80}$') { throw '更新記録の診断項目が不正です。' }
        $result[$name] = $value
    }
    $detail = [ordered]@{}
    if (($journal.PSObject.Properties.Name -contains 'failure_detail') -and $null -ne $journal.failure_detail) {
        foreach ($name in @('stage', 'component', 'action', 'exception_type')) {
            if ($journal.failure_detail.PSObject.Properties.Name -contains $name) {
                $value = [string]$journal.failure_detail.$name
                if ($value -and $value -notmatch '^[a-zA-Z0-9_.:-]{1,80}$') { throw '失敗操作の診断項目が不正です。' }
                $detail[$name] = $value
            }
        }
        foreach ($name in @('errno', 'winerror')) {
            if ($journal.failure_detail.PSObject.Properties.Name -contains $name) {
                $value = $journal.failure_detail.$name
                if ($null -ne $value -and $value -isnot [int] -and $value -isnot [long]) { throw 'エラー番号の形式が不正です。' }
                $detail[$name] = $value
            }
        }
    }
    $result['failure_detail'] = $detail
    $presence = [ordered]@{}
    foreach ($name in @('app', 'app.next', 'app.previous', '.venv', '.venv.next', '.venv.previous', 'runtime', 'runtime.next', 'runtime.previous', 'update_recovery\transaction.json.tmp', 'update_recovery\transaction.lock', 'update_recovery\retry-pending.json')) {
        $presence[$name] = [bool](Test-SafePathOrAbsent (Join-Path $root $name))
    }
    $result['present'] = $presence
    $probes = [ordered]@{}
    foreach ($name in @('app', 'app\nexus_ark.py', '.venv', '.venv\Scripts\python.exe', 'runtime', 'runtime\node.exe', 'update_recovery\transaction.json')) {
        $candidate = Join-Path $root $name
        if (Test-SafePathOrAbsent $candidate) { $probes[$name] = Get-UpdateDeleteAccessProbe $candidate }
    }
    $result['delete_access_probe'] = $probes
    $stopped = $true
    try {
        Assert-NexusLockClear (Join-Path $root 'app\nexus_ark.lock')
        Assert-TargetProcessesStopped $root
    }
    catch { $stopped = $false }
    $result['stopped_confirmed'] = $stopped
    return $result
}

function Invoke-RetryPreparation {
    param(
        [Parameter(Mandatory = $true)][string]$UvExecutable,
        [Parameter(Mandatory = $true)][hashtable]$Target
    )
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $UvExecutable
    $psi.Arguments = '--no-config run --no-project --managed-python --python cpython@3.12.16 python -m update_host.supervisor --prepare-retry --root ' + (ConvertTo-WindowsArgument $Target.Root)
    $psi.WorkingDirectory = $Target.Root
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
    $psi.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
    $psi.EnvironmentVariables['PYTHONPATH'] = Join-Path $Target.Root 'updater\current'
    $psi.EnvironmentVariables['PATH'] = (Split-Path -Parent $UvExecutable) + ';' + [string]$env:PATH
    $psi.EnvironmentVariables['PATHEXT'] = '.COM;.EXE;.BAT;.CMD'
    $psi.EnvironmentVariables['PYTHONUTF8'] = '1'
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $psi.EnvironmentVariables['PYTHONDONTWRITEBYTECODE'] = '1'
    foreach ($name in @('VIRTUAL_ENV', 'UV_PROJECT_ENVIRONMENT', 'PYTHONHOME')) { $psi.EnvironmentVariables.Remove($name) }
    $process = [Diagnostics.Process]::Start($psi)
    if ($null -eq $process) { throw '再試行の準備プロセスを開始できません。' }
    try {
        $outputTask = $process.StandardOutput.ReadToEndAsync()
        $errorTask = $process.StandardError.ReadToEndAsync()
        Wait-PreparationProcess -Process $process
        $outputText = $outputTask.GetAwaiter().GetResult()
        $errorTask.GetAwaiter().GetResult() | Out-Null
        if ($process.ExitCode -ne 0) { throw '再試行の準備を完了できませんでした。世代を削除せず、読み取り診断を実行してください。' }
        try { $result = $outputText.Trim() | ConvertFrom-Json }
        catch { throw '再試行準備の完了記録を確認できません。重ねて実行せず診断してください。' }
        if ([string]$result.phase -ne 'prepared') { throw '再試行準備が完了していません。' }
        return $result
    }
    finally { $process.Dispose() }
}

function Invoke-FailedUpdateRecovery {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Source,
        [Parameter(Mandatory = $true)][string]$TargetRoot
    )
    if (Test-SameOrNestedPath $Source.Root $TargetRoot) { throw '展開した配布物と今までのフォルダーは別の場所にしてください。' }
    $mutex = Enter-PreparationMutex -Root $TargetRoot
    try {
        Open-PreparationProgress $TargetRoot
        Set-PreparationStatus '停止状態と失敗した更新の記録を確認しています…'
        $target = Assert-TargetRoot -Root $TargetRoot -Source $Source -AllowRolledBackNext
        $diagnostic = Get-FailedUpdateDiagnostic $target.Root
        if ($diagnostic.phase -ne 'rolled_back' -or -not $diagnostic.stopped_confirmed) {
            throw '旧版への復旧完了と本体の停止を確認できません。再試行せず読み取り診断を実行してください。'
        }
        foreach ($probe in $diagnostic.delete_access_probe.Values) {
            if (-not $probe.opened) { throw '本体または更新記録を使用中、または権限を確認できません。再試行せず読み取り診断を保存してください。' }
        }
        $confirmation = Show-PreparationMessage -Text "Nexus Arkを通常終了し、フォルダー全体のバックアップを保持していますか？`r`n`r`n失敗時の記録と準備途中の世代を削除せず保管し、現在の会話・設定から更新を準備し直します。成功後は今までのフォルダーから起動確認を開始します。" -Kind Warning -Buttons YesNo
        if ($confirmation -ne [System.Windows.Forms.DialogResult]::Yes) { return $false }
        Set-PreparationStatus '更新用ツールを確認しています…'
        $uv = Invoke-UvRepair $Source
        Set-PreparationStatus '診断対応の保護hostを照合し、バックアップを残して準備しています…'
        Invoke-SecureHostMaintenance -Source $Source -TargetRoot $target.Root -AllowRolledBackNext | Out-Null
        $target = Assert-TargetRoot -Root $TargetRoot -Source $Source -AllowRolledBackNext
        Set-PreparationStatus '失敗時の証跡を保管し、現在の会話・設定から準備し直しています…'
        Invoke-RetryPreparation -UvExecutable $uv -Target $target | Out-Null
        Assert-NexusLockClear (Join-Path $target.App 'nexus_ark.lock')
        Assert-TargetProcessesStopped $target.Root
        Set-PreparationStatus '準備が完了しました。元のフォルダーから新版の起動確認を開始しています…'
        Start-SelectedRoot $target | Out-Null
        Close-PreparationProgress
        Show-PreparationMessage -Text "再試行の準備が完了し、今までのフォルダーから起動確認を開始しました。`r`n`r`nまだ更新成功の確定ではありません。起動後の本体バージョンと、会話・設定が保持されていることを確認してください。旧版に戻った場合は更新を繰り返さず、InspectFailedUpdate.batの診断結果をお知らせください。" -Kind Information -Buttons OK | Out-Null
        return $true
    }
    finally {
        Close-PreparationProgress
        $mutex.Dispose()
    }
}

function Invoke-RecoveryMain {
    param([switch]$ReadOnly)
    $root = Select-TargetRoot
    if (-not $root) { return 0 }
    if ($ReadOnly) {
        $result = Get-FailedUpdateDiagnostic $root
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.SaveFileDialog
        try {
            $dialog.Title = '診断結果の保存先を選択してください（キーや会話本文は含みません）'
            $dialog.Filter = 'JSON (*.json)|*.json'
            $dialog.FileName = 'NexusArk-update-diagnostic.json'
            if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { return 0 }
            if (Test-SameOrNestedPath $root $dialog.FileName) { throw '診断結果はNexus Arkフォルダーの外へ保存してください。' }
            Test-SafePathOrAbsent $dialog.FileName | Out-Null
            $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $dialog.FileName -Encoding UTF8
        }
        finally { $dialog.Dispose() }
        Show-PreparationMessage -Text '診断結果を保存しました。選択したNexus Arkの本体・lock・世代は変更していません。' | Out-Null
        return 0
    }
    $source = Get-PackageSource
    Invoke-FailedUpdateRecovery -Source $source -TargetRoot $root | Out-Null
    return 0
}

if (-not $script:IsRecoveryDotSourced) {
    try { exit (Invoke-RecoveryMain -ReadOnly:$InspectOnly) }
    catch {
        Close-PreparationProgress
        Show-PreparationMessage -Text '安全確認または再試行を完了できませんでした。フォルダー・lock・backupを削除せず、InspectFailedUpdate.batで読み取り診断を保存してお知らせください。' -Kind Error | Out-Null
        exit 1
    }
}
