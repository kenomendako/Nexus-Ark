<#
    Secure bootstrap used by PrepareExistingUpdate.ps1 for installations that
    still use the July (or an older) launcher.

    This file deliberately contains no product entry point.  The preparation
    GUI dot-sources it and calls Invoke-LegacySecureBootstrap.  Every child
    process gets a newly constructed environment; in particular, the old
    installation's Python and uv configuration are never inherited.
#>

$script:PythonDownloadsCatalogSha256 = '03d6e44c6566cb8af8b429cc64265797e918a9221250cd41dbbf2b6e87fc23db'

function Get-LegacyBootstrapFullPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw '[legacy-bootstrap-invalid-path]'
    }
    try {
        return [IO.Path]::GetFullPath($Path)
    }
    catch {
        throw '[legacy-bootstrap-invalid-path]'
    }
}

function Test-LegacyBootstrapReparsePoint {
    param([Parameter(Mandatory = $true)]$Item)
    return (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Assert-LegacyBootstrapSafeAncestors {
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = Get-LegacyBootstrapFullPath $Path
    $cursor = $full
    while ($true) {
        $item = Get-Item -LiteralPath $cursor -Force -ErrorAction SilentlyContinue
        if ($null -ne $item -and (Test-LegacyBootstrapReparsePoint $item)) {
            throw '[legacy-bootstrap-reparse-path]'
        }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent -or $parent -eq $cursor) {
            break
        }
        $cursor = $parent
    }
    return $full
}

function Assert-LegacyBootstrapDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$AllowAbsent
    )

    $full = Assert-LegacyBootstrapSafeAncestors $Path
    $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        if ($AllowAbsent) { return $full }
        throw '[legacy-bootstrap-missing-directory]'
    }
    if (-not $item.PSIsContainer -or (Test-LegacyBootstrapReparsePoint $item)) {
        throw '[legacy-bootstrap-unsafe-directory]'
    }
    return $full
}

function Assert-LegacyBootstrapFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = Assert-LegacyBootstrapSafeAncestors $Path
    $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        throw '[legacy-bootstrap-missing-file]'
    }
    if ($item.PSIsContainer -or (Test-LegacyBootstrapReparsePoint $item)) {
        throw '[legacy-bootstrap-unsafe-file]'
    }
    return $item.FullName
}

function Get-LegacyBootstrapPythonDownloadsCatalog {
    param([Parameter(Mandatory = $true)][string]$SourceApp)

    $catalog = Assert-LegacyBootstrapFile (Join-Path $SourceApp 'release\python-downloads.json')
    $actualHash = (Get-FileHash -LiteralPath $catalog -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $script:PythonDownloadsCatalogSha256) {
        throw '[legacy-bootstrap-python-catalog]'
    }
    return $catalog
}

function Test-LegacyBootstrapChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Child
    )

    $rootFull = (Get-LegacyBootstrapFullPath $Root).TrimEnd([char[]]@('\', '/')) + '\'
    $childFull = Get-LegacyBootstrapFullPath $Child
    return $childFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)
}

function Test-LegacyBootstrapSameOrChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Child
    )

    $rootFull = (Get-LegacyBootstrapFullPath $Root).TrimEnd([char[]]@('\', '/'))
    $childFull = (Get-LegacyBootstrapFullPath $Child).TrimEnd([char[]]@('\', '/'))
    return ([StringComparer]::OrdinalIgnoreCase.Equals($rootFull, $childFull) -or
        (Test-LegacyBootstrapChildPath -Root $rootFull -Child $childFull))
}

function Set-LegacyBootstrapStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [string]$Phase = ''
    )

    # Set-PreparationStatus is supplied by PrepareExistingUpdate.ps1.  The
    # command lookup also keeps this helper independently testable.
    $statusCommand = Get-Command -Name Set-PreparationStatus -CommandType Function -ErrorAction SilentlyContinue
    if ($null -ne $statusCommand) {
        try { & $statusCommand -Text $Text | Out-Null } catch { }
    }
    $pumpCommand = Get-Command -Name Update-PreparationProgress -CommandType Function -ErrorAction SilentlyContinue
    if ($null -ne $pumpCommand) {
        try { & $pumpCommand | Out-Null } catch { }
    }
}

function Quote-LegacyBootstrapArgument {
    param([Parameter(Mandatory = $true)][string]$Value)

    # Windows paths cannot contain a literal double quote.  Doubling a
    # trailing backslash keeps it from escaping the closing quote.
    $escaped = $Value.Replace('"', '\"')
    $escaped = $escaped.TrimEnd('\') + $escaped.Substring($escaped.TrimEnd('\').Length)
    if ($escaped.EndsWith('\')) { $escaped = $escaped + '\' }
    return '"' + $escaped + '"'
}

function Get-LegacyBootstrapEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$UvExecutable,
        [Parameter(Mandatory = $true)][string]$ToolsRoot
    )

    $uvDirectory = [IO.Path]::GetDirectoryName((Get-LegacyBootstrapFullPath $UvExecutable))
    if ([string]::IsNullOrWhiteSpace($uvDirectory)) {
        throw '[legacy-bootstrap-invalid-uv]'
    }

    # ProcessStartInfo starts with a copy of the caller's environment.  The
    # caller must therefore clear it and add only this explicit allow-list.
    $allowList = @(
        'ALLUSERSPROFILE', 'APPDATA', 'COMMONPROGRAMFILES',
        'COMMONPROGRAMFILES(X86)', 'COMSPEC', 'HOMEDRIVE', 'HOMEPATH',
        'LOCALAPPDATA', 'NUMBER_OF_PROCESSORS', 'OS', 'PATH', 'PATHEXT',
        'PROCESSOR_ARCHITECTURE', 'PROCESSOR_IDENTIFIER', 'PROCESSOR_LEVEL',
        'PROCESSOR_REVISION', 'PROGRAMDATA', 'PROGRAMFILES',
        'PROGRAMFILES(X86)', 'PUBLIC', 'SystemDrive', 'SystemRoot',
        'TEMP', 'TMP', 'USERDOMAIN', 'USERDOMAIN_ROAMINGPROFILE',
        'USERNAME', 'USERPROFILE', 'WINDIR'
    )
    $environment = @{}
    foreach ($name in $allowList) {
        $value = [Environment]::GetEnvironmentVariable($name, 'Process')
        if ($null -ne $value -and $value -ne '') {
            $environment[$name] = [string]$value
        }
    }
    $oldPath = [string]$environment['PATH']
    if ([string]::IsNullOrWhiteSpace($oldPath)) {
        $environment['PATH'] = $uvDirectory
    }
    else {
        $environment['PATH'] = $uvDirectory + ';' + $oldPath
    }
    # A repaired uv may already have a managed Python store selected by the
    # caller.  It must not be placed below the disposable bootstrap directory:
    # the venv is built from that store and deleting the tools directory would
    # otherwise delete the runtime needed by the new installation.  A missing
    # UV_PYTHON_INSTALL_DIR deliberately remains missing so uv uses its normal
    # managed store (prepared by Invoke-UvRepair).
    $explicitPythonRoot = [Environment]::GetEnvironmentVariable('UV_PYTHON_INSTALL_DIR', 'Process')
    if (-not [string]::IsNullOrWhiteSpace($explicitPythonRoot)) {
        $pythonRoot = Assert-LegacyBootstrapDirectory $explicitPythonRoot -AllowAbsent
        if (Test-LegacyBootstrapSameOrChildPath -Root $ToolsRoot -Child $pythonRoot) {
            throw '[legacy-bootstrap-python-store-boundary]'
        }
        $environment['UV_PYTHON_INSTALL_DIR'] = $pythonRoot
    }

    # Reusing an explicitly selected uv cache is safe after its ancestors are
    # checked.  Otherwise keep the cache in the owned temporary area; it is
    # disposable and cannot affect a user's existing cache.
    $explicitCacheRoot = [Environment]::GetEnvironmentVariable('UV_CACHE_DIR', 'Process')
    if ([string]::IsNullOrWhiteSpace($explicitCacheRoot)) {
        $environment['UV_CACHE_DIR'] = Join-Path $ToolsRoot 'uv-cache'
    }
    else {
        $cacheRoot = Assert-LegacyBootstrapDirectory $explicitCacheRoot -AllowAbsent
        if (Test-LegacyBootstrapSameOrChildPath -Root $ToolsRoot -Child $cacheRoot) {
            throw '[legacy-bootstrap-cache-boundary]'
        }
        $environment['UV_CACHE_DIR'] = $cacheRoot
    }
    # Offline validation is a fail-closed restriction, never an alternate index.
    if ([Environment]::GetEnvironmentVariable('UV_OFFLINE', 'Process') -eq '1') {
        $environment['UV_OFFLINE'] = '1'
    }
    # Do not let the fresh acquisition process read unrelated user netrc secrets.
    $netrc = Join-Path $ToolsRoot 'empty.netrc'
    $environment['NETRC'] = $netrc
    return $environment
}

function New-LegacyBootstrapProcessStartInfo {
    param(
        [Parameter(Mandatory = $true)][string]$FileName,
        [Parameter(Mandatory = $true)][string]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][hashtable]$Environment,
        [switch]$RedirectOutput
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $fullFile = Get-LegacyBootstrapFullPath $FileName
    $extension = [IO.Path]::GetExtension($fullFile).ToLowerInvariant()
    if ($extension -eq '.cmd' -or $extension -eq '.bat') {
        $comSpec = $Environment['ComSpec']
        if ([string]::IsNullOrWhiteSpace($comSpec)) {
            $comSpec = [IO.Path]::Combine([Environment]::SystemDirectory, 'cmd.exe')
        }
        $psi.FileName = $comSpec
        $psi.Arguments = '/d /s /c ""' + $fullFile + '" ' + $Arguments + '"'
    }
    else {
        $psi.FileName = $fullFile
        $psi.Arguments = $Arguments
    }
    $psi.WorkingDirectory = Assert-LegacyBootstrapDirectory $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    if ($RedirectOutput) {
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        try {
            $psi.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
            $psi.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
        }
        catch { }
    }
    $variables = $psi.EnvironmentVariables
    $variables.Clear()
    foreach ($entry in $Environment.GetEnumerator()) {
        $variables[[string]$entry.Key] = [string]$entry.Value
    }
    return $psi
}

function Wait-LegacyBootstrapProcess {
    param([Parameter(Mandatory = $true)][Diagnostics.Process]$Process)

    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        while (-not $Process.WaitForExit(100)) {
            $pump = Get-Command -Name Update-PreparationProgress -CommandType Function -ErrorAction SilentlyContinue
            if ($null -ne $pump) { try { & $pump | Out-Null } catch { } }
        }
        $Process.WaitForExit()
    }
    finally { $watch.Stop() }
}

function Invoke-LegacyBootstrapUv {
    param(
        [Parameter(Mandatory = $true)][string]$UvExecutable,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][hashtable]$Environment,
        [Parameter(Mandatory = $true)][string]$FailureCode
    )

    $parts = @()
    foreach ($argument in $ArgumentList) {
        if ($argument -match '^[A-Za-z0-9_./:=+@%-]+$' -and $argument -notmatch '[\\/]') {
            $parts += $argument
        }
        else {
            $parts += Quote-LegacyBootstrapArgument $argument
        }
    }
    $psi = New-LegacyBootstrapProcessStartInfo -FileName $UvExecutable -Arguments ($parts -join ' ') -WorkingDirectory $WorkingDirectory -Environment $Environment -RedirectOutput
    $process = $null
    try {
        $process = [Diagnostics.Process]::Start($psi)
        if ($null -eq $process) { throw '[legacy-bootstrap-process-start]' }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        Wait-LegacyBootstrapProcess -Process $process
        $stdoutTask.GetAwaiter().GetResult() | Out-Null
        $stderrTask.GetAwaiter().GetResult() | Out-Null
        if ($process.ExitCode -ne 0) { throw ('[{0}]' -f $FailureCode) }
    }
    catch {
        $message = [string]$_.Exception.Message
        if ($message -match '^\[[a-z0-9][a-z0-9_-]{1,63}\]$') { throw $message }
        throw ('[{0}]' -f $FailureCode)
    }
    finally {
        if ($null -ne $process) { $process.Dispose() }
    }
}

function Get-LegacyBootstrapUvVersion {
    param(
        [Parameter(Mandatory = $true)][string]$UvExecutable,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][hashtable]$Environment
    )

    $parts = @('--version')
    $psi = New-LegacyBootstrapProcessStartInfo -FileName $UvExecutable -Arguments '--version' -WorkingDirectory $WorkingDirectory -Environment $Environment -RedirectOutput
    $process = $null
    try {
        $process = [Diagnostics.Process]::Start($psi)
        if ($null -eq $process) { throw '[legacy-bootstrap-uv-version]' }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        Wait-LegacyBootstrapProcess -Process $process
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderrTask.GetAwaiter().GetResult() | Out-Null
        if ($process.ExitCode -ne 0) { throw '[legacy-bootstrap-uv-version]' }
    }
    catch {
        $message = [string]$_.Exception.Message
        if ($message -match '^\[[a-z0-9][a-z0-9_-]{1,63}\]$') { throw $message }
        throw '[legacy-bootstrap-uv-version]'
    }
    finally {
        if ($null -ne $process) { $process.Dispose() }
    }
    $match = [regex]::Match(([string]$stdout), '(?m)^uv\s+([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)')
    if (-not $match.Success) { throw '[legacy-bootstrap-uv-version]' }
    try {
        if ([version]$match.Groups[1].Value -lt [version]'0.13.4') {
            throw '[legacy-bootstrap-uv-version-old]'
        }
    }
    catch {
        $message = [string]$_.Exception.Message
        if ($message -match '^\[[a-z0-9][a-z0-9_-]{1,63}\]$') { throw $message }
        throw '[legacy-bootstrap-uv-version]'
    }
    return $match.Groups[1].Value
}

function ConvertFrom-LegacyBootstrapProgressLine {
    param([Parameter(Mandatory = $true)][string]$Line)

    if ([string]::IsNullOrWhiteSpace($Line)) { throw '[legacy-bootstrap-output-invalid]' }
    try { $value = $Line | ConvertFrom-Json -ErrorAction Stop }
    catch { throw '[legacy-bootstrap-output-invalid]' }
    if ($null -eq $value -or [string]$value.event -ne 'progress') {
        throw '[legacy-bootstrap-output-invalid]'
    }
    $phases = @('download', 'runtime', 'copy', 'environment', 'trial', 'switch', 'complete')
    $phase = [string]$value.phase
    if ($phase -notin $phases) { throw '[legacy-bootstrap-output-invalid]' }
    return $value
}

function ConvertFrom-LegacyBootstrapResultLine {
    param([Parameter(Mandatory = $true)][string]$Line)

    if ([string]::IsNullOrWhiteSpace($Line)) { throw '[legacy-bootstrap-output-invalid]' }
    try { $value = $Line | ConvertFrom-Json -ErrorAction Stop }
    catch { throw '[legacy-bootstrap-output-invalid]' }
    if ($null -eq $value -or [string]$value.event -ne 'result') {
        throw '[legacy-bootstrap-output-invalid]' }
    $state = [string]$value.state
    if ($state -notin @('complete', 'blocked', 'error')) {
        throw '[legacy-bootstrap-output-invalid]'
    }
    return $value
}

function Read-LegacyBootstrapJsonOutput {
    param([Parameter(Mandatory = $true)][Diagnostics.Process]$Process)

    $stderrTask = $Process.StandardError.ReadToEndAsync()
    $lineTask = $Process.StandardOutput.ReadLineAsync()
    $result = $null
    $seenResult = $false
    $malformed = $false
    try {
        while ($true) {
            while (-not $lineTask.IsCompleted) {
                $pump = Get-Command -Name Update-PreparationProgress -CommandType Function -ErrorAction SilentlyContinue
                if ($null -ne $pump) { try { & $pump | Out-Null } catch { } }
                Start-Sleep -Milliseconds 40
            }
            $line = $lineTask.GetAwaiter().GetResult()
            if ($null -eq $line) { break }
            if ($malformed) {
                $lineTask = $Process.StandardOutput.ReadLineAsync()
                continue
            }
            try {
                if ($seenResult) { throw '[legacy-bootstrap-output-invalid]' }
                $generic = $line | ConvertFrom-Json -ErrorAction Stop
                if ($null -eq $generic -or [string]::IsNullOrWhiteSpace([string]$generic.event)) {
                    throw '[legacy-bootstrap-output-invalid]'
                }
                if ([string]$generic.event -eq 'progress') {
                    $progress = ConvertFrom-LegacyBootstrapProgressLine -Line $line
                    $captions = @{
                        download = '更新ファイルを検証・取得しています…'
                        runtime = '署名済み専用ランタイムを準備しています…'
                        copy = '会話・設定を新版へコピーしています…'
                        environment = '新版のPython依存環境を準備しています…'
                        trial = '新版の起動確認をしています…'
                        switch = '安全に切り替えています…'
                        complete = '更新が完了しました。起動前の最終確認をしています…'
                    }
                    Set-LegacyBootstrapStatus -Text $captions[[string]$progress.phase] -Phase ([string]$progress.phase)
                }
                elseif ([string]$generic.event -eq 'result') {
                    if ($null -ne $result) { throw '[legacy-bootstrap-output-invalid]' }
                    $result = ConvertFrom-LegacyBootstrapResultLine -Line $line
                    $seenResult = $true
                }
                else { throw '[legacy-bootstrap-output-invalid]' }
            }
            catch { $malformed = $true }
            $lineTask = $Process.StandardOutput.ReadLineAsync()
        }
        Wait-LegacyBootstrapProcess -Process $Process
        # The stderr task is intentionally only drained.  Its content is never
        # returned to the caller or included in the user-visible error.
        $stderrTask.GetAwaiter().GetResult() | Out-Null
    }
    catch {
        $malformed = $true
        try { $stderrTask.GetAwaiter().GetResult() | Out-Null } catch { }
    }
    if ($malformed -or $null -eq $result) { throw '[legacy-bootstrap-output-invalid]' }
    return $result
}

function Invoke-LegacyBootstrapUpdateProcess {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExecutable,
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][hashtable]$Source,
        [Parameter(Mandatory = $true)][hashtable]$Target,
        [Parameter(Mandatory = $true)][string]$UvExecutable,
        [Parameter(Mandatory = $true)][hashtable]$Environment
    )

    $arguments = @(
        '-I', '-B', '-X', 'utf8',
        (Quote-LegacyBootstrapArgument $ScriptPath),
        '--root', (Quote-LegacyBootstrapArgument ([string]$Target.Root)),
        '--source-root', (Quote-LegacyBootstrapArgument ([string]$Source.Root)),
        '--uv', (Quote-LegacyBootstrapArgument $UvExecutable)
    ) -join ' '
    $psi = New-LegacyBootstrapProcessStartInfo -FileName $PythonExecutable -Arguments $arguments -WorkingDirectory ([string]$Source.App) -Environment $Environment -RedirectOutput
    $process = $null
    try {
        $process = [Diagnostics.Process]::Start($psi)
        if ($null -eq $process) { throw '[legacy-bootstrap-process-start]' }
        $result = Read-LegacyBootstrapJsonOutput -Process $process
        $exitCode = $process.ExitCode
    }
    catch {
        $message = [string]$_.Exception.Message
        if ($message -match '^\[[a-z0-9][a-z0-9_-]{1,63}\]$') { throw $message }
        throw '[legacy-bootstrap-child-failed]'
    }
    finally {
        if ($null -ne $process) { $process.Dispose() }
    }
    if ($exitCode -ne 0 -or [string]$result.state -ne 'complete') {
        $safeCodes = @('secure-release-required','invalid-update-url','signature-verification-failed',
            'download-verification-failed','insufficient-space','process-or-lock','data-changed',
            'environment-failed','environment-verification-failed','trial-port-unavailable',
            'trial-exited','trial-timeout','trial-stop-unconfirmed','verification-failed')
        $code = 'child-failed'
        if ($result.PSObject.Properties.Name -contains 'error_code' -and [string]$result.error_code -in $safeCodes) {
            $code = [string]$result.error_code
        }
        throw ('[legacy-bootstrap-' + $code + ']')
    }
    if ([string]$result.state -ne 'complete') {
        if ([string]$result.state -eq 'blocked') { throw '[legacy-bootstrap-child-blocked]' }
        throw '[legacy-bootstrap-child-failed]'
    }
    if ([string]::IsNullOrWhiteSpace([string]$result.target_version)) {
        throw '[legacy-bootstrap-output-invalid]'
    }
    return $result
}

function Remove-LegacyBootstrapTools {
    param([Parameter(Mandatory = $true)][string]$ToolsRoot)

    try {
        if (Test-Path -LiteralPath $ToolsRoot -PathType Container) {
            Remove-Item -LiteralPath $ToolsRoot -Recurse -Force -ErrorAction Stop
        }
        return $true
    }
    catch { return $false }
}

function Assert-LegacyBootstrapRecoveryTarget {
    param([Parameter(Mandatory = $true)][string]$Root)

    $rootFull = Get-LegacyBootstrapFullPath $Root
    Assert-LegacyBootstrapSafeAncestors $rootFull | Out-Null
    if (Test-Path -LiteralPath $rootFull) {
        try { Assert-LegacyBootstrapDirectory $rootFull | Out-Null }
        catch { throw '[legacy-bootstrap-recovery-root-unsafe]' }
    }
    $parentInfo = [IO.Directory]::GetParent($rootFull)
    if ($null -eq $parentInfo) { throw '[legacy-bootstrap-recovery-root-unsafe]' }
    $parent = Assert-LegacyBootstrapDirectory ([string]$parentInfo)
    $rootName = [IO.Path]::GetFileName($rootFull.TrimEnd([char[]]@('\', '/')))
    if ([string]::IsNullOrWhiteSpace($rootName)) {
        throw '[legacy-bootstrap-recovery-root-unsafe]'
    }
    $workspace = Join-Path $parent ('.{0}.nexus-secure-update' -f $rootName)
    Assert-LegacyBootstrapSafeAncestors $workspace | Out-Null
    if (-not (Test-Path -LiteralPath $workspace -PathType Container)) {
        throw '[legacy-bootstrap-recovery-workspace-missing]'
    }
    Assert-LegacyBootstrapDirectory $workspace | Out-Null
    $receipt = Join-Path $workspace 'receipt.json'
    $previous = Join-Path $workspace 'previous'
    if (-not (Test-Path -LiteralPath $receipt -PathType Leaf)) {
        throw '[legacy-bootstrap-recovery-receipt-missing]'
    }
    if (-not (Test-Path -LiteralPath $previous -PathType Container)) {
        throw '[legacy-bootstrap-recovery-previous-missing]'
    }
    Assert-LegacyBootstrapFile $receipt | Out-Null
    Assert-LegacyBootstrapDirectory $previous | Out-Null
    return @{
        Root = $rootFull
        Workspace = $workspace
        Receipt = $receipt
        Previous = $previous
    }
}

function Invoke-LegacySecureBootstrap {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][hashtable]$Source,
        [Parameter(Mandatory = $true)][hashtable]$Target,
        [Parameter(Mandatory = $true)][string]$UvExecutable
    )

    $toolsRoot = $null
    try {
        $recovery = $Target.ContainsKey('Recovery') -and [bool]$Target['Recovery']
        foreach ($name in @('Root', 'App')) {
            if (-not $Source.ContainsKey($name) -or [string]::IsNullOrWhiteSpace([string]$Source[$name])) {
                throw '[legacy-bootstrap-invalid-source]'
            }
        }
        if (-not $Target.ContainsKey('Root') -or [string]::IsNullOrWhiteSpace([string]$Target['Root'])) {
            throw '[legacy-bootstrap-invalid-target]'
        }
        if (-not $Target.ContainsKey('Version') -or [string]::IsNullOrWhiteSpace([string]$Target['Version'])) {
            throw '[legacy-bootstrap-invalid-target]'
        }
        if ($recovery) {
            if ([string]$Target['Version'] -ne 'recovery') {
                throw '[legacy-bootstrap-invalid-target]'
            }
        }
        else {
            foreach ($name in @('App', 'Start')) {
                if (-not $Target.ContainsKey($name) -or [string]::IsNullOrWhiteSpace([string]$Target[$name])) {
                    throw '[legacy-bootstrap-invalid-target]'
                }
            }
        }
        Assert-LegacyBootstrapDirectory ([string]$Source.Root) | Out-Null
        Assert-LegacyBootstrapDirectory ([string]$Source.App) | Out-Null
        if ((Test-LegacyBootstrapSameOrChildPath -Root ([string]$Source.Root) -Child ([string]$Target.Root)) -or
            (Test-LegacyBootstrapSameOrChildPath -Root ([string]$Target.Root) -Child ([string]$Source.Root))) {
            throw '[legacy-bootstrap-source-target-overlap]'
        }
        if (-not (Test-LegacyBootstrapChildPath -Root ([string]$Source.Root) -Child ([string]$Source.App))) {
            throw '[legacy-bootstrap-source-boundary]'
        }
        if ($recovery) {
            Assert-LegacyBootstrapRecoveryTarget -Root ([string]$Target.Root) | Out-Null
        }
        else {
            Assert-LegacyBootstrapDirectory ([string]$Target.Root) | Out-Null
            Assert-LegacyBootstrapDirectory ([string]$Target.App) | Out-Null
            if (-not (Test-LegacyBootstrapChildPath -Root ([string]$Target.Root) -Child ([string]$Target.App))) {
                throw '[legacy-bootstrap-target-boundary]'
            }
            if (-not (Test-LegacyBootstrapChildPath -Root ([string]$Target.Root) -Child ([string]$Target.Start))) {
                throw '[legacy-bootstrap-target-boundary]'
            }
            Assert-LegacyBootstrapFile ([string]$Target.Start) | Out-Null
        }
        $uvFull = Assert-LegacyBootstrapFile $UvExecutable
        $sourceVersionFile = Assert-LegacyBootstrapFile (Join-Path ([string]$Source.App) 'version.json')
        $sourceVersionData = Get-Content -LiteralPath $sourceVersionFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $pythonMinimum = '3.12.16'
        if ($sourceVersionData.PSObject.Properties.Name -contains 'min_python_version') {
            $pythonMinimum = [string]$sourceVersionData.min_python_version
        }
        if ($pythonMinimum -notmatch '^3[.]12[.][0-9]+$' -or [version]$pythonMinimum -lt [version]'3.12.16') { throw '[legacy-bootstrap-python-version]' }
        $pythonRequest = 'cpython@' + $pythonMinimum
        $sourceScript = Assert-LegacyBootstrapFile (Join-Path ([string]$Source.App) 'release\legacy_secure_update.py')
        $requirements = Assert-LegacyBootstrapFile (Join-Path ([string]$Source.App) 'release\legacy_bootstrap_requirements.txt')
        $pythonDownloadsCatalog = Get-LegacyBootstrapPythonDownloadsCatalog -SourceApp ([string]$Source.App)
        $targetRootFull = Get-LegacyBootstrapFullPath ([string]$Target.Root)
        $targetParentInfo = [IO.Directory]::GetParent($targetRootFull)
        if ($null -eq $targetParentInfo) { throw '[legacy-bootstrap-target-boundary]' }
        $targetParent = Assert-LegacyBootstrapDirectory ([string]$targetParentInfo)
        $drive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($targetParent))
        if ($drive.AvailableFreeSpace -lt 268435456) { throw '[legacy-bootstrap-insufficient-space]' }
        $toolsRoot = Join-Path $targetParent ('.nexus-bootstrap-tools-{0}' -f ([guid]::NewGuid().ToString('N')))
        Assert-LegacyBootstrapSafeAncestors $toolsRoot | Out-Null
        if (Test-Path -LiteralPath $toolsRoot) { throw '[legacy-bootstrap-tools-exists]' }
        New-Item -ItemType Directory -Path $toolsRoot -ErrorAction Stop | Out-Null
        Assert-LegacyBootstrapDirectory $toolsRoot | Out-Null
        [IO.File]::WriteAllText((Join-Path $toolsRoot 'empty.netrc'), '', (New-Object Text.UTF8Encoding($false)))
        New-Item -ItemType Directory -Path (Join-Path $toolsRoot 'uv-cache') -Force -ErrorAction Stop | Out-Null
        $environment = Get-LegacyBootstrapEnvironment -UvExecutable $uvFull -ToolsRoot $toolsRoot
        $environment['UV_PYTHON_DOWNLOADS_JSON_URL'] = $pythonDownloadsCatalog
        if ($environment.ContainsKey('UV_PYTHON_INSTALL_DIR')) {
            $managedRoot = [string]$environment['UV_PYTHON_INSTALL_DIR']
            if ((Test-LegacyBootstrapSameOrChildPath -Root ([string]$Source.Root) -Child $managedRoot) -or
                (Test-LegacyBootstrapSameOrChildPath -Root ([string]$Target.Root) -Child $managedRoot)) {
                throw '[legacy-bootstrap-python-store-boundary]'
            }
        }
        $version = Get-LegacyBootstrapUvVersion -UvExecutable $uvFull -WorkingDirectory ([string]$Source.App) -Environment $environment
        Set-LegacyBootstrapStatus -Text '署名済み専用ランタイムを準備しています…' -Phase 'runtime'
        $venv = Join-Path $toolsRoot 'venv'
        Invoke-LegacyBootstrapUv -UvExecutable $uvFull -WorkingDirectory ([string]$Source.App) -Environment $environment -FailureCode 'legacy-bootstrap-venv-failed' -ArgumentList @(
            'venv', '--no-config', '--managed-python', '--python', $pythonRequest, $venv
        )
        $python = Join-Path $venv 'Scripts\python.exe'
        Assert-LegacyBootstrapFile $python | Out-Null
        Set-LegacyBootstrapStatus -Text '新版のPython依存環境を準備しています…' -Phase 'environment'
        Invoke-LegacyBootstrapUv -UvExecutable $uvFull -WorkingDirectory ([string]$Source.App) -Environment $environment -FailureCode 'legacy-bootstrap-dependencies-failed' -ArgumentList @(
            'pip', 'install', '--no-config', '--python', $python, '--require-hashes', '--only-binary', ':all:', '--no-deps', '--index-url', 'https://pypi.org/simple', '-r', $requirements
        )
        Set-LegacyBootstrapStatus -Text '旧版を起動せず、安全な更新処理を実行しています…' -Phase 'download'
        $result = Invoke-LegacyBootstrapUpdateProcess -PythonExecutable $python -ScriptPath $sourceScript -Source $Source -Target $Target -UvExecutable $uvFull -Environment $environment
        $cleaned = Remove-LegacyBootstrapTools -ToolsRoot $toolsRoot
        if (-not $cleaned) {
            $result | Add-Member -NotePropertyName recovery_state -NotePropertyValue 'cleanup-pending' -Force
        }
        return $result
    }
    catch {
        $message = [string]$_.Exception.Message
        if ($message -match '^\[[a-z0-9][a-z0-9_-]{1,63}\]$') {
            throw $message
        }
        throw '[legacy-bootstrap-failed]'
    }
}
