<# 初回移行の読み取り診断。選択先を変更せず、固定項目だけを返す。 #>
[CmdletBinding()]
param([string]$TargetRoot = '')
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Assert-DiagnosticPath {
    param([string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -notmatch '^[A-Za-z]:\\') { throw 'ローカルドライブのフォルダーを選んでください。' }
    $current = $full
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $attributes = [IO.File]::GetAttributes($current)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'リンクを含む場所は、この診断では開きません。'
            }
        }
        $current = [IO.Path]::GetDirectoryName($current.TrimEnd('\'))
    }
    return $full
}

function Read-DiagnosticJson {
    param([string]$Path)
    $full = Assert-DiagnosticPath $Path
    $file = Get-Item -LiteralPath $full -Force
    if ($file.PSIsContainer -or $file.Length -gt 65536) { throw '診断用の記録を安全に読めません。' }
    return (Get-Content -LiteralPath $full -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Get-LegacyBootstrapDiagnostic {
    param([Parameter(Mandatory=$true)][string]$Root)
    $rootFull = (Assert-DiagnosticPath $Root).TrimEnd('\')
    if (-not [IO.Directory]::Exists($rootFull) -or -not [IO.File]::Exists((Join-Path $rootFull 'Start.bat'))) {
        throw '今まで使っていたNexus Arkフォルダー（Start.batのある場所）を選んでください。'
    }
    $parent = [IO.Path]::GetDirectoryName($rootFull)
    $workspace = Join-Path $parent ('.' + [IO.Path]::GetFileName($rootFull) + '.nexus-secure-update')
    $workspace = Assert-DiagnosticPath $workspace
    if (-not ('NexusLegacyReadOnlyProbe' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class NexusLegacyReadOnlyProbe {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern SafeFileHandle CreateFileW(string path, uint access, uint share,
        IntPtr security, uint disposition, uint flags, IntPtr template);
    public static int Check(string path, uint access) {
        // OPEN_EXISTING + full sharing + OPEN_REPARSE_POINT. No writes/rename/delete.
        using (SafeFileHandle h = CreateFileW(path, access, 7, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero)) {
            return h.IsInvalid ? Marshal.GetLastWin32Error() : 0;
        }
    }
}
'@
    }
    $locations = [ordered]@{
        root = $rootFull
        workspace = $workspace
        next_root = (Join-Path $workspace 'next-root')
        previous = (Join-Path $workspace 'previous')
    }
    $result = [ordered]@{
        schema = 1; tool = 'Nexus Ark 初回移行の読み取り診断'; read_only = $true
        folders = [ordered]@{}; receipt_state = 'missing'; versions = [ordered]@{}
        delete_access_probe = [ordered]@{}
        notes = @('選択先の削除・移動・書込み・起動・外部通信はしていません。',
            '成功したアクセス確認も、失敗当時の状態や次回の更新成功を保証しません。')
    }
    foreach ($label in $locations.Keys) {
        $path = Assert-DiagnosticPath $locations[$label]
        $present = [IO.Directory]::Exists($path)
        $result.folders[$label] = $present
        if ($present) {
            $number = [NexusLegacyReadOnlyProbe]::Check($path, 0x10000)
            $result.delete_access_probe[$label] = [ordered]@{ opened = ($number -eq 0); winerror = $number }
            $versionPath = Join-Path $path 'app\version.json'
            if ([IO.File]::Exists($versionPath)) {
                $value = Read-DiagnosticJson $versionPath
                if ($value.PSObject.Properties.Name -contains 'version') {
                    $version = [string]$value.version
                    if ($version -match '^\d{1,5}\.\d{1,5}\.\d{1,5}\.\d{1,5}$') { $result.versions[$label] = $version }
                }
            }
        }
    }
    $receiptPath = Join-Path $workspace 'receipt.json'
    if ([IO.File]::Exists($receiptPath)) {
        $receipt = Read-DiagnosticJson $receiptPath
        if ($receipt.PSObject.Properties.Name -notcontains 'state' -or [string]$receipt.state -notin @(
            'preparing','prepared','swap_intent','old_parked','new_promoted','committed','rolled_back','manual_recovery_required')) {
            throw '記録の処理状態が未知です。上書きせず停止しました。'
        }
        $result.receipt_state = [string]$receipt.state
    }
    $number = [NexusLegacyReadOnlyProbe]::Check($parent, 0x44)
    $result['parent_create_delete_child_access_probe'] = [ordered]@{ opened = ($number -eq 0); winerror = $number }
    return $result
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
        if (-not $TargetRoot) {
            Add-Type -AssemblyName System.Windows.Forms
            $dialog = New-Object Windows.Forms.FolderBrowserDialog
            try {
                $dialog.Description = '今まで使っていたNexus Arkフォルダーを選んでください。変更せず診断します。'
                $dialog.ShowNewFolderButton = $false
                if ($dialog.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { exit 0 }
                $TargetRoot = $dialog.SelectedPath
            } finally { $dialog.Dispose() }
        }
        Get-LegacyBootstrapDiagnostic $TargetRoot | ConvertTo-Json -Depth 6
        Write-Host '上の診断結果をお知らせください。本体・バックアップは変更していません。'
    } catch {
        # OS例外のpath/内部本文は表示しない。
        Write-Host '診断を完了できませんでした。フォルダーや記録は変更していません。この画面をお知らせください。'
        exit 1
    }
}
