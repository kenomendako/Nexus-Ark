<#
Nexus Ark: prepare the managed Python used by the protected Windows launcher.

This helper deliberately has no dependency on the application or on a Python
installation.  It runs under Windows PowerShell 5.1 before the protected host
is started and only asks uv to add CPython 3.12.16 to uv's managed directory.
It never edits a virtual environment, a launcher, or the Windows registry.

The caller supplies the exact uv executable.  Installation disables configuration discovery; the legacy selection probe
keeps the launcher configuration so an unsafe user preference is detected.  The process-local environment used for uv is restored after
each child process, including when uv fails to start.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$UvExecutable,

    [switch]$CheckOnly,

    # An explicit directory is useful for isolated validation.  When omitted,
    # the directory reported by `uv python dir` is used.
    [string]$InstallDirectory = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:RequiredPython = '3.12.16'
$script:RequiredPythonVersion = [version]$script:RequiredPython
$script:PythonRequest = 'cpython@3.12.16'
$script:MinimumUvVersion = [version]'0.13.4'
$script:PythonDownloadsCatalogSha256 = '03d6e44c6566cb8af8b429cc64265797e918a9221250cd41dbbf2b6e87fc23db'
$script:VersionProbe = 'import sys; print(sys.implementation.name); print(sys.version.split()[0])'

function Get-NormalizedPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw 'Path is empty.'
    }
    try {
        return [IO.Path]::GetFullPath($Path)
    }
    catch {
        throw 'Path could not be resolved.'
    }
}

function Test-ReparsePoint {
    param(
        [Parameter(Mandatory = $true)]
        $Item
    )

    return (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Assert-SafePath {
    <#
    Check every existing component, including ancestors of a not-yet-created
    managed root.  This prevents a junction/symlink from redirecting uv into
    an existing interpreter or user directory.
    #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [switch]$AllowMissingLeaf,

        [switch]$RequireFile,

        [switch]$RequireDirectory,

        [string]$Label = 'path'
    )

    $full = Get-NormalizedPath $Path
    $root = [IO.Path]::GetPathRoot($full)
    if ([string]::IsNullOrWhiteSpace($root)) {
        throw ('Could not resolve the root of {0}.' -f $Label)
    }

    $tail = $full.Substring($root.Length)
    $parts = @($tail -split '[\\/]' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $cursor = $root
    $lastExisting = $null
    foreach ($part in $parts) {
        $cursor = Join-Path $cursor $part
        $item = Get-Item -LiteralPath $cursor -Force -ErrorAction SilentlyContinue
        if ($null -eq $item) {
            if (-not $AllowMissingLeaf) {
                throw ('{0} was not found.' -f $Label)
            }
            # The remaining components cannot be inspected until uv creates
            # them.  All existing ancestors have already been checked.
            break
        }
        if (Test-ReparsePoint $item) {
            throw ('{0} contains a link or reparse point.' -f $Label)
        }
        $lastExisting = $item
    }

    if ($RequireFile) {
        if ($null -eq $lastExisting -or $lastExisting.PSIsContainer) {
            throw ('{0} is not a regular file.' -f $Label)
        }
    }
    if ($RequireDirectory) {
        if ($null -eq $lastExisting -or -not $lastExisting.PSIsContainer) {
            throw ('{0} is not a regular directory.' -f $Label)
        }
    }
    return $full
}

function Assert-ManagedDirectoryPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $full = Assert-SafePath -Path $Path -AllowMissingLeaf -Label 'managed Python directory'
    $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($null -ne $item -and -not $item.PSIsContainer) {
        throw 'Managed Python path is not a regular directory.'
    }
    return $full
}

function Get-TrustedPythonDownloadsCatalog {
    $catalog = Assert-SafePath -Path (Join-Path $PSScriptRoot 'python-downloads.json') -RequireFile -Label 'Python downloads catalog'
    $actualHash = (Get-FileHash -LiteralPath $catalog -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $script:PythonDownloadsCatalogSha256) {
        throw 'The bundled Python downloads catalog failed its SHA256 check.'
    }
    return $catalog
}

function Set-ProcessEnvironmentValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    Set-Item -LiteralPath ('Env:{0}' -f $Name) -Value $Value
}

function Remove-ProcessEnvironmentValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    Remove-Item -LiteralPath ('Env:{0}' -f $Name) -ErrorAction SilentlyContinue
}

function Invoke-Uv {
    <#
    Invoke uv through PowerShell so a .cmd fake can be used by the isolated
    canary as well as the real uv.exe.  PowerShell 5.1 has no ProcessStartInfo
    environment dictionary, so the requested child-only variables are applied
    for this call and restored in finally before returning to the caller.
    #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [hashtable]$Environment = @{}
    )

    $previous = @{}
    $wasPresent = @{}
    # Never inherit an operator-provided catalog.  Only Install-RequiredPython
    # supplies this variable after validating the bundled, hash-pinned file.
    $environmentNames = @('UV_PYTHON_DOWNLOADS_JSON_URL')
    foreach ($name in $Environment.Keys) {
        if ($environmentNames -notcontains [string]$name) {
            $environmentNames += [string]$name
        }
    }
    $output = @()
    $exitCode = 9009
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        foreach ($name in $environmentNames) {
            $environmentPath = 'Env:{0}' -f $name
            if (Test-Path -LiteralPath $environmentPath) {
                $wasPresent[$name] = $true
                $environmentItem = Get-Item -LiteralPath $environmentPath
                $previous[$name] = [string]$environmentItem.Value
            }
            else {
                $wasPresent[$name] = $false
            }
            if ($Environment.ContainsKey($name)) {
                Set-ProcessEnvironmentValue -Name $name -Value ([string]$Environment[$name])
            }
            else {
                Remove-ProcessEnvironmentValue -Name $name
            }
        }

        try {
            # Windows PowerShell 5.1 represents native stderr records as
            # NativeCommandError.  With the script-wide Stop preference that
            # can turn harmless uv download progress into an exception.  Keep
            # the stderr records captured, but decide success by exit code.
            $ErrorActionPreference = 'Continue'
            $output = @(& $Executable @Arguments 2>&1 | ForEach-Object { [string]$_ })
            $exitCode = [int]$LASTEXITCODE
        }
        catch {
            # A missing or non-executable path must fail closed without
            # forwarding PowerShell's potentially sensitive command details.
            $output = @()
            $exitCode = 9009
        }
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        foreach ($name in $environmentNames) {
            if (-not $wasPresent.ContainsKey($name)) {
                continue
            }
            if ($wasPresent[$name]) {
                Set-ProcessEnvironmentValue -Name $name -Value $previous[$name]
            }
            else {
                Remove-ProcessEnvironmentValue -Name $name
            }
        }
    }

    return [pscustomobject]@{
        Output   = @($output)
        ExitCode = $exitCode
    }
}

function Get-UvVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable
    )

    $result = Invoke-Uv -Executable $Executable -Arguments @('--version', '--no-config')
    if ($result.ExitCode -ne 0) {
        throw 'Could not verify the uv version.'
    }
    $text = ($result.Output -join "`n")
    $match = [regex]::Match($text, '(?im)^\s*uv\s+([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)')
    if (-not $match.Success) {
        throw 'Could not parse the uv version output.'
    }
    try {
        $version = [version]$match.Groups[1].Value
    }
    catch {
        throw 'The uv version format is invalid.'
    }
    if ($version -lt $script:MinimumUvVersion) {
        throw 'uv 0.13.4 or newer is required.'
    }
    return $version
}

function Get-UvManagedDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable
    )

    $result = Invoke-Uv -Executable $Executable -Arguments @('python', 'dir')
    if ($result.ExitCode -ne 0) {
        throw 'Could not resolve the uv managed Python directory.'
    }
    $candidates = @(
        $result.Output |
            ForEach-Object { ([string]$_).Trim() } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($candidates.Count -eq 0) {
        throw 'uv returned an empty managed Python directory.'
    }
    # uv prints one absolute path.  Search backwards for a rooted path so a
    # future informational line cannot be mistaken for the managed directory.
    $directory = $null
    for ($index = $candidates.Count - 1; $index -ge 0; $index--) {
        $candidate = [string]($candidates[$index])
        if ([IO.Path]::IsPathRooted($candidate)) {
            $directory = $candidate
            break
        }
    }
    if ([string]::IsNullOrWhiteSpace($directory)) {
        throw 'uv did not return a rooted managed Python directory.'
    }
    $normalized = Assert-ManagedDirectoryPath -Path $directory
    return $normalized
}

function Get-VersionProbeArguments {
    param(
        [switch]$Offline
    )

    # This is intentionally the legacy root Start.bat selector.  Do not add
    # --managed-python or --no-config here: the probe must catch a caller's
    # global uv preference that would make the real legacy host choose an old
    # system interpreter.  --offline is the only probe-specific behavior.
    $arguments = @('run', '--no-project', '--python', '3.12')
    if ($Offline) {
        $arguments += '--offline'
    }
    $arguments += @('python', '-I', '-c', $script:VersionProbe)
    return $arguments
}

function Invoke-VersionProbe {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [Parameter(Mandatory = $true)]
        [string]$ManagedDirectory,

        [switch]$Offline
    )

    $environment = @{ UV_PYTHON_INSTALL_DIR = $ManagedDirectory }
    if ($Offline) {
        $environment['UV_OFFLINE'] = '1'
    }
    $result = Invoke-Uv -Executable $Executable -Arguments (Get-VersionProbeArguments -Offline:$Offline) -Environment $environment
    if ($result.ExitCode -ne 0) {
        return $null
    }

    $lines = @(
        $result.Output |
            ForEach-Object { ([string]$_).Trim() } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    for ($index = 0; $index -lt ($lines.Count - 1); $index++) {
        if ($lines[$index] -notmatch '^(?i:cpython)$') {
            continue
        }
        $versionMatch = [regex]::Match($lines[$index + 1], '^(\d+)\.(\d+)\.(\d+)$')
        if (-not $versionMatch.Success) {
            continue
        }
        try {
            $version = [version]::new(
                [int]$versionMatch.Groups[1].Value,
                [int]$versionMatch.Groups[2].Value,
                [int]$versionMatch.Groups[3].Value
            )
        }
        catch {
            return $null
        }
        if ($version.Major -eq 3 -and $version.Minor -eq 12 -and $version -ge $script:RequiredPythonVersion) {
            return [pscustomobject]@{
                Implementation = 'cpython'
                Version        = $version
            }
        }
        return $null
    }
    return $null
}

function Install-RequiredPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [Parameter(Mandatory = $true)]
        [string]$ManagedDirectory
    )

    # No --reinstall, --force, --upgrade, or cleanup operation is used.  uv
    # therefore leaves existing managed interpreters and bin entries intact.
    $arguments = @(
        'python', 'install', $script:PythonRequest,
        '--no-bin', '--no-registry', '--install-dir', $ManagedDirectory,
        '--no-config'
    )
    $catalog = Get-TrustedPythonDownloadsCatalog
    $environment = @{
        UV_PYTHON_INSTALL_DIR = $ManagedDirectory
        UV_PYTHON_DOWNLOADS_JSON_URL = $catalog
    }
    $result = Invoke-Uv -Executable $Executable -Arguments $arguments -Environment $environment
    if ($result.ExitCode -ne 0) {
        throw 'Could not install the required managed Python. Check network access and uv.'
    }

    # Re-check the path after uv has created it.  A reparse point introduced
    # during installation is treated as a failure before the host starts.
    Assert-ManagedDirectoryPath -Path $ManagedDirectory | Out-Null
}

try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'This helper only runs on Windows.'
    }

    $uv = Assert-SafePath -Path $UvExecutable -RequireFile -Label 'uv executable'
    $null = Get-UvVersion -Executable $uv

    if ([string]::IsNullOrWhiteSpace($InstallDirectory)) {
        $managedDirectory = Get-UvManagedDirectory -Executable $uv
    }
    else {
        $managedDirectory = Assert-ManagedDirectoryPath -Path $InstallDirectory
    }

    $existing = Invoke-VersionProbe -Executable $uv -ManagedDirectory $managedDirectory -Offline
    if ($null -eq $existing) {
        if ($CheckOnly) {
            throw ('Could not select CPython {0} or newer from the managed directory.' -f $script:RequiredPython)
        }
        Install-RequiredPython -Executable $uv -ManagedDirectory $managedDirectory
        $existing = Invoke-VersionProbe -Executable $uv -ManagedDirectory $managedDirectory -Offline
        if ($null -eq $existing) {
            throw 'The managed Python implementation or version could not be verified.'
        }
    }

    # Success output is intentionally limited to the verified interpreter
    # version.  Paths, command lines, and inherited environment values are not
    # printed.
    Write-Output ($existing.Version.ToString())
    exit 0
}
catch {
    # Do not print captured uv output: it may contain user paths or credentials
    # supplied by an external uv configuration.  The helper always fails closed.
    Write-Host ('[Nexus Ark] secure Python preparation failed closed: {0}' -f $_.Exception.Message)
    exit 1
}
