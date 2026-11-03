<#!
Nexus Ark: safely repair the stable update host in an existing installation.

This helper is intentionally independent from the application update
transaction.  It accepts only a complete distribution updater/current tree as
the source and only a stopped, known installation root as the target.  The
receipt and every path recorded in it are target-root-relative; an absolute
source path never enters the persistent receipt.

When dot-sourced this file defines functions only.  Standalone invocation
prints one JSON result and returns a non-zero exit code on any rejection.
#>
[CmdletBinding()]
param(
    [string]$TargetRoot = '',
    [string]$SourceHostPath = '',
    [switch]$InspectOnly,
    [switch]$AllowRolledBackNext
)

$script:IsDotSourced = ($MyInvocation.InvocationName -eq '.')
. (Join-Path $PSScriptRoot 'SecureLauncherMaintenance.ps1')
if (-not $script:IsDotSourced) {
    Set-StrictMode -Version 2.0
    $ErrorActionPreference = 'Stop'
    [Console]::OutputEncoding = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList @($false)
}

# 89d5c9eb stable host: seven files.  5746fd60 adds windows_processes.py and
# changes the transaction/supervisor files.  The shipped 0.77 host is retained
# as known_077, the 0.87/0.88 host as known_088, and the complete 0.89--0.93
# eight-file host is retained as historical generations.  The current source
# is a separate known_new generation because supervisor.py has changed.
# The source is not an authorization boundary by itself: every complete
# generation is pinned below and receipts must carry one of these exact maps.
# Receipts from prior helpers may label any historical generation as
# ``known_new``; they are accepted only when their complete map proves that
# exact prior generation.  No state-only or partial-map fallback is accepted.
$script:KnownOldHostSha256 = [ordered]@{
    '__init__.py' = 'e58a28a84176ff2ef4909f29709eb0efe75455d59abf1368f64c829caef24ad5'
    'contracts.py' = '4018fe870273e8fa38b5e07b60bbb49d51edd2feaec38f22e087d528f3fa115f'
    'runtime.py' = '38490c97b317a73ae029c7b6e9b8765625a8e9229388963130cf955334b303d3'
    'runtime_archive.py' = 'd4484f97a0d71531f9ac046b36d98bb47f092149335b559e36158a2493922b25'
    'supervisor.py' = '66a7b8b231c8787f379d35ef3d5502021c136333b3849f5b99068e00e7bafbaa'
    'transaction.py' = '14be150f67201224c0b6a63edcbdb13a42b11a1b64e932d812b4a138af3231ba'
    'trial.py' = 'a0bc50e42e462bf35dac908477b7863ac4a6031e8dcbb8a054f3bd3d4a80f326'
}
$script:KnownOld8HostSha256 = [ordered]@{
    '__init__.py' = 'e58a28a84176ff2ef4909f29709eb0efe75455d59abf1368f64c829caef24ad5'
    'contracts.py' = '4018fe870273e8fa38b5e07b60bbb49d51edd2feaec38f22e087d528f3fa115f'
    'runtime.py' = '38490c97b317a73ae029c7b6e9b8765625a8e9229388963130cf955334b303d3'
    'runtime_archive.py' = 'd4484f97a0d71531f9ac046b36d98bb47f092149335b559e36158a2493922b25'
    'supervisor.py' = '91f59ac334a8c4d9955605e147c7519d3020992ebd9bf00c069bbfbe13b07b2e'
    'transaction.py' = '1a06f6ca858b040cd877de5bf8f31ae00a003486dbd9de56ea22dc5fabae7fe7'
    'trial.py' = 'a0bc50e42e462bf35dac908477b7863ac4a6031e8dcbb8a054f3bd3d4a80f326'
    'windows_processes.py' = 'd6c43c7327dde06c021800f87d800a86bdf11550c46411ba4c73c7c30f171641'
}
$script:Known077HostSha256 = [ordered]@{
    '__init__.py' = 'e58a28a84176ff2ef4909f29709eb0efe75455d59abf1368f64c829caef24ad5'
    'contracts.py' = '958c0c669f716b4eeb04083a1ba39eba913d90bfd7baae7b9c9a15bad848af20'
    'runtime.py' = '38490c97b317a73ae029c7b6e9b8765625a8e9229388963130cf955334b303d3'
    'runtime_archive.py' = 'd4484f97a0d71531f9ac046b36d98bb47f092149335b559e36158a2493922b25'
    'supervisor.py' = '91f59ac334a8c4d9955605e147c7519d3020992ebd9bf00c069bbfbe13b07b2e'
    'transaction.py' = '1a06f6ca858b040cd877de5bf8f31ae00a003486dbd9de56ea22dc5fabae7fe7'
    'trial.py' = 'a0bc50e42e462bf35dac908477b7863ac4a6031e8dcbb8a054f3bd3d4a80f326'
    'windows_processes.py' = 'd6c43c7327dde06c021800f87d800a86bdf11550c46411ba4c73c7c30f171641'
}
$script:Known085HostSha256 = [ordered]@{
    '__init__.py' = 'e58a28a84176ff2ef4909f29709eb0efe75455d59abf1368f64c829caef24ad5'
    'contracts.py' = '958c0c669f716b4eeb04083a1ba39eba913d90bfd7baae7b9c9a15bad848af20'
    'runtime.py' = '3f446f01094438b75974a60215e5d670f90175d83bc2f62a0fb9aff45781245b'
    'runtime_archive.py' = '15b6fd5835efebcb4a02210c7f7ebeffd52a86d4776dfeca109c9e068fb7dbcc'
    'supervisor.py' = '91f59ac334a8c4d9955605e147c7519d3020992ebd9bf00c069bbfbe13b07b2e'
    'transaction.py' = '1a06f6ca858b040cd877de5bf8f31ae00a003486dbd9de56ea22dc5fabae7fe7'
    'trial.py' = 'a0bc50e42e462bf35dac908477b7863ac4a6031e8dcbb8a054f3bd3d4a80f326'
    'windows_processes.py' = 'd6c43c7327dde06c021800f87d800a86bdf11550c46411ba4c73c7c30f171641'
}
$script:Known088HostSha256 = [ordered]@{
    '__init__.py' = 'e58a28a84176ff2ef4909f29709eb0efe75455d59abf1368f64c829caef24ad5'
    'contracts.py' = '958c0c669f716b4eeb04083a1ba39eba913d90bfd7baae7b9c9a15bad848af20'
    'runtime.py' = '3f446f01094438b75974a60215e5d670f90175d83bc2f62a0fb9aff45781245b'
    'runtime_archive.py' = '15b6fd5835efebcb4a02210c7f7ebeffd52a86d4776dfeca109c9e068fb7dbcc'
    'supervisor.py' = '4aaafb02b9edd232e0f834d28a5de60f8bc95e88ba8beaf513ad217abe29afc6'
    'transaction.py' = '946a100fd0f5b61746f725f194e6c214684db9378e26df6ef406b6d1c35d48d9'
    'trial.py' = 'a0bc50e42e462bf35dac908477b7863ac4a6031e8dcbb8a054f3bd3d4a80f326'
    'windows_processes.py' = 'd6c43c7327dde06c021800f87d800a86bdf11550c46411ba4c73c7c30f171641'
}
$script:Known093HostSha256 = [ordered]@{
    '__init__.py' = 'e58a28a84176ff2ef4909f29709eb0efe75455d59abf1368f64c829caef24ad5'
    'contracts.py' = '958c0c669f716b4eeb04083a1ba39eba913d90bfd7baae7b9c9a15bad848af20'
    'runtime.py' = '3f446f01094438b75974a60215e5d670f90175d83bc2f62a0fb9aff45781245b'
    'runtime_archive.py' = '15b6fd5835efebcb4a02210c7f7ebeffd52a86d4776dfeca109c9e068fb7dbcc'
    'supervisor.py' = '7bbdc5e33f87aca6c8b76e64393825fe24612e50443c178406944588dff4be30'
    'transaction.py' = '946a100fd0f5b61746f725f194e6c214684db9378e26df6ef406b6d1c35d48d9'
    'trial.py' = 'a0bc50e42e462bf35dac908477b7863ac4a6031e8dcbb8a054f3bd3d4a80f326'
    'windows_processes.py' = 'd6c43c7327dde06c021800f87d800a86bdf11550c46411ba4c73c7c30f171641'
}
$script:KnownNewHostSha256 = [ordered]@{
    '__init__.py' = 'e58a28a84176ff2ef4909f29709eb0efe75455d59abf1368f64c829caef24ad5'
    'contracts.py' = '958c0c669f716b4eeb04083a1ba39eba913d90bfd7baae7b9c9a15bad848af20'
    'runtime.py' = '3f446f01094438b75974a60215e5d670f90175d83bc2f62a0fb9aff45781245b'
    'runtime_archive.py' = '15b6fd5835efebcb4a02210c7f7ebeffd52a86d4776dfeca109c9e068fb7dbcc'
    'supervisor.py' = 'd2539b3f81721145326367202424327283feba1296541c79fb01799d71027897'
    'transaction.py' = '946a100fd0f5b61746f725f194e6c214684db9378e26df6ef406b6d1c35d48d9'
    'trial.py' = 'a0bc50e42e462bf35dac908477b7863ac4a6031e8dcbb8a054f3bd3d4a80f326'
    'windows_processes.py' = 'd6c43c7327dde06c021800f87d800a86bdf11550c46411ba4c73c7c30f171641'
}

function Get-KnownHostGeneration {
    param([Parameter(Mandatory = $true)][string]$State)
    switch ($State) {
        'known_old' {
            return [ordered]@{ state = 'known_old'; rank = 0; map = $script:KnownOldHostSha256 }
        }
        'known_old8' {
            return [ordered]@{ state = 'known_old8'; rank = 1; map = $script:KnownOld8HostSha256 }
        }
        'known_077' {
            return [ordered]@{ state = 'known_077'; rank = 2; map = $script:Known077HostSha256 }
        }
        'known_085' {
            return [ordered]@{ state = 'known_085'; rank = 3; map = $script:Known085HostSha256 }
        }
        'known_088' {
            return [ordered]@{ state = 'known_088'; rank = 4; map = $script:Known088HostSha256 }
        }
        'known_093' {
            return [ordered]@{ state = 'known_093'; rank = 5; map = $script:Known093HostSha256 }
        }
        'known_new' {
            return [ordered]@{ state = 'known_new'; rank = 6; map = $script:KnownNewHostSha256 }
        }
        default { throw 'host世代が許可済み一覧にありません。' }
    }
}

# Keep this list synchronized with RefreshSecureLauncher.ps1.  Both LF and
# Windows-package CRLF forms are accepted, but no other Start is replaceable.
$script:KnownCanonicalStartSha256 = @(
    'ba5c48ebae2dec33a4da3ee8b644f3e1f99322eb77a23a16ddb675d23b9eb5c7', # 0.61/0.62 LF
    '34529f95471c044bf199929cc24523f201f71588e066b74616e416523042770d', # 0.61/0.62 Windows CRLF
    'ca8b0cc1e38d4dcc40436f0744436e3c65bcc904cd894c2c263f3eaff02d53d1',
    'b58de9707250d4657d199025b75a1e3c50c315272362c1fe593470b2a163ec20',
    '837c25b9c4b4c5fb30d7f2f6d6081ed8daa4f0bfcbe2aeb31cddca9ddeb24b39',
    'e3070fbc412577aff121d8a8aff33bb0bd15c790fce205580926569299de7c37',
    '3bafb0efb475fdd9bd9e686739a461018c1612dc092a356b9bf8a9ed2538684a', # 3.12.16 LF before import guard
    '184427c65309ebc1f45b2d2a2c5f71ee2ec1e70cb40c458a35449c1b06705241', # 3.12.16 CRLF before import guard
    'd01dc08b7184ad2ea2eb9679e2ff1d0a47e9446fa5f80f9907e9f36766c5410a', # 3.12.16 LF with import guard
    '94efac6d885ca3e7745cc6772621fd9712763cdcff6807cb38198383c7ec03f3' # 3.12.16 CRLF with import guard
)

function Get-NormalizedPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'パスが空です。' }
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($full)
    if ($full.Length -gt $root.Length) { $full = $full.TrimEnd([char[]]@('\', '/')) }
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
        $item = Get-Item -LiteralPath (Get-SecureReceiptIoPath $cursor) -Force -ErrorAction SilentlyContinue
        if ($null -eq $item) {
            if (-not $AllowMissing) { throw ('{0} を確認できません。' -f $Label) }
        }
        elseif (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw ('{0} にリンクまたは再解析ポイントが含まれます。' -f $Label)
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $cursor) { break }
        $cursor = $parent
    }
}

function Assert-PathUnder {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $full = Get-NormalizedPath $Path
    $base = Get-NormalizedPath $Root
    $prefix = $base.TrimEnd([char[]]@('\', '/')) + '\'
    if (-not ([StringComparer]::OrdinalIgnoreCase.Equals($full, $base) -or
            $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase))) {
        throw ('{0} が対象rootの外側です。' -f $Label)
    }
    return $full
}

function Test-SameOrNestedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )
    $leftFull = Get-NormalizedPath $Left
    $rightFull = Get-NormalizedPath $Right
    if ([StringComparer]::OrdinalIgnoreCase.Equals($leftFull, $rightFull)) { return $true }
    $leftPrefix = $leftFull.TrimEnd([char[]]@('\', '/')) + '\'
    $rightPrefix = $rightFull.TrimEnd([char[]]@('\', '/')) + '\'
    return ($leftFull.StartsWith($rightPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        $rightFull.StartsWith($leftPrefix, [StringComparison]::OrdinalIgnoreCase))
}

function Assert-RegularDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$AllowMissing
    )
    $full = Get-NormalizedPath $Path
    Assert-NoReparseAncestors -Path $full -Label $Label -AllowMissing:$AllowMissing
    $item = Get-Item -LiteralPath (Get-SecureReceiptIoPath $full) -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        if ($AllowMissing) { return $full }
        throw ('{0} が見つかりません。' -f $Label)
    }
    if (-not $item.PSIsContainer -or (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw ('{0} が通常のフォルダーではありません。' -f $Label)
    }
    return $full
}

function Assert-RegularFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$AllowMissing
    )
    $full = Get-NormalizedPath $Path
    Assert-NoReparseAncestors -Path $full -Label $Label -AllowMissing:$AllowMissing
    $item = Get-Item -LiteralPath (Get-SecureReceiptIoPath $full) -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        if ($AllowMissing) { return $full }
        throw ('{0} が見つかりません。' -f $Label)
    }
    if ($item.PSIsContainer -or (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw ('{0} が通常のファイルではありません。' -f $Label)
    }
    return $full
}

function Get-SecureSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [IO.File]::Open(
        (Get-SecureReceiptIoPath $Path),
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        }
        finally { $sha.Dispose() }
    }
    finally { $stream.Dispose() }
}

function Get-RelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )
    $full = Get-NormalizedPath $Path
    $base = Get-NormalizedPath $Root
    $prefix = $base.TrimEnd([char[]]@('\', '/')) + '\'
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw '相対pathを対象root外へ作成できません。'
    }
    return $full.Substring($prefix.Length).Replace('/', '\')
}

function Resolve-TargetRelative {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Relative,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ([string]::IsNullOrWhiteSpace($Relative) -or
        [IO.Path]::IsPathRooted($Relative) -or
        $Relative.Contains(':') -or
        $Relative -match '(^|[\\/])\.\.([\\/]|$)') {
        throw ('{0} が固定相対pathではありません。' -f $Label)
    }
    $full = Get-NormalizedPath (Join-Path $Root $Relative)
    Assert-PathUnder -Path $full -Root $Root -Label $Label | Out-Null
    return $full
}

function Test-PycacheRelative {
    param([Parameter(Mandatory = $true)][string]$Relative)
    foreach ($part in ($Relative -split '\\')) {
        if ($part -ieq '__pycache__') { return $true }
    }
    return $false
}

function Get-SecureInventory {
    param([Parameter(Mandatory = $true)][string]$Root)
    $rootPath = Assert-RegularDirectory -Path $Root -Label 'host tree'
    $records = New-Object System.Collections.ArrayList
    $pending = New-Object System.Collections.Stack
    $pending.Push($rootPath)
    while ($pending.Count -gt 0) {
        $directory = [string]$pending.Pop()
        foreach ($item in @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)) {
            $itemPath = Get-NormalizedPath $item.FullName
            Assert-NoReparseAncestors -Path $itemPath -Label 'host inventory item' | Out-Null
            $relative = Get-RelativePath -Path $itemPath -Root $rootPath
            $ignored = Test-PycacheRelative -Relative $relative
            if ($item.PSIsContainer) {
                [void]$records.Add([ordered]@{
                    relative = $relative
                    kind = 'directory'
                    ignored = $ignored
                })
                $pending.Push($itemPath)
            }
            else {
                [void]$records.Add([ordered]@{
                    relative = $relative
                    kind = 'file'
                    ignored = $ignored
                    length = [int64]$item.Length
                    sha256 = Get-SecureSha256 -Path $itemPath
                })
            }
        }
    }
    return @($records | ForEach-Object { $_ })
}

function Compare-HostInventory {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Expected,
        [Parameter(Mandatory = $true)][string]$ExpectedState
    )
    $records = Get-SecureInventory -Root $Root
    $actual = @{}
    foreach ($record in $records) {
        if ($record.ignored) { continue }
        $relative = [string]$record.relative
        $key = $relative.ToLowerInvariant()
        if ($actual.ContainsKey($key)) { throw 'host inventoryにcase違いの重複pathがあります。' }
        $actual[$key] = $record
        if ($record.kind -eq 'directory' -and $relative -ine 'update_host') {
            throw 'host inventoryに未知のdirectoryがあります。'
        }
    }
    $expectedKeys = @($Expected.Keys | ForEach-Object { ([string]$_).ToLowerInvariant() })
    $actualFiles = @($actual.Keys | Where-Object { $actual[$_].kind -eq 'file' })
    foreach ($key in $actualFiles) {
        if ($expectedKeys -notcontains $key) { throw 'host inventoryに未知のfileがあります。' }
    }
    foreach ($key in $expectedKeys) {
        if (-not $actual.ContainsKey($key) -or $actual[$key].kind -ne 'file') {
            throw 'host inventoryの必須fileが不足しています。'
        }
        if ([string]$actual[$key].sha256 -ne [string]$Expected[$key]) {
            throw 'host inventoryのSHA256が既知版と一致しません。'
        }
    }
    $files = [ordered]@{}
    foreach ($key in $expectedKeys) {
        $files[[string]$actual[$key].relative] = [string]$actual[$key].sha256
    }
    return [ordered]@{
        state = $ExpectedState
        files = $files
        ignored_pycache = @($records | Where-Object { $_.ignored }).Count
    }
}

function Compare-CurrentInventory {
    <#
    The public SourceHostPath is distribution/updater/current.  The fixed
    allowlist is for its update_host child, but the current directory itself
    is also an authorization boundary: only that child (and harmless regular
    __pycache__ trees) may be present.  This keeps a package-side extra file
    from becoming part of the persistent update-host state.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Expected,
        [Parameter(Mandatory = $true)][string]$ExpectedState
    )
    $current = Assert-RegularDirectory -Path $Root -Label 'updater/current'
    $hostDirectory = Join-Path $current 'update_host'
    Assert-RegularDirectory -Path $hostDirectory -Label 'updater/current/update_host' | Out-Null
    foreach ($item in @(Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop)) {
        Assert-NoReparseAncestors -Path $item.FullName -Label 'current inventory item' | Out-Null
        if ($item.Name -ieq 'update_host') { continue }
        if ($item.Name -ieq '__pycache__') {
            Get-SecureInventory -Root $item.FullName | Out-Null
            continue
        }
        throw 'updater/currentに未知のfileまたはdirectoryがあります。'
    }
    $hostInventory = Compare-HostInventory -Root $hostDirectory -Expected $Expected -ExpectedState $ExpectedState
    return [ordered]@{
        state = $hostInventory.state
        files = $hostInventory.files
        ignored_pycache = [int]$hostInventory.ignored_pycache
        host_path = $hostDirectory
    }
}

function Assert-CacheFreeCurrent {
    param([Parameter(Mandatory = $true)][string]$Root)
    foreach ($record in @(Get-SecureInventory -Root $Root)) {
        if ((Test-PycacheRelative -Relative ([string]$record.relative)) -or
            ($record.kind -eq 'file' -and [string]$record.relative -match '(?i)\.pyc$')) {
            throw 'new stagingにPython cacheを持ち込めません。'
        }
    }
}

function Get-HostState {
    param([Parameter(Mandatory = $true)][string]$CurrentPath)
    $path = Get-NormalizedPath $CurrentPath
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) { return [ordered]@{ state = 'missing'; path = $path } }
    if (-not $item.PSIsContainer -or (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw 'updater/currentが通常のdirectoryではありません。'
    }
    foreach ($generation in @('known_old', 'known_old8', 'known_077', 'known_085', 'known_088', 'known_093', 'known_new')) {
        $definition = Get-KnownHostGeneration -State $generation
        try {
            $verified = Compare-CurrentInventory -Root $path -Expected $definition.map -ExpectedState $definition.state
            if ($null -ne $verified) { return $verified }
        }
        catch {}
    }
    throw 'updater/currentのhost全体が既知の7/8 file構成ではありません。'
}

function Assert-ApprovedSourceHost {
    param([Parameter(Mandatory = $true)][string]$SourceHostPath)
    $source = Get-NormalizedPath $SourceHostPath
    Assert-NoReparseAncestors -Path $source -Label 'source updater/current' | Out-Null
    $leaf = Split-Path -Leaf $source
    $parent = Split-Path -Parent $source
    if ($leaf -ine 'current' -or (Split-Path -Leaf $parent) -ine 'updater') {
        throw 'sourceはdistribution/updater/currentでなければなりません。'
    }
    foreach ($generation in @('known_old', 'known_old8', 'known_077', 'known_085', 'known_088', 'known_093', 'known_new')) {
        $definition = Get-KnownHostGeneration -State $generation
        try {
            $verified = Compare-CurrentInventory -Root $source -Expected $definition.map -ExpectedState $definition.state
            if ($null -ne $verified) {
                return [ordered]@{ path = $source; inventory = $verified }
            }
        }
        catch {}
    }
    throw 'source updater/currentが許可済みhost世代と一致しません。'
}

function Assert-NewSourceHost {
    # Compatibility entry point for callers that used the previous helper
    # name.  Source validation now accepts the approved old8 generation too,
    # so an interrupted old7→old8 receipt can be recovered safely.
    param([Parameter(Mandatory = $true)][string]$SourceHostPath)
    return Assert-ApprovedSourceHost -SourceHostPath $SourceHostPath
}

function Get-CanonicalStartState {
    param([Parameter(Mandatory = $true)][string]$Root)
    $start = Assert-RegularFile -Path (Join-Path $Root 'Start.bat') -Label '対象Start.bat'
    $sha = Get-SecureSha256 -Path $start
    $strictUtf8 = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList @($false, $true)
    try {
        $text = [IO.File]::ReadAllText($start, $strictUtf8)
    }
    catch {
        # Legacy batch files may be OEM/ANSI encoded.  Robocopy is ASCII, so
        # an ASCII fallback is sufficient for the no-host legacy decision;
        # canonical protected Start files are still authorized by fixed SHA.
        $text = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($start))
    }
    return [ordered]@{
        path = $start
        sha256 = $sha
        text = $text
        canonical = ($script:KnownCanonicalStartSha256 -contains $sha)
        legacy = ($text -match '(?i)\brobocopy\b')
    }
}

function Get-RepairRoot {
    param([Parameter(Mandatory = $true)][string]$TargetRoot)
    return (Join-Path $TargetRoot 'update_recovery\secure-update-host-repair')
}

function Get-RepairReceipts {
    param([Parameter(Mandatory = $true)][string]$TargetRoot)
    $repairRoot = Get-RepairRoot -TargetRoot $TargetRoot
    if (-not (Test-Path -LiteralPath $repairRoot)) { return @() }
    Assert-RegularDirectory -Path $repairRoot -Label 'repair workspace' | Out-Null
    $result = New-Object System.Collections.ArrayList
    foreach ($entry in @(Get-ChildItem -LiteralPath $repairRoot -Force -ErrorAction Stop)) {
        Assert-NoReparseAncestors -Path $entry.FullName -Label 'repair workspace entry' | Out-Null
        if (-not $entry.PSIsContainer -or $entry.Name -notmatch '^[0-9a-fA-F]{32}$') {
            throw 'repair workspaceに不正なentryがあります。'
        }
        $operation = $entry
        $receiptPath = Join-Path $operation.FullName 'receipt.json'
        $hasReceiptHistory = $false
        foreach ($operationEntry in @(Get-ChildItem -LiteralPath $operation.FullName -Force -ErrorAction Stop)) {
            Assert-NoReparseAncestors -Path $operationEntry.FullName -Label 'repair operation entry' | Out-Null
            if ($operationEntry.Name -eq 'receipt.json') { continue }
            if ($operationEntry.Name -match '^\.receipt-[0-9a-fA-F]{32}\.previous\.json$') {
                Assert-RegularFile -Path $operationEntry.FullName -Label 'receipt history' | Out-Null
                $hasReceiptHistory = $true
                continue
            }
            if ($operationEntry.Name -match '^\.receipt-[0-9a-fA-F]{32}\.tmp$') {
                Assert-RegularFile -Path $operationEntry.FullName -Label 'receipt temporary evidence' | Out-Null
                continue
            }
            if (-not $operationEntry.PSIsContainer -or
                $operationEntry.Name -notin @('backup-current', 'staging-current', 'parked-old')) {
                throw 'repair operationに不正なentryがあります。'
            }
        }
        if (-not (Test-Path -LiteralPath $receiptPath)) {
            # Copying backup/staging happens before the intent receipt is
            # atomically published.  Such an orphan cannot authorize a
            # rename, so it is preserved as evidence and a fresh operation
            # may be started while current remains the verified old tree.
            if ($hasReceiptHistory) {
                throw 'current receiptがないoperationにreceipt historyがあります。'
            }
            if (Test-Path -LiteralPath (Join-Path $operation.FullName 'parked-old')) {
                throw 'receiptのないoperationにparked-oldがあります。'
            }
            foreach ($evidenceName in @('backup-current', 'staging-current')) {
                $evidencePath = Join-Path $operation.FullName $evidenceName
                if (Test-Path -LiteralPath $evidencePath) {
                    Assert-RegularDirectory -Path $evidencePath -Label ('orphan {0}' -f $evidenceName) | Out-Null
                    Get-SecureInventory -Root $evidencePath | Out-Null
                }
            }
            continue
        }
        Assert-RegularFile -Path $receiptPath -Label 'repair receipt' | Out-Null
        try { $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { throw 'repair receiptを読み取れません。' }
        if ($null -eq $receipt -or [int]$receipt.schema_version -ne 1 -or
            [string]$receipt.operation_id -ine $operation.Name) {
            throw 'repair receiptのschemaが不正です。'
        }
        if ([string]$receipt.state -notin @('prepared', 'old_parked', 'new_promoted', 'restored_old', 'complete')) {
            throw 'repair receiptのstateが不正です。'
        }
        [void]$result.Add([ordered]@{ path = $receiptPath; operation = $operation.FullName; value = $receipt })
    }
    return @($result | ForEach-Object { $_ })
}

function Assert-ReceiptPaths {
    param(
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)]$Receipt
    )
    $operationId = [string]$Receipt.operation_id
    if ($operationId -notmatch '^[0-9a-fA-F]{32}$') {
        throw 'repair receiptのoperation idが不正です。'
    }
    foreach ($name in @('current_relative', 'backup_relative', 'staging_relative', 'parked_relative', 'receipt_relative')) {
        $value = [string]$Receipt.$name
        if ([string]::IsNullOrWhiteSpace($value)) { throw 'repair receiptに固定相対pathがありません。' }
        Resolve-TargetRelative -Root $TargetRoot -Relative $value -Label $name | Out-Null
    }
    if ([string]$Receipt.current_relative -ne 'updater\current') {
        throw 'repair receiptのcurrent pathが固定契約と一致しません。'
    }
    if ($Receipt.backup_relative -notmatch ('^update_recovery\\secure-update-host-repair\\' + [regex]::Escape($operationId) + '\\backup-current$') -or
        $Receipt.staging_relative -notmatch ('^update_recovery\\secure-update-host-repair\\' + [regex]::Escape($operationId) + '\\staging-current$') -or
        $Receipt.parked_relative -notmatch ('^update_recovery\\secure-update-host-repair\\' + [regex]::Escape($operationId) + '\\parked-old$') -or
        $Receipt.receipt_relative -notmatch ('^update_recovery\\secure-update-host-repair\\' + [regex]::Escape($operationId) + '\\receipt\.json$')) {
        throw 'repair receiptのpathが固定契約と一致しません。'
    }
    $oldDefinition = Get-ReceiptGenerationDefinition -Receipt $Receipt -Role old
    $newDefinition = Get-ReceiptGenerationDefinition -Receipt $Receipt -Role new
    if ($oldDefinition.rank -ge $newDefinition.rank) {
        throw 'repair receiptのhost世代の順序が不正です。'
    }
}

function Get-SecureReceiptIoPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    # 呼出元は通常表記のpathでancestor/reparse検査を済ませてから使用する。
    # File.ReplaceのbackupだけがMAX_PATHを越える場合にも、元receiptと
    # previousを同じvolumeへ原子的に保管する契約を維持する。
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\?\', [StringComparison]::Ordinal)) { return $full }
    if ($full.StartsWith('\\', [StringComparison]::Ordinal)) { return ('\\?\UNC\' + $full.Substring(2)) }
    return ('\\?\' + $full)
}

function Invoke-RepairReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Receipt
    )
    $parent = Split-Path -Parent $Path
    Assert-RegularDirectory -Path $parent -Label 'receipt parent' | Out-Null
    $json = $Receipt | ConvertTo-Json -Depth 12 -Compress
    $temporary = Join-Path $parent ('.receipt-{0}.tmp' -f ([guid]::NewGuid().ToString('N')))
    $encoding = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList @($false)
    $temporaryIo = Get-SecureReceiptIoPath $temporary
    [IO.File]::WriteAllText($temporaryIo, $json + "`n", $encoding)
    $stream = $null
    try {
        $stream = [IO.File]::Open($temporaryIo, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        $stream.Flush($true)
    }
    finally { if ($null -ne $stream) { $stream.Dispose() } }
    if (Test-Path -LiteralPath (Get-SecureReceiptIoPath $Path)) {
        # Windows PowerShell binds a $null third argument as an empty path on
        # some .NET overloads.  Keep a unique previous receipt as durable
        # evidence and always pass a real same-volume path to File.Replace.
        $previous = Join-Path $parent ('.receipt-{0}.previous.json' -f ([guid]::NewGuid().ToString('N')))
        [IO.File]::Replace($temporaryIo, (Get-SecureReceiptIoPath $Path), (Get-SecureReceiptIoPath $previous), $true)
    }
    else {
        [IO.File]::Move($temporaryIo, (Get-SecureReceiptIoPath $Path))
    }
}

function Invoke-SecureReceiptWrite {
    param(
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)]$Receipt
    )
    Assert-ReceiptPaths -TargetRoot $TargetRoot -Receipt $Receipt
    $receiptPath = Resolve-TargetRelative -Root $TargetRoot -Relative ([string]$Receipt.receipt_relative) -Label 'receipt'
    Invoke-RepairReceipt -Path $receiptPath -Receipt $Receipt
}

function Invoke-RepairMove {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    [IO.Directory]::Move($Source, $Destination)
}

function Invoke-SecureDirectoryMove {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    Assert-NoReparseAncestors -Path $Source -Label 'directory move source' | Out-Null
    Assert-NoReparseAncestors -Path $Destination -Label 'directory move destination' -AllowMissing
    if (Test-Path -LiteralPath $Destination) { throw 'directory move destination already exists。' }
    Invoke-RepairMove -Source $Source -Destination $Destination
}

function Copy-SecureTree {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [switch]$ExcludePythonCache
    )
    Assert-RegularDirectory -Path $Source -Label 'copy source' | Out-Null
    Assert-NoReparseAncestors -Path $Destination -Label 'copy destination' -AllowMissing
    $destinationIo = Get-SecureReceiptIoPath $Destination
    if ([IO.Directory]::Exists($destinationIo) -or [IO.File]::Exists($destinationIo)) { throw 'copy destination already exists。' }
    [IO.Directory]::CreateDirectory($destinationIo) | Out-Null
    $stack = New-Object System.Collections.Stack
    $stack.Push([ordered]@{ source = (Get-NormalizedPath $Source); destination = (Get-NormalizedPath $Destination) })
    while ($stack.Count -gt 0) {
        $pair = $stack.Pop()
        foreach ($item in @(Get-ChildItem -LiteralPath $pair.source -Force -ErrorAction Stop)) {
            Assert-NoReparseAncestors -Path $item.FullName -Label 'copy source item' | Out-Null
            if ($ExcludePythonCache) {
                if ($item.PSIsContainer -and $item.Name -ieq '__pycache__') {
                    Get-SecureInventory -Root $item.FullName | Out-Null
                    continue
                }
                if (-not $item.PSIsContainer -and $item.Name -match '(?i)\.pyc$') {
                    continue
                }
            }
            $destinationItem = Join-Path $pair.destination $item.Name
            if ($item.PSIsContainer) {
                [IO.Directory]::CreateDirectory((Get-SecureReceiptIoPath $destinationItem)) | Out-Null
                $stack.Push([ordered]@{ source = $item.FullName; destination = $destinationItem })
            }
            else {
                # ancestor/reparse検査は上で通常表記のpathへ実施済み。
                # 保全先だけMAX_PATHを越える場合も、除外せず全件コピーする。
                [IO.File]::Copy((Get-SecureReceiptIoPath $item.FullName), (Get-SecureReceiptIoPath $destinationItem), $false)
                if ((Get-SecureSha256 -Path $destinationItem) -ne (Get-SecureSha256 -Path $item.FullName)) {
                    throw 'copy後のhost file SHA256検証に失敗しました。'
                }
            }
        }
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
            throw 'lockのPID形式が不正です。'
        }
    }
    catch {
        throw 'app/nexus_ark.lockの形式を安全に確認できません。'
    }
    try {
        $details = @(Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId={0}' -f $pidValue) -ErrorAction Stop)
    }
    catch {
        $message = [string]$_.Exception.Message
        if ($message -match '(?i)(cannot find|no process|not found|process.*identifier)') { return }
        throw 'app/nexus_ark.lockのprocess状態を安全に確認できません。'
    }
    if ($details.Count -eq 0) { return }
    if ([string]::IsNullOrWhiteSpace([string]$details[0].CommandLine)) {
        throw 'app/nexus_ark.lockのprocessを確認できません。'
    }
    if ([string]$details[0].CommandLine -match '(?i)nexus_ark(\.py|\.exe)') {
        throw 'app/nexus_ark.lockが実行中のNexus Arkを示しています。'
    }
}

function Assert-TargetProcessesStopped {
    param([Parameter(Mandatory = $true)][string]$Root)
    try { $processes = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop) }
    catch { throw '対象rootのprocessを確認できません。' }
    $rootPrefix = (Get-NormalizedPath $Root).TrimEnd([char[]]@('\', '/'))
    $rootPattern = [regex]::Escape($rootPrefix) + '(?:[\\/"'']|$)'
    $targetPattern = '(?i)(?:nexus_ark\.py|nexus_ark\.exe|update_host\.supervisor|Start\.bat)'
    foreach ($process in $processes) {
        if ([int]$process.ProcessId -eq $PID) { continue }
        $commandLine = [string]$process.CommandLine
        $executablePath = [string]$process.ExecutablePath
        $name = [string]$process.Name
        $targetsRoot = ($commandLine -match $rootPattern) -or ($executablePath -match $rootPattern)
        $targetsHost = $commandLine -match $targetPattern
        if (($name -match '(?i)^nexus_ark(?:\.exe)?$') -and [string]::IsNullOrWhiteSpace($commandLine)) {
            throw 'Nexus Ark processの詳細を確認できません。'
        }
        if ($targetsRoot -and $targetsHost) { throw '対象rootのNexus Ark processが実行中です。' }
        if ($targetsHost -and [string]::IsNullOrWhiteSpace($executablePath) -and -not $targetsRoot) {
            throw 'Nexus Ark processの実行元を確認できません。'
        }
        if (($name -match '(?i)^cmd(?:\.exe)?$') -and ($commandLine -match '(?i)Start\.bat')) {
            throw 'Start.batを実行するcmd processが残っています。'
        }
    }
}

function Get-RolledBackUpdateJournal {
    param([Parameter(Mandatory = $true)][string]$Root)

    $recovery = Join-Path $Root 'update_recovery'
    $journal = Join-Path $recovery 'transaction.json'
    Assert-RegularDirectory -Path $recovery -Label 'update_recovery' | Out-Null
    Assert-RegularFile -Path $journal -Label '更新transaction' | Out-Null
    if ((Get-Item -LiteralPath $journal).Length -gt 65536) {
        throw '更新transactionが大きすぎるため停止しました。'
    }
    try { $state = Get-Content -LiteralPath $journal -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw '更新transactionを安全に確認できません。' }
    if ($null -eq $state -or [string]$state.schema_version -ne '1') {
        throw '更新transactionのschemaが安全な復旧形式ではありません。'
    }
    if ([string]$state.phase -cne 'rolled_back') {
        throw '更新transactionがrolled_backではありません。'
    }
    $activeStepProperties = @($state.PSObject.Properties | Where-Object { $_.Name -eq 'active_step' })
    if ($activeStepProperties.Count -gt 0 -and $null -ne $state.active_step) {
        throw '更新transactionに未確定のactive_stepがあります。'
    }
    $digestProperties = @($state.PSObject.Properties | Where-Object { $_.Name -eq 'manifest_digest' })
    $digest = if ($digestProperties.Count -gt 0) { [string]$state.manifest_digest } else { '' }
    if ($digest -notmatch '^[0-9a-fA-F]{64}$') {
        throw '更新transactionのmanifest digestが不正です。'
    }
    $versionProperties = @($state.PSObject.Properties | Where-Object { $_.Name -eq 'target_version' })
    $targetVersion = if ($versionProperties.Count -gt 0) { ([string]$state.target_version).Trim() } else { '' }
    if ($targetVersion -notmatch '^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$') {
        throw '更新transactionのtarget versionが不正です。'
    }
    foreach ($name in @('retry_pending', 'retry-pending')) {
        $property = @($state.PSObject.Properties | Where-Object { $_.Name -eq $name })
        if ($property.Count -gt 0 -and $null -ne $state.$name -and
            [string]$state.$name -notin @('', '0', 'false', 'False')) {
            throw '更新transactionに再試行保留状態があります。'
        }
    }
    $retryGuard = Join-Path $recovery 'retry-pending.json'
    if (Test-Path -LiteralPath $retryGuard) {
        Assert-NoReparseAncestors -Path $retryGuard -Label 'retry-pending guard' | Out-Null
        throw '再試行保留guardが残っています。'
    }
    return [ordered]@{
        state = $state
        target_version = $targetVersion
        manifest_digest = $digest
    }
}

function Assert-NoReparseTree {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $rootPath = Get-NormalizedPath $Path
    $pending = New-Object System.Collections.Stack
    $pending.Push($rootPath)
    while ($pending.Count -gt 0) {
        $directory = [string]$pending.Pop()
        foreach ($item in @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw ('{0} にリンクまたは再解析ポイントが含まれます。' -f $Label)
            }
            if ($item.PSIsContainer) { $pending.Push((Get-NormalizedPath $item.FullName)) }
        }
    }
}

function Assert-RolledBackNextTree {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)]$Journal
    )
    $allowedRootDirectories = @('app.next', '.venv.next', 'runtime.next')
    $allowedRootFiles = @('pyproject.toml.next', 'uv.lock.next')
    foreach ($relative in $allowedRootDirectories) {
        $candidate = Join-Path $Root $relative
        if (Test-Path -LiteralPath $candidate) {
            Assert-RegularDirectory -Path $candidate -Label $relative | Out-Null
            Assert-NoReparseTree -Path $candidate -Label $relative
        }
    }
    foreach ($relative in $allowedRootFiles) {
        $candidate = Join-Path $Root $relative
        if (Test-Path -LiteralPath $candidate) {
            Assert-RegularFile -Path $candidate -Label $relative | Out-Null
        }
    }
    $staging = Join-Path $Root 'update_staging'
    if (-not (Test-Path -LiteralPath $staging)) { return }
    Assert-RegularDirectory -Path $staging -Label 'update_staging' | Out-Null
    $children = @(Get-ChildItem -LiteralPath $staging -Force -ErrorAction Stop)
    foreach ($child in $children) {
        if ($child.Name -notin @('app', 'runtime')) {
            throw 'update_stagingに未知のentryがあります。'
        }
        Assert-RegularDirectory -Path $child.FullName -Label ('update_staging/{0}' -f $child.Name) | Out-Null
        Assert-NoReparseTree -Path $child.FullName -Label ('update_staging/{0}' -f $child.Name)
    }
    $stagingApp = Join-Path $staging 'app'
    if (-not (Test-Path -LiteralPath $stagingApp)) {
        if ($children.Count -gt 0) { throw 'update_stagingにappがありません。' }
        return
    }
    $manifestPath = Join-Path $stagingApp 'release_manifest.json'
    Assert-RegularFile -Path $manifestPath -Label 'update_staging/app/release_manifest.json' | Out-Null
    try { $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw 'update_stagingのrelease manifestを確認できません。' }
    if ($null -eq $manifest -or [string]$manifest.release_version -cne [string]$Journal.target_version) {
        throw 'update_stagingのrelease versionがtransactionと一致しません。'
    }
}

function Assert-NoPendingArtifacts {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [switch]$AllowRolledBackNext
    )
    Assert-NexusLauncherMaintenance -Root $Root -KnownStartHashes $script:KnownCanonicalStartSha256
    $rolledBackJournal = $null
    if ($AllowRolledBackNext) {
        $rolledBackJournal = Get-RolledBackUpdateJournal -Root $Root
        Assert-RolledBackNextTree -Root $Root -Journal $rolledBackJournal
    }
    $allowedRootNext = @('app.next', '.venv.next', 'runtime.next', 'pyproject.toml.next', 'uv.lock.next')
    foreach ($relative in @(
        'update_recovery\transaction.lock',
        'update_recovery\bridge.lock',
        'updater.next',
        'app.next',
        '.venv.next',
        'Start.bat.lock'
    )) {
        $candidate = Join-Path $Root $relative
        if (Test-Path -LiteralPath $candidate) {
            Assert-NoReparseAncestors -Path $candidate -Label $relative | Out-Null
            if ($AllowRolledBackNext -and $allowedRootNext -contains $relative) {
                if ($relative -in @('app.next', '.venv.next', 'runtime.next')) {
                    Assert-RegularDirectory -Path $candidate -Label $relative | Out-Null
                    Assert-NoReparseTree -Path $candidate -Label $relative
                }
                else {
                    Assert-RegularFile -Path $candidate -Label $relative | Out-Null
                }
                continue
            }
            throw '未解決の更新lockまたはnext世代があります。'
        }
    }
    # An initialized updater may leave an empty staging directory, including
    # after a successful legacy migration. It contains no pending generation.
    $staging = Join-Path $Root 'update_staging'
    if (Test-Path -LiteralPath $staging) {
        Assert-RegularDirectory -Path $staging -Label 'update_staging' | Out-Null
        if (@(Get-ChildItem -LiteralPath $staging -Force -ErrorAction Stop).Count -gt 0 -and -not $AllowRolledBackNext) {
            throw '未解決の更新stagingがあります。'
        }
    }
    foreach ($candidate in @(Get-ChildItem -LiteralPath $Root -Force -ErrorAction Stop | Where-Object { $_.Name -match '(?i)\.tmp$' })) {
        if (Test-NexusLauncherTemporaryName $candidate.Name) { continue }
        Assert-NoReparseAncestors -Path $candidate.FullName -Label 'root temporary update entry' | Out-Null
        throw '対象root直下に一時更新fileがあります。'
    }
    foreach ($directoryRelative in @('.', 'app', 'runtime', 'updater', 'metadata')) {
        $directory = if ($directoryRelative -eq '.') { $Root } else { Join-Path $Root $directoryRelative }
        if (-not (Test-Path -LiteralPath $directory)) { continue }
        Assert-RegularDirectory -Path $directory -Label ('pending scan {0}' -f $directoryRelative) | Out-Null
        foreach ($candidate in @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop | Where-Object { $_.Name -match '(?i)\.next$' })) {
            Assert-NoReparseAncestors -Path $candidate.FullName -Label 'pending .next entry' | Out-Null
            if ($AllowRolledBackNext -and $directoryRelative -eq '.' -and $allowedRootNext -contains $candidate.Name) {
                continue
            }
            throw '未解決の.next世代があります。'
        }
    }
    $recovery = Join-Path $Root 'update_recovery'
    if (Test-Path -LiteralPath $recovery) {
        Assert-RegularDirectory -Path $recovery -Label 'update_recovery' | Out-Null
        foreach ($candidate in @(Get-ChildItem -LiteralPath $recovery -Force -ErrorAction Stop | Where-Object { $_.Name -match '(?i)\.tmp$' })) {
            Assert-NoReparseAncestors -Path $candidate.FullName -Label 'recovery temporary update entry' | Out-Null
            throw 'update_recoveryに一時更新fileがあります。'
        }
        foreach ($candidate in @(Get-ChildItem -LiteralPath $recovery -Force -ErrorAction Stop | Where-Object { $_.Name -match '(?i)\.next$' })) {
            Assert-NoReparseAncestors -Path $candidate.FullName -Label 'recovery pending .next entry' | Out-Null
            throw 'update_recoveryに未解決の.next世代があります。'
        }
        $journal = Join-Path $recovery 'transaction.json'
        if (Test-Path -LiteralPath $journal) {
            Assert-RegularFile -Path $journal -Label '更新transaction' | Out-Null
            try { $state = Get-Content -LiteralPath $journal -Raw -Encoding UTF8 | ConvertFrom-Json }
            catch { throw '更新transactionを確認できません。' }
            if ([string]$state.phase -notin @('committed', 'rolled_back')) { throw '未完了の更新transactionがあります。' }
        }
    }
}

function Open-SecureRepairLock {
    param([Parameter(Mandatory = $true)][string]$Root)
    $recovery = Join-Path $Root 'update_recovery'
    if (-not (Test-Path -LiteralPath $recovery)) { New-Item -ItemType Directory -Path $recovery -Force | Out-Null }
    Assert-RegularDirectory -Path $recovery -Label 'update_recovery' | Out-Null
    $path = Join-Path $recovery 'secure-update-host-repair.lock'
    # This lock is a lease, not a durable pending marker.  A process crash can
    # leave the zero-length file behind; OpenOrCreate + FileShare.None makes a
    # live holder fail closed while allowing a later process to reclaim a
    # stale file without deleting it first (which would introduce a race).
    Assert-NoReparseAncestors -Path $path -Label 'secure repair lock' -AllowMissing
    try {
        return [IO.File]::Open($path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    }
    catch { throw '同じ更新host修復、または未解決の修復lockがあります。' }
}

function Assert-SecureRepairLeaseAvailable {
    param([Parameter(Mandatory = $true)][string]$Root)
    $recovery = Join-Path $Root 'update_recovery'
    $path = Join-Path $recovery 'secure-update-host-repair.lock'
    if (-not (Test-Path -LiteralPath $path)) { return }
    Assert-NoReparseAncestors -Path $path -Label 'secure repair lock' | Out-Null
    $lease = $null
    try {
        # InspectOnly must not create or delete the persistent lease marker.
        # Opening an existing file exclusively distinguishes a stale marker
        # from a live helper without a delete/recreate race.
        $lease = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    }
    catch { throw '同じ更新host修復が実行中です。' }
    finally { if ($null -ne $lease) { $lease.Dispose() } }
}

function New-SecureRepairResult {
    param(
        [Parameter(Mandatory = $true)][string]$State,
        [Parameter(Mandatory = $true)][bool]$Changed,
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)][string]$SourceHostPath,
        [string]$ReceiptRelative = '',
        [string]$BackupRelative = '',
        [string]$OldSha256 = '',
        [string]$NewSha256 = '',
        [string]$RecoveryAction = '',
        [string]$Message = ''
    )
    return [ordered]@{
        state = $State
        changed = $Changed
        target_root = $TargetRoot
        source_host = $SourceHostPath
        receipt_path = $ReceiptRelative
        backup_path = $BackupRelative
        old_sha256 = $OldSha256
        new_sha256 = $NewSha256
        recovery_action = $RecoveryAction
        message = $Message
    }
}

function Get-TargetAssessment {
    param([Parameter(Mandatory = $true)][string]$TargetRoot)
    $root = Assert-RegularDirectory -Path $TargetRoot -Label '対象root'
    $start = Get-CanonicalStartState -Root $root
    $current = Join-Path $root 'updater\current'
    $hostItem = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
    if ($null -eq $hostItem) {
        if ($start.legacy) {
            return [ordered]@{ state = 'legacy'; root = $root; start = $start; current = $current }
        }
        if (-not $start.canonical) { throw 'hostがないrootのStart.batがcanonicalではありません。' }
        return [ordered]@{ state = 'missing'; root = $root; start = $start; current = $current }
    }
    if (-not $start.canonical) { throw '対象Start.batがcanonical SHAと一致しません。' }
    try {
        $hostAssessment = Get-HostState -CurrentPath $current
    }
    catch {
        # Only the observed 0.89 seven-file subset is eligible for an
        # independent rebuild. It is never a runnable/repairable generation.
        if ($start.sha256 -notin @(
            '3bafb0efb475fdd9bd9e686739a461018c1612dc092a356b9bf8a9ed2538684a',
            '184427c65309ebc1f45b2d2a2c5f71ee2ec1e70cb40c458a35449c1b06705241'
        )) { throw }
        $incomplete089 = [ordered]@{
            '__init__.py' = 'e58a28a84176ff2ef4909f29709eb0efe75455d59abf1368f64c829caef24ad5'
            'contracts.py' = '958c0c669f716b4eeb04083a1ba39eba913d90bfd7baae7b9c9a15bad848af20'
            'runtime.py' = '3f446f01094438b75974a60215e5d670f90175d83bc2f62a0fb9aff45781245b'
            'runtime_archive.py' = '15b6fd5835efebcb4a02210c7f7ebeffd52a86d4776dfeca109c9e068fb7dbcc'
            'supervisor.py' = '7bbdc5e33f87aca6c8b76e64393825fe24612e50443c178406944588dff4be30'
            'transaction.py' = '946a100fd0f5b61746f725f194e6c214684db9378e26df6ef406b6d1c35d48d9'
            'trial.py' = 'a0bc50e42e462bf35dac908477b7863ac4a6031e8dcbb8a054f3bd3d4a80f326'
        }
        $hostAssessment = Compare-CurrentInventory -Root $current -Expected $incomplete089 -ExpectedState 'incomplete_089'
    }
    return [ordered]@{ state = [string]$hostAssessment.state; root = $root; start = $start; current = $current; inventory = $hostAssessment }
}

function Get-PendingRepair {
    param([Parameter(Mandatory = $true)][string]$TargetRoot)
    $receipts = @(Get-RepairReceipts -TargetRoot $TargetRoot)
    $pending = @($receipts | Where-Object { [string]$_.value.state -ne 'complete' })
    $completed = @($receipts | Where-Object { [string]$_.value.state -eq 'complete' })
    if ($pending.Count -gt 1) { throw '未完了の修復receiptが複数あります。' }
    foreach ($complete in $completed) {
        Assert-ReceiptPaths -TargetRoot $TargetRoot -Receipt $complete.value
        Assert-ReceiptTree -TargetRoot $TargetRoot -Receipt $complete.value -Kind backup | Out-Null
        $completedStaging = Get-ReceiptPathValue -TargetRoot $TargetRoot -Receipt $complete.value -Name 'staging_relative'
        Assert-NoReparseAncestors -Path $completedStaging -Label 'completed staging' -AllowMissing
        if (Test-Path -LiteralPath $completedStaging) {
            throw '完了済みの修復にstaging世代が残っています。'
        }
        Assert-ReceiptTree -TargetRoot $TargetRoot -Receipt $complete.value -Kind parked | Out-Null
    }
    $completedStates = @{}
    foreach ($complete in $completed) {
        $newState = (Get-ReceiptGenerationDefinition -Receipt $complete.value -Role new).state
        if ($completedStates.ContainsKey($newState)) {
            throw '同じhost世代の完了済み修復receiptが複数あります。'
        }
        $completedStates[$newState] = $true
    }
    if ($pending.Count -eq 1) {
        $receipt = $pending[0]
        Assert-ReceiptPaths -TargetRoot $TargetRoot -Receipt $receipt.value
        Assert-ReceiptTree -TargetRoot $TargetRoot -Receipt $receipt.value -Kind backup | Out-Null
        foreach ($kind in @('staging', 'parked')) {
            $candidate = Get-ReceiptPathValue -TargetRoot $TargetRoot -Receipt $receipt.value -Name ('{0}_relative' -f $kind)
            if (Test-Path -LiteralPath $candidate) {
                Assert-ReceiptTree -TargetRoot $TargetRoot -Receipt $receipt.value -Kind $kind | Out-Null
            }
        }
        $pendingNewState = (Get-ReceiptGenerationDefinition -Receipt $receipt.value -Role new).state
        if (@($completed | Where-Object {
                    (Get-ReceiptGenerationDefinition -Receipt $_.value -Role new).state -eq $pendingNewState
                }).Count -gt 0) {
            throw '同じhost世代の完了済み修復receiptと未完了receiptが混在しています。'
        }
        # 完了履歴はdisk上で検証・保持済み。絶対pathを含む取得wrapperを
        # pending receiptへ付加せず、後続操作ではdiskから再列挙する。
        return $receipt
    }
    if ($receipts.Count -gt 0) {
        $orderedCompleted = @($completed | Sort-Object -Property @{ Expression = {
                    (Get-ReceiptGenerationDefinition -Receipt $_.value -Role new).rank
                } })
        return [ordered]@{ complete = $true; receipt = $orderedCompleted[$orderedCompleted.Count - 1]; receipts = @($orderedCompleted) }
    }
    return $null
}

function New-SecureReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)][string]$OperationId,
        [Parameter(Mandatory = $true)]$OldInventory,
        [Parameter(Mandatory = $true)]$NewInventory
    )
    $operationRelative = Join-Path 'update_recovery\secure-update-host-repair' $OperationId
    return [ordered]@{
        schema_version = 1
        operation_id = $OperationId
        state = 'prepared'
        target_root_relative = '.'
        current_relative = 'updater\current'
        backup_relative = Join-Path $operationRelative 'backup-current'
        staging_relative = Join-Path $operationRelative 'staging-current'
        parked_relative = Join-Path $operationRelative 'parked-old'
        receipt_relative = Join-Path $operationRelative 'receipt.json'
        old_state = [string]$OldInventory.state
        new_state = [string]$NewInventory.state
        old_files = $OldInventory.files
        new_files = $NewInventory.files
        ignored_pycache = [int]$OldInventory.ignored_pycache + [int]$NewInventory.ignored_pycache
    }
}

function Get-ReceiptPathValue {
    param(
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)]$Receipt,
        [Parameter(Mandatory = $true)][ValidateSet('backup_relative', 'staging_relative', 'parked_relative', 'receipt_relative', 'current_relative')][string]$Name
    )
    return Resolve-TargetRelative -Root $TargetRoot -Relative ([string]$Receipt.$Name) -Label $Name
}

function Assert-ReceiptFilesExact {
    param(
        [Parameter(Mandatory = $true)]$Files,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ($null -eq $Files) { throw ('{0}がありません。' -f $Label) }
    $entries = New-Object System.Collections.ArrayList
    if ($Files -is [System.Collections.IDictionary]) {
        foreach ($key in $Files.Keys) {
            [void]$entries.Add([ordered]@{ key = [string]$key; value = [string]$Files[$key] })
        }
    }
    else {
        foreach ($property in @($Files.PSObject.Properties)) {
            [void]$entries.Add([ordered]@{ key = [string]$property.Name; value = [string]$property.Value })
        }
    }
    if ($entries.Count -ne $Expected.Count) { throw ('{0}が承認済み完全mapと一致しません。' -f $Label) }
    $seen = @{}
    foreach ($entry in $entries) {
        $key = [string]$entry.key
        $lower = $key.ToLowerInvariant()
        if ($seen.ContainsKey($lower) -or -not (@($Expected.Keys) -ccontains $key)) {
            throw ('{0}が承認済み完全mapと一致しません。' -f $Label)
        }
        $seen[$lower] = $true
        if ([string]$entry.value -cne [string]$Expected[$key]) {
            throw ('{0}が承認済み完全mapと一致しません。' -f $Label)
        }
    }
}

function Get-ReceiptGenerationDefinition {
    param(
        [Parameter(Mandatory = $true)]$Receipt,
        [Parameter(Mandatory = $true)][ValidateSet('old', 'new')][string]$Role
    )
    $stateName = if ($Role -eq 'old') { [string]$Receipt.old_state } else { [string]$Receipt.new_state }
    $files = if ($Role -eq 'old') { $Receipt.old_files } else { $Receipt.new_files }
    foreach ($candidateName in @('known_new', 'known_093', 'known_old', 'known_old8', 'known_077', 'known_085', 'known_088')) {
        $candidate = Get-KnownHostGeneration -State $candidateName
        try { Assert-ReceiptFilesExact -Files $files -Expected $candidate.map -Label ('receipt {0}_files' -f $Role) }
        catch { continue }
        if ($stateName -eq $candidate.state) { return $candidate }
        # Receipts emitted by previous helpers called a historical generation
        # ``known_new``.  The complete map is the compatibility proof; no
        # state-only or partial-map fallback is accepted.
        if ($Role -eq 'new' -and $stateName -eq 'known_new' -and
            $candidate.state -in @('known_093', 'known_old8', 'known_077', 'known_085', 'known_088')) {
            return [ordered]@{ state = $candidate.state; rank = $candidate.rank; map = $candidate.map; legacy_label = $true }
        }
        throw ('receiptの{0}_stateと{0}_filesが一致しません。' -f $Role)
    }
    throw ('receiptの{0}_filesが承認済み完全mapと一致しません。' -f $Role)
}

function Assert-ReceiptTree {
    param(
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)]$Receipt,
        [Parameter(Mandatory = $true)][ValidateSet('backup', 'staging', 'parked')][string]$Kind
    )
    $relativeName = '{0}_relative' -f $Kind
    $path = Get-ReceiptPathValue -TargetRoot $TargetRoot -Receipt $Receipt -Name $relativeName
    Assert-RegularDirectory -Path $path -Label ('receipt {0}' -f $Kind) | Out-Null
    $oldDefinition = Get-ReceiptGenerationDefinition -Receipt $Receipt -Role old
    $newDefinition = Get-ReceiptGenerationDefinition -Receipt $Receipt -Role new
    if ($oldDefinition.rank -ge $newDefinition.rank) {
        throw 'repair receiptのhost世代の順序が不正です。'
    }
    $definition = if ($Kind -eq 'backup' -or $Kind -eq 'parked') { $oldDefinition } else { $newDefinition }
    $expected = $definition.map
    $expectedState = $definition.state
    return Compare-CurrentInventory -Root $path -Expected $expected -ExpectedState $expectedState
}

function Invoke-RepairRecovery {
    param(
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)][string]$SourceHostPath,
        [Parameter(Mandatory = $true)]$Pending
    )
    $receipt = $Pending.value
    $root = Get-NormalizedPath $TargetRoot
    $oldDefinition = Get-ReceiptGenerationDefinition -Receipt $receipt -Role old
    $newDefinition = Get-ReceiptGenerationDefinition -Receipt $receipt -Role new
    $oldMap = $oldDefinition.map
    $newMap = $newDefinition.map
    $current = Get-ReceiptPathValue -TargetRoot $root -Receipt $receipt -Name 'current_relative'
    $backup = Get-ReceiptPathValue -TargetRoot $root -Receipt $receipt -Name 'backup_relative'
    $staging = Get-ReceiptPathValue -TargetRoot $root -Receipt $receipt -Name 'staging_relative'
    $parked = Get-ReceiptPathValue -TargetRoot $root -Receipt $receipt -Name 'parked_relative'
    $currentState = Get-HostState -CurrentPath $current
    $stagingExists = Test-Path -LiteralPath $staging
    $parkedExists = Test-Path -LiteralPath $parked
    if ($stagingExists) {
        Assert-ReceiptTree -TargetRoot $root -Receipt $receipt -Kind staging | Out-Null
        Assert-CacheFreeCurrent -Root $staging
    }
    if ($parkedExists) { Assert-ReceiptTree -TargetRoot $root -Receipt $receipt -Kind parked | Out-Null }
    Assert-ReceiptTree -TargetRoot $root -Receipt $receipt -Kind backup | Out-Null

    if ($currentState.state -eq $newDefinition.state -and $parkedExists) {
        if ($stagingExists) { throw '新版currentとstagingが重複しているため停止します。' }
        Assert-CacheFreeCurrent -Root $current
        $receipt.new_state = $newDefinition.state
        $receipt.state = 'new_promoted'
        Invoke-SecureReceiptWrite -TargetRoot $root -Receipt $receipt
        $receipt.state = 'complete'
        Invoke-SecureReceiptWrite -TargetRoot $root -Receipt $receipt
        return New-SecureRepairResult -State $newDefinition.state -Changed $true -TargetRoot $root -SourceHostPath $SourceHostPath -ReceiptRelative $receipt.receipt_relative -BackupRelative $receipt.backup_relative -OldSha256 $oldMap['supervisor.py'] -NewSha256 $newMap['supervisor.py'] -RecoveryAction 'verified_new_finalized' -Message '検証済み新版hostを確定しました。'
    }
    if ($currentState.state -eq $oldDefinition.state -and -not $parkedExists) {
        if ($stagingExists) {
            Assert-AppLockClear -Root $root
            Assert-TargetProcessesStopped -Root $root
            Invoke-SecureDirectoryMove -Source $current -Destination $parked
            Assert-ReceiptTree -TargetRoot $root -Receipt $receipt -Kind parked | Out-Null
            $parkedExists = $true
            $receipt.state = 'old_parked'
            Invoke-SecureReceiptWrite -TargetRoot $root -Receipt $receipt
        }
        else {
            return New-SecureRepairResult -State 'repair_pending' -Changed $false -TargetRoot $root -SourceHostPath $SourceHostPath -ReceiptRelative $receipt.receipt_relative -BackupRelative $receipt.backup_relative -RecoveryAction 'verified_old_waiting_for_staging' -Message '検証済み旧hostを保持し、新host stagingを待機しています。'
        }
    }
    $currentItem = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
    if ($null -eq $currentItem -and $parkedExists -and $stagingExists) {
        Assert-AppLockClear -Root $root
        Assert-TargetProcessesStopped -Root $root
        Invoke-SecureDirectoryMove -Source $staging -Destination $current
        Compare-CurrentInventory -Root $current -Expected $newMap -ExpectedState $newDefinition.state | Out-Null
        Assert-CacheFreeCurrent -Root $current
        $receipt.state = 'new_promoted'
        Invoke-SecureReceiptWrite -TargetRoot $root -Receipt $receipt
        $receipt.state = 'complete'
        Invoke-SecureReceiptWrite -TargetRoot $root -Receipt $receipt
        $receipt.new_state = $newDefinition.state
        return New-SecureRepairResult -State $newDefinition.state -Changed $true -TargetRoot $root -SourceHostPath $SourceHostPath -ReceiptRelative $receipt.receipt_relative -BackupRelative $receipt.backup_relative -OldSha256 $oldMap['supervisor.py'] -NewSha256 $newMap['supervisor.py'] -RecoveryAction 'verified_old_parked_then_new_promoted' -Message '中断後に検証済み新版hostを確定しました。'
    }
    if ($null -eq $currentItem -and $parkedExists -and -not $stagingExists) {
        Assert-AppLockClear -Root $root
        Assert-TargetProcessesStopped -Root $root
        Copy-SecureTree -Source $backup -Destination $current
        Compare-CurrentInventory -Root $current -Expected $oldMap -ExpectedState $oldDefinition.state | Out-Null
        $receipt.state = 'restored_old'
        Invoke-SecureReceiptWrite -TargetRoot $root -Receipt $receipt
        return New-SecureRepairResult -State 'repair_pending' -Changed $true -TargetRoot $root -SourceHostPath $SourceHostPath -ReceiptRelative $receipt.receipt_relative -BackupRelative $receipt.backup_relative -OldSha256 $oldMap['supervisor.py'] -RecoveryAction 'verified_old_restored' -Message '新版を確定できず、検証済み旧hostへ戻しました。'
    }
    if ($currentState.state -eq $oldDefinition.state -and $parkedExists -and -not $stagingExists -and
        [string]$receipt.state -eq 'restored_old') {
        return New-SecureRepairResult -State 'repair_pending' -Changed $false -TargetRoot $root -SourceHostPath $SourceHostPath -ReceiptRelative $receipt.receipt_relative -BackupRelative $receipt.backup_relative -OldSha256 $oldMap['supervisor.py'] -RecoveryAction 'verified_old_restored' -Message '検証済み旧hostを保持しています。新版hostは確定していません。'
    }
    if ($null -eq $currentItem -and -not $parkedExists) {
        # A crash or external interruption before current→parked must never
        # cause a guessed promotion.  Restore the verified old backup and
        # leave a resumable receipt; staging, when present, remains evidence.
        Assert-AppLockClear -Root $root
        Assert-TargetProcessesStopped -Root $root
        Copy-SecureTree -Source $backup -Destination $current
        Compare-CurrentInventory -Root $current -Expected $oldMap -ExpectedState $oldDefinition.state | Out-Null
        $receipt.state = 'restored_old'
        Invoke-SecureReceiptWrite -TargetRoot $root -Receipt $receipt
        return New-SecureRepairResult -State 'repair_pending' -Changed $true -TargetRoot $root -SourceHostPath $SourceHostPath -ReceiptRelative $receipt.receipt_relative -BackupRelative $receipt.backup_relative -OldSha256 $oldMap['supervisor.py'] -RecoveryAction 'verified_old_restored' -Message 'currentが失われたため、検証済み旧hostを復旧しました。'
    }
    throw '修復receiptとhost世代の組合せが未知状態です。上書きせず停止しました。'
}

function Invoke-SecureUpdateHostRepair {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)][string]$SourceHostPath,
        [switch]$InspectOnly,
        [switch]$AllowRolledBackNext
    )
    Set-StrictMode -Version 2.0
    $ErrorActionPreference = 'Stop'
    $root = Assert-RegularDirectory -Path $TargetRoot -Label '対象root'
    $sourceCandidate = Get-NormalizedPath $SourceHostPath
    if (Test-SameOrNestedPath -Left $root -Right $sourceCandidate) {
        throw 'sourceとtargetの同一または入れ子pathは受け付けません。'
    }
    $assessment = Get-TargetAssessment -TargetRoot $root
    if ($assessment.state -eq 'legacy') {
        return New-SecureRepairResult -State 'legacy' -Changed $false -TargetRoot $root -SourceHostPath $SourceHostPath -Message 'legacy robocopy Start.batは既存移行経路へ委譲します。'
    }
    $pending = Get-PendingRepair -TargetRoot $root
    $hasCompleteReceipt = ($null -ne $pending -and @($pending.Keys) -contains 'complete')
    if ($assessment.state -eq 'incomplete_089') {
        # A known subset authorizes inspection and rebuilding beside the old
        # root only. No partial host is copied, executed, or promoted here.
        if (-not $InspectOnly -or $null -ne $pending) {
            throw '欠落した0.89 hostは直接修復せず、独立した初回更新で再構築してください。'
        }
        $completeSource = Assert-ApprovedSourceHost -SourceHostPath $SourceHostPath
        if ($completeSource.inventory.state -ne 'known_new') { throw '欠落hostの再構築には最新の完全な配布hostが必要です。' }
        Assert-NoPendingArtifacts -Root $root -AllowRolledBackNext:$AllowRolledBackNext
        Assert-AppLockClear -Root $root
        Assert-TargetProcessesStopped -Root $root
        Assert-SecureRepairLeaseAvailable -Root $root
        return New-SecureRepairResult -State 'incomplete_089' -Changed $false -TargetRoot $root -SourceHostPath $SourceHostPath -Message '既知0.89 hostの欠落を確認しました。旧hostを起動せず、独立した初回更新で再構築します。'
    }
    if ($assessment.state -eq 'missing' -and ($null -eq $pending -or $hasCompleteReceipt)) {
        throw 'canonical hostがないrootに、復旧可能な未完了receiptがありません。'
    }
    $source = Assert-ApprovedSourceHost -SourceHostPath $SourceHostPath
    $sourceDefinition = Get-KnownHostGeneration -State ([string]$source.inventory.state)
    $assessmentDefinition = if ($assessment.state -in @('known_old', 'known_old8', 'known_077', 'known_085', 'known_088', 'known_093', 'known_new')) {
        Get-KnownHostGeneration -State ([string]$assessment.state)
    }
    else { $null }
    if ($InspectOnly) {
        Assert-NoPendingArtifacts -Root $root -AllowRolledBackNext:$AllowRolledBackNext
        Assert-AppLockClear -Root $root
        Assert-TargetProcessesStopped -Root $root
        Assert-SecureRepairLeaseAvailable -Root $root
    }
    if ($hasCompleteReceipt) {
        $completeReceipt = $pending.receipt.value
        $completeDefinition = Get-ReceiptGenerationDefinition -Receipt $completeReceipt -Role new
        if ($assessment.state -ne $completeDefinition.state) { throw 'complete receiptと現在hostが一致しません。' }
        if ($sourceDefinition.rank -le $completeDefinition.rank) {
            if ($sourceDefinition.rank -lt $completeDefinition.rank) {
                throw 'sourceが現在hostより古い世代です。'
            }
            if ($InspectOnly) {
                return New-SecureRepairResult -State ([string]$assessment.state) -Changed $false -TargetRoot $root -SourceHostPath $source.path -ReceiptRelative $completeReceipt.receipt_relative -BackupRelative $completeReceipt.backup_relative -OldSha256 $completeDefinition.map['supervisor.py'] -NewSha256 $completeDefinition.map['supervisor.py'] -Message '既知のhost世代です。'
            }
            Assert-NoPendingArtifacts -Root $root -AllowRolledBackNext:$AllowRolledBackNext
            Assert-AppLockClear -Root $root
            Assert-TargetProcessesStopped -Root $root
            Assert-SecureRepairLeaseAvailable -Root $root
            return New-SecureRepairResult -State ([string]$assessment.state) -Changed $false -TargetRoot $root -SourceHostPath $source.path -ReceiptRelative $completeReceipt.receipt_relative -BackupRelative $completeReceipt.backup_relative -OldSha256 $completeDefinition.map['supervisor.py'] -NewSha256 $completeDefinition.map['supervisor.py'] -Message '既知のhost世代です。変更しません。'
        }
    }
    if ($null -ne $pending -and -not $hasCompleteReceipt) {
        $pendingDefinition = Get-ReceiptGenerationDefinition -Receipt $pending.value -Role new
        if ($sourceDefinition.rank -lt $pendingDefinition.rank) {
            throw 'sourceが未完了receiptの対象世代より古い世代です。'
        }
        if ($InspectOnly) {
            return New-SecureRepairResult -State 'repair_pending' -Changed $false -TargetRoot $root -SourceHostPath $source.path -ReceiptRelative $pending.value.receipt_relative -BackupRelative $pending.value.backup_relative -RecoveryAction ([string]$pending.value.state) -Message '未完了の修復receiptがあります。'
        }
        Assert-NoPendingArtifacts -Root $root -AllowRolledBackNext:$AllowRolledBackNext
        Assert-AppLockClear -Root $root
        Assert-TargetProcessesStopped -Root $root
        $lock = $null
        $recoveryResult = $null
        try {
            $lock = Open-SecureRepairLock -Root $root
            Assert-NoPendingArtifacts -Root $root -AllowRolledBackNext:$AllowRolledBackNext
            $recoveryResult = Invoke-RepairRecovery -TargetRoot $root -SourceHostPath $source.path -Pending $pending
        }
        finally {
            if ($null -ne $lock) {
                $lock.Dispose()
            }
        }
        # A package may contain the latest new8 source while an interrupted
        # legacy receipt only proves old8.  Complete that recovery first,
        # release the old lease, then start a fresh old8→new8 operation.  The
        # two receipts remain separate and each tree is verified before the
        # next generation is touched.
        if ($sourceDefinition.rank -gt $pendingDefinition.rank -and
            [string]$recoveryResult.state -eq $pendingDefinition.state) {
            return Invoke-SecureUpdateHostRepair -TargetRoot $root -SourceHostPath $source.path -AllowRolledBackNext:$AllowRolledBackNext
        }
        return $recoveryResult
    }
    if ($assessment.state -in @('known_old', 'known_old8', 'known_077', 'known_085', 'known_088', 'known_093', 'known_new')) {
        if ($sourceDefinition.rank -lt $assessmentDefinition.rank) {
            throw 'sourceが現在hostより古い世代です。'
        }
        if ($sourceDefinition.rank -eq $assessmentDefinition.rank) {
            $sameMap = $assessmentDefinition.map
            if ($InspectOnly) {
                return New-SecureRepairResult -State ([string]$assessment.state) -Changed $false -TargetRoot $root -SourceHostPath $source.path -OldSha256 $sameMap['supervisor.py'] -NewSha256 $sameMap['supervisor.py'] -Message '既知のhost世代です。'
            }
            Assert-NoPendingArtifacts -Root $root -AllowRolledBackNext:$AllowRolledBackNext
            Assert-AppLockClear -Root $root
            Assert-TargetProcessesStopped -Root $root
            Assert-SecureRepairLeaseAvailable -Root $root
            return New-SecureRepairResult -State ([string]$assessment.state) -Changed $false -TargetRoot $root -SourceHostPath $source.path -OldSha256 $sameMap['supervisor.py'] -NewSha256 $sameMap['supervisor.py'] -Message '既知のhost世代です。変更しません。'
        }
    }
    if ($assessment.state -eq 'known_new') {
        if ($InspectOnly) {
            return New-SecureRepairResult -State 'known_new' -Changed $false -TargetRoot $root -SourceHostPath $source.path -OldSha256 $script:KnownNewHostSha256['supervisor.py'] -NewSha256 $script:KnownNewHostSha256['supervisor.py'] -Message '既知の新版hostです。'
        }
        Assert-NoPendingArtifacts -Root $root -AllowRolledBackNext:$AllowRolledBackNext
        Assert-AppLockClear -Root $root
        Assert-TargetProcessesStopped -Root $root
        Assert-SecureRepairLeaseAvailable -Root $root
        return New-SecureRepairResult -State 'known_new' -Changed $false -TargetRoot $root -SourceHostPath $source.path -OldSha256 $script:KnownNewHostSha256['supervisor.py'] -NewSha256 $script:KnownNewHostSha256['supervisor.py'] -Message '既知の新版hostです。変更しません。'
    }
    if ($assessment.state -notin @('known_old', 'known_old8', 'known_077', 'known_085', 'known_088', 'known_093')) { throw '修復対象hostが既知旧版ではありません。' }
    if ($InspectOnly) {
        return New-SecureRepairResult -State ([string]$assessment.state) -Changed $false -TargetRoot $root -SourceHostPath $source.path -OldSha256 $assessmentDefinition.map['supervisor.py'] -NewSha256 $sourceDefinition.map['supervisor.py'] -Message '既知の旧hostです。修復可能です。'
    }
    Assert-NoPendingArtifacts -Root $root -AllowRolledBackNext:$AllowRolledBackNext
    Assert-AppLockClear -Root $root
    Assert-TargetProcessesStopped -Root $root
    $lock = $null
    $receipt = $null
    try {
        $lock = Open-SecureRepairLock -Root $root
        Assert-NoPendingArtifacts -Root $root -AllowRolledBackNext:$AllowRolledBackNext
        Assert-AppLockClear -Root $root
        Assert-TargetProcessesStopped -Root $root
        $operationId = ([guid]::NewGuid().ToString('N'))
        $repairRoot = Get-RepairRoot -TargetRoot $root
        if (-not (Test-Path -LiteralPath $repairRoot)) {
            Assert-NoReparseAncestors -Path $repairRoot -Label 'repair workspace' -AllowMissing
            New-Item -ItemType Directory -Path $repairRoot -Force | Out-Null
        }
        Assert-RegularDirectory -Path $repairRoot -Label 'repair workspace' | Out-Null
        $operationRoot = Join-Path $repairRoot $operationId
        Assert-NoReparseAncestors -Path $operationRoot -Label 'repair operation' -AllowMissing
        New-Item -ItemType Directory -Path $operationRoot -Force | Out-Null
        Assert-RegularDirectory -Path $operationRoot -Label 'repair operation' | Out-Null
        $backup = Join-Path $operationRoot 'backup-current'
        $staging = Join-Path $operationRoot 'staging-current'
        $parked = Join-Path $operationRoot 'parked-old'
        $oldInventory = Compare-CurrentInventory -Root $assessment.current -Expected $assessmentDefinition.map -ExpectedState ([string]$assessment.state)
        $newInventory = Compare-CurrentInventory -Root $source.path -Expected $sourceDefinition.map -ExpectedState ([string]$source.inventory.state)
        Copy-SecureTree -Source $assessment.current -Destination $backup
        Compare-CurrentInventory -Root $backup -Expected $assessmentDefinition.map -ExpectedState ([string]$assessment.state) | Out-Null
        Copy-SecureTree -Source $source.path -Destination $staging -ExcludePythonCache
        Compare-CurrentInventory -Root $staging -Expected $sourceDefinition.map -ExpectedState ([string]$source.inventory.state) | Out-Null
        Assert-CacheFreeCurrent -Root $staging
        $receipt = New-SecureReceipt -TargetRoot $root -OperationId $operationId -OldInventory $oldInventory -NewInventory $newInventory
        Invoke-SecureReceiptWrite -TargetRoot $root -Receipt $receipt
        Compare-CurrentInventory -Root $assessment.current -Expected $assessmentDefinition.map -ExpectedState ([string]$assessment.state) | Out-Null
        Assert-AppLockClear -Root $root
        Assert-TargetProcessesStopped -Root $root
        Invoke-SecureDirectoryMove -Source $assessment.current -Destination $parked
        Assert-ReceiptTree -TargetRoot $root -Receipt $receipt -Kind parked | Out-Null
        $receipt.state = 'old_parked'
        Invoke-SecureReceiptWrite -TargetRoot $root -Receipt $receipt
        Compare-CurrentInventory -Root $staging -Expected $sourceDefinition.map -ExpectedState ([string]$source.inventory.state) | Out-Null
        Assert-CacheFreeCurrent -Root $staging
        Assert-AppLockClear -Root $root
        Assert-TargetProcessesStopped -Root $root
        Invoke-SecureDirectoryMove -Source $staging -Destination $assessment.current
        Compare-CurrentInventory -Root $assessment.current -Expected $sourceDefinition.map -ExpectedState ([string]$source.inventory.state) | Out-Null
        $receipt.state = 'new_promoted'
        Invoke-SecureReceiptWrite -TargetRoot $root -Receipt $receipt
        $receipt.state = 'complete'
        Invoke-SecureReceiptWrite -TargetRoot $root -Receipt $receipt
        return New-SecureRepairResult -State ([string]$source.inventory.state) -Changed $true -TargetRoot $root -SourceHostPath $source.path -ReceiptRelative $receipt.receipt_relative -BackupRelative $receipt.backup_relative -OldSha256 $assessmentDefinition.map['supervisor.py'] -NewSha256 $sourceDefinition.map['supervisor.py'] -RecoveryAction 'old_backed_up_new_promoted' -Message '検証済み新版hostへ交換しました。'
    }
    finally {
        if ($null -ne $lock) {
            $lock.Dispose()
        }
    }
}

function Get-SecureRepairFailureClassification {
    <#
    Convert an internal validation message into a small, stable contract for
    the batch/PowerShell caller.  The message itself is deliberately not
    returned: it can contain paths or provider details.  Keep this mapping
    fail-closed; an unknown error remains ``validation``.
    #>
    param([Parameter(Mandatory = $true)][string]$FailureMessage)

    if ($FailureMessage -match '対象Start[.]batがcanonical SHAと一致しません|hostがないrootのStart[.]batがcanonicalではありません') {
        return [ordered]@{
            kind = 'start-hash-mismatch'
            message = '対象フォルダーのStart.batが、対応する更新方式の正規ファイルと一致しません。'
        }
    }
    if ($FailureMessage -match 'app/nexus_ark[.]lockが実行中のNexus Arkを示しています') {
        return [ordered]@{
            kind = 'app-lock-active'
            message = 'アプリのlockが実行中のNexus Arkを示しています。Nexus Arkを完全に終了してから再試行してください。'
        }
    }
    if ($FailureMessage -match '対象rootのNexus Ark processが実行中です|Start[.]batを実行するcmd processが残っています|Nexus Arkが実行中です') {
        return [ordered]@{
            kind = 'process-running'
            message = '対象フォルダーのNexus Arkまたは起動処理が実行中です。完全に終了してから再試行してください。'
        }
    }
    if ($FailureMessage -match '対象rootのprocessを確認できません|Nexus Ark processの詳細を確認できません|Nexus Ark processの実行元を確認できません|app/nexus_ark[.]lockのprocessを確認できません|process状態を安全に確認できません') {
        return [ordered]@{
            kind = 'process-unknown'
            message = '対象フォルダーの実行中プロセスを安全に確認できないため停止しました。診断ツールの結果を確認してください。'
        }
    }
    if ($FailureMessage -match '同じ更新host修復、または未解決の修復lockがあります|同じ更新host修復が実行中です') {
        return [ordered]@{
            kind = 'repair-lock-active'
            message = '更新host修復のlockが使用中です。ほかの準備ツールを閉じてから再試行してください。'
        }
    }
    if ($FailureMessage -match 'app/nexus_ark[.]lockの形式を安全に確認できません|app/nexus_ark[.]lockのprocess状態を安全に確認できません') {
        return [ordered]@{
            kind = 'app-lock-invalid'
            message = 'アプリのlockを安全に確認できないため停止しました。既存フォルダーとbackupを保持してください。'
        }
    }
    if ($FailureMessage -match '未解決の更新lockまたはnext世代があります|未解決の更新stagingがあります|対象root直下に一時更新fileがあります|未解決の[.]next世代があります|update_recoveryに一時更新fileがあります|更新transaction|未完了の更新transactionがあります') {
        return [ordered]@{
            kind = 'pending-update'
            message = '未完了の更新作業が残っているため停止しました。既存フォルダーとbackupを保持してください。'
        }
    }
    if ($FailureMessage -match 'repair receipt|修復receipt|repair workspace|修復receiptとhost世代|修復receiptが複数|staging世代が残っています') {
        return [ordered]@{
            kind = 'receipt-or-recovery'
            message = '更新hostの復旧記録を安全に確認できないため停止しました。既存フォルダーとbackupを保持してください。'
        }
    }
    if ($FailureMessage -match 'host inventory|updater/current|sourceはdistribution|sourceとtarget|SHA256|reparse|リンク|directory|file|path|canonical host|修復対象host') {
        return [ordered]@{
            kind = 'inventory-or-path'
            message = '更新hostまたは対象フォルダーの構成を検証できないため停止しました。'
        }
    }
    return [ordered]@{
        kind = 'validation'
        message = '更新host修復の安全条件を満たさないため停止しました。'
    }
}

if (-not $script:IsDotSourced) {
    try {
        if ([string]::IsNullOrWhiteSpace($TargetRoot) -or [string]::IsNullOrWhiteSpace($SourceHostPath)) {
            throw 'TargetRootとSourceHostPathが必要です。'
        }
        $result = Invoke-SecureUpdateHostRepair -TargetRoot $TargetRoot -SourceHostPath $SourceHostPath -InspectOnly:$InspectOnly -AllowRolledBackNext:$AllowRolledBackNext
        $result | ConvertTo-Json -Depth 12 -Compress
        exit 0
    }
    catch {
        $failureMessage = [string]$_.Exception.Message
        $failure = Get-SecureRepairFailureClassification -FailureMessage $failureMessage
        $failureKind = [string]$failure.kind
        [Console]::Error.WriteLine(('更新host修復を拒否しました: {0}' -f $failureKind))
        [ordered]@{
            state = 'blocked'
            changed = $false
            # Never echo user-selected absolute paths in a rejection result.
            # The caller already knows the selected root and can correlate the
            # result with its own in-memory operation state.
            target_root = ''
            source_host = ''
            receipt_path = ''
            backup_path = ''
            old_sha256 = ''
            new_sha256 = ''
            recovery_action = ''
            failure_kind = $failureKind
            message = [string]$failure.message
        } | ConvertTo-Json -Depth 8 -Compress
        exit 1
    }
}
