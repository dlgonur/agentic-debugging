<#
.SYNOPSIS
    Idempotent Windows helper to install Agentic Debugger and ensure global CLI alias availability.

.DESCRIPTION
    Installs Agentic Debugger with [app] dependencies and ensures Python's
    Scripts directory is on the User PATH so 'agenticdebugger' and
    'agentic-debugger' can be run globally from any PowerShell or CMD terminal.

.PARAMETER Uninstall
    Uninstalls the package and reverses User PATH modifications if present.

.PARAMETER Python
    Python command or path to use for installation (default: 'python').
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

# Verify Python existence and version
try {
    $pythonVersion = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
} catch {
    Write-Error "Python executable '$Python' was not found or failed to execute."
    exit 1
}

$versionParts = $pythonVersion.Split('.')
$major = [int]$versionParts[0]
$minor = [int]$versionParts[1]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
    Write-Error "Agentic Debugger requires Python 3.11+, found $pythonVersion."
    exit 1
}

# Resolve repository root dynamically without hardcoded machine paths
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

# Resolve Python's scripts directory
$scriptsDir = & $Python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
if (-not $scriptsDir -or -not (Test-Path $scriptsDir)) {
    Write-Error "Failed to locate Python scripts directory ($scriptsDir)."
    exit 1
}

if ($Uninstall) {
    Write-Host "Uninstalling agentic-debugger..."
    & $Python -m pip uninstall -y agentic-debugger

    # Reverse User PATH change if scripts directory was registered in User PATH
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath) {
        $userEntries = $userPath.Split(';', [System.StringSplitOptions]::RemoveEmptyEntries)
        $filtered = @()
        $found = $false
        foreach ($entry in $userEntries) {
            if ($entry.Trim().TrimEnd('\') -ieq $scriptsDir.Trim().TrimEnd('\')) {
                $found = $true
            } else {
                $filtered += $entry
            }
        }
        if ($found) {
            $newUserPath = [string]::Join(';', $filtered)
            [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
            Write-Host "Removed '$scriptsDir' from User PATH."
        }
    }
    Write-Host "Agentic Debugger uninstall complete."
    exit 0
}

# Check for unrelated existing agenticdebugger executable
$existing = Get-Command "agenticdebugger" -ErrorAction SilentlyContinue
if ($existing) {
    $existingDir = Split-Path $existing.Source -Parent
    if ($existingDir.TrimEnd('\') -ine $scriptsDir.TrimEnd('\')) {
        Write-Error "An unrelated executable 'agenticdebugger' already exists at '$($existing.Source)'. Aborting installation to prevent collision."
        exit 1
    }
}

# Install the package in editable mode with Textual app extra
Write-Host "Installing Agentic Debugger from '$repoRoot'..."
& $Python -m pip install -e "$repoRoot[app]"
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip installation failed with exit code $LASTEXITCODE."
    exit $LASTEXITCODE
}

# Verify generated Windows launcher
$aliasExe = Join-Path $scriptsDir "agenticdebugger.exe"
if (-not (Test-Path $aliasExe)) {
    Write-Error "Expected launcher '$aliasExe' was not created by pip installation."
    exit 1
}

# Minimal, non-admin User PATH management (HKCU\Environment)
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$userEntries = @()
if ($userPath) {
    $userEntries = $userPath.Split(';', [System.StringSplitOptions]::RemoveEmptyEntries)
}

$alreadyInUserPath = $false
foreach ($entry in $userEntries) {
    if ($entry.Trim().TrimEnd('\') -ieq $scriptsDir.Trim().TrimEnd('\')) {
        $alreadyInUserPath = $true
        break
    }
}

$alreadyInProcessPath = $false
$processEntries = $env:PATH.Split(';', [System.StringSplitOptions]::RemoveEmptyEntries)
foreach ($entry in $processEntries) {
    if ($entry.Trim().TrimEnd('\') -ieq $scriptsDir.Trim().TrimEnd('\')) {
        $alreadyInProcessPath = $true
        break
    }
}

if (-not $alreadyInUserPath) {
    $newEntries = $userEntries + $scriptsDir
    $newUserPath = [string]::Join(';', $newEntries)
    [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
    Write-Host "Added '$scriptsDir' to User PATH."
    Write-Host "Note: Newly opened PowerShell and CMD terminals will recognize 'agenticdebugger' globally."
    Write-Host "Restart open terminal windows to pick up the updated User PATH."
} else {
    Write-Host "Python Scripts directory '$scriptsDir' is already configured in User PATH."
}

if (-not $alreadyInProcessPath) {
    $env:PATH = "$env:PATH;$scriptsDir"
}

Write-Host "Success! 'agenticdebugger' and 'agentic-debugger' are installed and available."
