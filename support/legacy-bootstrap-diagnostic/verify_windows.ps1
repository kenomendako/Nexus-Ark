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

Set-Content -LiteralPath (Join-Path $root 'Start.bat') -Value '@echo off; robocopy old new'
$legacy = Get-LegacyBootstrapDiagnostic $root
if (-not $legacy.layout.start.legacy_robocopy -or $legacy.layout.start.modern_host) { throw 'legacy-layout-wrong' }
Set-Content -LiteralPath (Join-Path $root 'Start.bat') -Value '@echo off; python -m update_host.supervisor'
$partial = Get-LegacyBootstrapDiagnostic $root
if (-not $partial.layout.start.modern_host -or $partial.layout.paths['updater\current'].directory) { throw 'missing-host-layout-wrong' }
New-Item -ItemType Directory -Path (Join-Path $root 'updater\current\update_host') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $Tool))) 'app\update_host\supervisor.py') -Destination (Join-Path $root 'updater\current\update_host\supervisor.py')
$matching = Get-LegacyBootstrapDiagnostic $root
if (-not $matching.layout.host_files['supervisor.py'].matches_089) { throw 'real-089-hash-mismatch' }
Set-Content -LiteralPath (Join-Path $root 'updater\current\update_host\supervisor.py') -Value 'DO_NOT_DISCLOSE'
$before = Snapshot
$mismatch = Get-LegacyBootstrapDiagnostic $root
if (-not $mismatch.layout.host_files['supervisor.py'].present -or $mismatch.layout.host_files['supervisor.py'].matches_089) { throw 'bad-host-accepted' }
$combined = Get-LegacyBootstrapDiagnostic -Root $root -PackageRoot (Join-Path $workspace 'next-root')
if ($combined.package.same_or_nested -or $combined.package.layout.paths['updater\current'].directory) { throw 'package-layout-wrong' }
$same = Get-LegacyBootstrapDiagnostic -Root $root -PackageRoot $root
if (-not $same.package.same_or_nested) { throw 'same-selection-not-detected' }
Set-Content -LiteralPath (Join-Path $root 'updater\current\extra-private-name.txt') -Value 'DO_NOT_DISCLOSE'
$extra = Get-LegacyBootstrapDiagnostic $root
if ($extra.layout.extra_entry_counts.current -ne 1 -or ($extra | ConvertTo-Json -Depth 8).Contains('extra-private-name')) { throw 'extra-count-wrong-or-private-name-leaked' }
$before = Snapshot
$output = Join-Path $case 'Reports'
New-Item -ItemType Directory -Path $output | Out-Null
$name = Save-DiagnosticReport -Result $mismatch -TargetRoot $root -Directory $output
$report = Get-Content -LiteralPath (Join-Path $output $name) -Raw -Encoding UTF8
if ($report.Contains('DO_NOT_DISCLOSE') -or $report.Contains($case)) { throw 'report-secret-leaked' }
foreach ($destination in @($root,$workspace,(Join-Path $workspace 'next-root'))) {
    $rejected = $false
    try { Save-DiagnosticReport -Result $mismatch -TargetRoot $root -Directory $destination | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'report-wrote-protected-tree' }
}
$rejected = $false
try { Save-DiagnosticReport -Result $mismatch -TargetRoot $root -Directory $output -PackageRoot $output | Out-Null } catch { $rejected = $true }
if (-not $rejected) { throw 'report-wrote-package-tree' }
# The only newly added entries are the explicitly requested report and its folder.
$after = Snapshot
foreach ($line in ($before -split "`n")) { if (($after -split "`n") -notcontains $line) { throw 'layout-diagnostic-changed-target' } }
$extras = @($after -split "`n" | Where-Object { ($before -split "`n") -notcontains $_ })
if ($extras.Count -ne 2 -or @($extras | Where-Object { -not $_.StartsWith($output) }).Count -ne 0) { throw 'unexpected-diagnostic-write' }

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
$entryRoot = Join-Path $case 'Entry root'
$tools = Join-Path $case 'Entry tools'
$entryPackage = Join-Path $case 'Entry package'
New-Item -ItemType Directory -Path $entryRoot,$tools,$entryPackage | Out-Null
Set-Content -LiteralPath (Join-Path $entryRoot 'Start.bat') -Value '@echo off'
$entryTool = Join-Path $tools 'InspectLegacyBootstrap.ps1'
# Exercise the actual packaged CRLF script entry without interactive selection.
$scriptText = [IO.File]::ReadAllText($Tool).Replace("`r`n","`n").Replace("`n","`r`n")
[IO.File]::WriteAllText($entryTool,$scriptText,(New-Object Text.UTF8Encoding($true)))
$command = "& '" + $entryTool.Replace("'","''") + "' -TargetRoot '" + $entryRoot.Replace("'","''") + "' -PackageRoot '" + $entryPackage.Replace("'","''") + "'"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
$psi = New-Object Diagnostics.ProcessStartInfo
$psi.FileName = Join-Path $PSHOME 'powershell.exe'
$psi.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ' + $encoded
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$process = [Diagnostics.Process]::Start($psi)
try {
    $outTask = $process.StandardOutput.ReadToEndAsync()
    $errTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(30000)) { $process.Kill(); throw 'script-entry-timeout' }
    $outTask.GetAwaiter().GetResult() | Out-Null
    $errTask.GetAwaiter().GetResult() | Out-Null
    if ($process.ExitCode -ne 0) { throw 'script-entry-failed' }
} finally { $process.Dispose() }
$reports = @(Get-ChildItem -LiteralPath $tools -Filter 'NexusArk-legacy-diagnostic-*.json')
if ($reports.Count -ne 1) { throw 'script-entry-report-missing' }
$entryReport = Get-Content -LiteralPath $reports[0].FullName -Raw -Encoding UTF8 | ConvertFrom-Json
if ($entryReport.tool_revision -ne 2 -or -not $entryReport.read_only -or $entryReport.package.same_or_nested) { throw 'script-entry-report-wrong' }
if (@(Get-ChildItem -LiteralPath $entryRoot -Force).Count -ne 1) { throw 'script-entry-changed-target' }
Write-Output '{"passed":true,"cases":["state-and-presence","no-private-output","target-hash-and-tree-unchanged","native-sharing-violation-32","legacy-vs-modern-layout","missing-host","real-089-host-hash","wrong-host-hash","package-layout","same-selection","unknown-file-count-without-name","report-no-private-output","report-protected-tree-rejected","only-report-written","missing-workspace","unknown-state-rejected","native-junction-rejected","actual-crlf-script-entry-and-report"]}'
