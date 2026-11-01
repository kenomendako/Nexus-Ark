<# 初回移行の読み取り診断。選択先を変更せず、固定項目だけを返す。 #>
[CmdletBinding()]
param([string]$TargetRoot = '', [string]$PackageRoot = '')
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

function Get-DiagnosticLayout {
    param([string]$Root)
    $startPath = Assert-DiagnosticPath (Join-Path $Root 'Start.bat')
    $start = [ordered]@{ present = [IO.File]::Exists($startPath); sha256 = ''; legacy_robocopy = $false; modern_host = $false }
    if ($start.present) {
        if ((Get-Item -LiteralPath $startPath -Force).Length -gt 131072) { throw '起動用ファイルが大きすぎます。' }
        $start.sha256 = (Get-FileHash -LiteralPath $startPath -Algorithm SHA256).Hash.ToLowerInvariant()
        # ASCII identifiers suffice even if the launcher uses OEM encoding.
        $text = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($startPath))
        $start.legacy_robocopy = $text -match '(?i)\brobocopy\b'
        $start.modern_host = $text -match 'update_host\.supervisor'
    }
    $presence = [ordered]@{}
    foreach ($name in @('app','app\nexus_ark.py','app\pyproject.toml','app\uv.lock','updater','updater\current','updater\current\update_host','runtime','metadata','update_recovery','updater.next','app.next','.venv.next')) {
        $path = Assert-DiagnosticPath (Join-Path $Root $name)
        $presence[$name] = [ordered]@{ directory = [IO.Directory]::Exists($path); file = [IO.File]::Exists($path) }
    }
    # This is diagnostic comparison against the shipped 0.89 generation, not
    # authorization to run/repair files. Never import modules from the target.
    $expected = [ordered]@{
        '__init__.py' = 'e58a28a84176ff2ef4909f29709eb0efe75455d59abf1368f64c829caef24ad5'
        'contracts.py' = '958c0c669f716b4eeb04083a1ba39eba913d90bfd7baae7b9c9a15bad848af20'
        'runtime.py' = '3f446f01094438b75974a60215e5d670f90175d83bc2f62a0fb9aff45781245b'
        'runtime_archive.py' = '15b6fd5835efebcb4a02210c7f7ebeffd52a86d4776dfeca109c9e068fb7dbcc'
        'supervisor.py' = '7bbdc5e33f87aca6c8b76e64393825fe24612e50443c178406944588dff4be30'
        'transaction.py' = '946a100fd0f5b61746f725f194e6c214684db9378e26df6ef406b6d1c35d48d9'
        'trial.py' = 'a0bc50e42e462bf35dac908477b7863ac4a6031e8dcbb8a054f3bd3d4a80f326'
        'windows_processes.py' = 'd6c43c7327dde06c021800f87d800a86bdf11550c46411ba4c73c7c30f171641'
    }
    $hostFiles = [ordered]@{}
    foreach ($name in $expected.Keys) {
        $path = Assert-DiagnosticPath (Join-Path $Root ('updater\current\update_host\' + $name))
        $entry = [ordered]@{ present = [IO.File]::Exists($path); matches_089 = $false }
        if ($entry.present -and (Get-Item -LiteralPath $path -Force).Length -le 524288) {
            $entry.matches_089 = ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -eq $expected[$name])
        }
        $hostFiles[$name] = $entry
    }
    $extraCounts = [ordered]@{}
    foreach ($scope in @('current','modules')) {
        $relative = if ($scope -eq 'current') { 'updater\current' } else { 'updater\current\update_host' }
        $allowed = if ($scope -eq 'current') { @('update_host','__pycache__') } else { @($expected.Keys) + @('__pycache__') }
        $path = Assert-DiagnosticPath (Join-Path $Root $relative)
        $count = 0
        if ([IO.Directory]::Exists($path)) {
            foreach ($item in @(Get-ChildItem -LiteralPath $path -Force | Select-Object -First 257)) {
                if ($allowed -notcontains $item.Name) { $count++ }
            }
        }
        $extraCounts[$scope] = $count
    }
    return [ordered]@{ start = $start; paths = $presence; host_files = $hostFiles; extra_entry_counts = $extraCounts }
}

function Get-LegacyBootstrapDiagnostic {
    param([Parameter(Mandatory=$true)][string]$Root, [string]$PackageRoot = '')
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
        schema = 1; tool_revision = 2; tool = 'Nexus Ark 初回移行の読み取り診断'; read_only = $true
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
    $result['layout'] = Get-DiagnosticLayout $rootFull
    if ($PackageRoot) {
        $package = (Assert-DiagnosticPath $PackageRoot).TrimEnd('\')
        $sameOrNested = $package.Equals($rootFull, [StringComparison]::OrdinalIgnoreCase) -or
            $package.StartsWith($rootFull + '\', [StringComparison]::OrdinalIgnoreCase) -or
            $rootFull.StartsWith($package + '\', [StringComparison]::OrdinalIgnoreCase)
        $result['package'] = [ordered]@{ same_or_nested = $sameOrNested; layout = (Get-DiagnosticLayout $package) }
    }
    return $result
}

function Save-DiagnosticReport {
    param($Result, [string]$TargetRoot, [string]$Directory = $PSScriptRoot, [string]$PackageRoot = '')
    $directoryFull = Assert-DiagnosticPath $Directory
    $rootFull = (Assert-DiagnosticPath $TargetRoot).TrimEnd('\')
    $workspace = Join-Path ([IO.Path]::GetDirectoryName($rootFull)) ('.' + [IO.Path]::GetFileName($rootFull) + '.nexus-secure-update')
    $protectedPaths = @($rootFull, $workspace)
    if ($PackageRoot) { $protectedPaths += (Assert-DiagnosticPath $PackageRoot).TrimEnd('\') }
    foreach ($protected in $protectedPaths) {
        if ($directoryFull.Equals($protected, [StringComparison]::OrdinalIgnoreCase) -or
            $directoryFull.StartsWith($protected.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw '診断対象内へは結果を保存しません。'
        }
    }
    $name = 'NexusArk-legacy-diagnostic-' + [Guid]::NewGuid().ToString('N') + '.json'
    $json = $Result | ConvertTo-Json -Depth 8
    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($json)
    $stream = [IO.File]::Open((Join-Path $directoryFull $name), [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    return $name
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
        if (-not $PackageRoot) {
            Add-Type -AssemblyName System.Windows.Forms
            $dialog = New-Object Windows.Forms.FolderBrowserDialog
            try {
                $dialog.Description = '2回目：新しい本体ZIP NexusArk_0.5.0.89を展開したフォルダー（PrepareSecureUpdate.batがある場所）を選んでください。'
                $dialog.ShowNewFolderButton = $false
                if ($dialog.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { exit 0 }
                $PackageRoot = $dialog.SelectedPath
            } finally { $dialog.Dispose() }
        }
        $result = Get-LegacyBootstrapDiagnostic -Root $TargetRoot -PackageRoot $PackageRoot
        $result | ConvertTo-Json -Depth 8
        try {
            $name = Save-DiagnosticReport -Result $result -TargetRoot $TargetRoot -PackageRoot $PackageRoot
            Write-Host ('このツールと同じフォルダーに保存した「' + $name + '」をお知らせください。')
        } catch { Write-Host '結果ファイルを保存できませんでした。上の診断結果をコピーまたは画像でお知らせください。' }
        Write-Host '本体・バックアップは変更していません。'
    } catch {
        # OS例外のpath/内部本文は表示しない。
        Write-Host '診断を完了できませんでした。フォルダーや記録は変更していません。この画面をお知らせください。'
        exit 1
    }
}
