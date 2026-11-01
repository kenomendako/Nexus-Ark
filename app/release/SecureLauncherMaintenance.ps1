# Shared read-only checks for interrupted canonical Start.bat maintenance.
# Helpers supply their fixed SHA allowlists; no caller-provided file authorizes
# a launcher. Evidence remains in place and is never executed or promoted.
function Test-NexusLauncherTemporaryName {
    param([string]$Name)
    return $Name -match '^Start\.bat\.refresh-[0-9a-fA-F]{32}\.tmp$'
}

function Assert-NexusLauncherMaintenance {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string[]]$KnownStartHashes,
        [switch]$OwnLease
    )
    $leasePath = Join-Path $Root 'update_recovery\secure-launcher-refresh.lock'
    Assert-NoReparseAncestors -Path $leasePath -Label 'launcher maintenance lease' -AllowMissing
    if ((Test-Path -LiteralPath $leasePath) -and -not $OwnLease) {
        $lease = $null
        try { $lease = [IO.File]::Open($leasePath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
        catch { throw 'Start.batの準備が実行中です。完了を待ってください。' }
        finally { if ($null -ne $lease) { $lease.Dispose() } }
    }
    foreach ($entry in @(Get-ChildItem -LiteralPath $Root -Force -ErrorAction Stop)) {
        if (-not (Test-NexusLauncherTemporaryName $entry.Name)) { continue }
        Assert-RegularFile -Path $entry.FullName -Label 'interrupted launcher temporary' | Out-Null
        $operationId = $entry.Name.Substring('Start.bat.refresh-'.Length, 32)
        $operation = Join-Path (Join-Path $Root 'update_recovery\secure-launcher-refresh') $operationId
        Assert-RegularDirectory -Path $operation -Label 'interrupted launcher backup' | Out-Null
        $backup = Assert-RegularFile -Path (Join-Path $operation 'Start.bat') -Label 'interrupted launcher backup bytes'
        $recordPath = Assert-RegularFile -Path (Join-Path $operation 'record.json') -Label 'interrupted launcher record'
        try { $record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { throw '中断したStart.batの準備記録を確認できません。' }
        $oldSha = (Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash.ToLowerInvariant()
        $newSha = (Get-FileHash -LiteralPath $entry.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $current = Assert-RegularFile -Path (Join-Path $Root 'Start.bat') -Label 'current launcher'
        $currentSha = (Get-FileHash -LiteralPath $current -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($KnownStartHashes -notcontains $oldSha -or $KnownStartHashes -notcontains $newSha -or
            $KnownStartHashes -notcontains $currentSha -or $oldSha -ne [string]$record.old_start_sha256 -or
            $newSha -ne [string]$record.new_start_sha256 -or [string]$record.operation_id -ine $operationId -or
            [string]$record.state -notin @('prepared','complete') -or
            -not [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetFullPath([string]$record.target_root).TrimEnd('\'), [IO.Path]::GetFullPath($Root).TrimEnd('\'))) {
            throw '中断したStart.batの世代と記録を照合できません。証跡を残して停止します。'
        }
    }
}
