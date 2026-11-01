param([Parameter(Mandatory=$true)][string]$Tool)
$ErrorActionPreference = 'Stop'
. $Tool
$case = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('Temp\NexusLegacyDiagnostic-' + [Guid]::NewGuid().ToString('N'))
$root = Join-Path $case 'Nexus Ark old'
$workspace = Join-Path $case '.Nexus Ark old.nexus-secure-update'
New-Item -ItemType Directory -Path (Join-Path $root 'app'), (Join-Path $workspace 'next-root\app') -Force | Out-Null
Set-Content -LiteralPath (Join-Path $root 'Start.bat') -Value '@echo off'
Set-Content -LiteralPath (Join-Path $root 'app\version.json') -Value '{"version":"0.4.0.1","secret":"DO_NOT_DISCLOSE"}'
Set-Content -LiteralPath (Join-Path $workspace 'next-root\app\version.json') -Value '{"version":"0.5.0.89"}'
Set-Content -LiteralPath (Join-Path $workspace 'receipt.json') -Value '{"state":"rolled_back","secret":"DO_NOT_DISCLOSE"}'
function Snapshot {
    @(Get-ChildItem -LiteralPath $case -Force -Recurse | ForEach-Object {
        if ($_.PSIsContainer) { $_.FullName + ':directory' }
        else { $_.FullName + ':' + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
    }) -join "`n"
}
$before = Snapshot
$value = Get-LegacyBootstrapDiagnostic $root
if ($value.receipt_state -ne 'rolled_back' -or $value.versions.root -ne '0.4.0.1' -or $value.versions.next_root -ne '0.5.0.89') { throw 'wrong-state' }
if (-not $value.folders.root -or -not $value.folders.next_root -or $value.folders.previous) { throw 'wrong-presence' }
if (-not $value.delete_access_probe.root.opened) { throw 'normal-probe-failed' }
$json = $value | ConvertTo-Json -Depth 6
if ($json.Contains('DO_NOT_DISCLOSE') -or $json.Contains($case) -or $json.Contains('secret')) { throw 'private-value-leaked' }
if ((Snapshot) -ne $before) { throw 'diagnostic-changed-target' }

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class LegacyDiagnosticTestLock {
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
 public static extern SafeFileHandle CreateFileW(string p,uint a,uint s,IntPtr sec,uint d,uint f,IntPtr t);
}
'@
$handle = [LegacyDiagnosticTestLock]::CreateFileW($root, 0x80000000L, 3, [IntPtr]::Zero, 3, 0x02000000, [IntPtr]::Zero)
try {
    if ($handle.IsInvalid) { throw 'lock-fixture-failed' }
    $locked = Get-LegacyBootstrapDiagnostic $root
    if ($locked.delete_access_probe.root.opened -or $locked.delete_access_probe.root.winerror -ne 32) { throw 'sharing-lock-not-detected' }
} finally { $handle.Dispose() }
if ((Snapshot) -ne $before) { throw 'locked-diagnostic-changed-target' }

$other = Join-Path $case 'No workspace'
New-Item -ItemType Directory -Path $other | Out-Null
Set-Content -LiteralPath (Join-Path $other 'Start.bat') -Value '@echo off'
$empty = Get-LegacyBootstrapDiagnostic $other
if ($empty.receipt_state -ne 'missing' -or $empty.folders.workspace) { throw 'missing-workspace-misreported' }
Set-Content -LiteralPath (Join-Path $workspace 'receipt.json') -Value '{"state":"unknown-private-value"}'
$rejected = $false
try { Get-LegacyBootstrapDiagnostic $root | Out-Null } catch { $rejected = $true }
if (-not $rejected) { throw 'unknown-state-accepted' }
$junction = Join-Path $case '.No workspace.nexus-secure-update'
New-Item -ItemType Junction -Path $junction -Target $workspace | Out-Null
$rejected = $false
try { Get-LegacyBootstrapDiagnostic $other | Out-Null } catch { $rejected = $true }
if (-not $rejected) { throw 'junction-followed' }
Write-Output '{"passed":true,"cases":["state-and-presence","no-private-output","target-hash-and-tree-unchanged","native-sharing-violation-32","missing-workspace","unknown-state-rejected","native-junction-rejected"]}'
