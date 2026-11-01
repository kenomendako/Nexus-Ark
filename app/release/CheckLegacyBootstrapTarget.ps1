[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Root,
      [Parameter(Mandatory=$true)][string]$SourceRoot)
$ErrorActionPreference='Stop'
try {
    . (Join-Path $PSScriptRoot 'PrepareExistingUpdate.ps1')
    $source = Get-PackageSource -PackageRoot $SourceRoot
    $target = Assert-TargetRoot -Root $Root -Source $source
    exit 0
} catch { exit 1 }
