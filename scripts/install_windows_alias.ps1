<#
.SYNOPSIS
    Idempotent Windows installer for Agentic Debugger app-owned global CLI launcher.

.DESCRIPTION
    Creates an application-owned isolated virtual environment under
    <InstallRoot>\AgenticDebugger\cli-venv (default: %LOCALAPPDATA%\AgenticDebugger\cli-venv),
    installs Agentic Debugger with [app] dependencies in editable mode,
    creates an ownership marker (.agentic-debugger-managed), and registers only
    its app-owned Scripts directory in User PATH so 'agenticdebugger' and
    'agentic-debugger' can be run globally from any PowerShell or CMD terminal.

.PARAMETER Uninstall
    Removes the app-owned virtual environment (verifying ownership marker before
    destructive deletion) and deletes only the app-owned launcher directory from
    User PATH, preserving all pre-existing PATH entries.

.PARAMETER Python
    Base Python executable used to create the app-owned environment (default: 'python').

.PARAMETER InstallRoot
    Optional root directory override under which AgenticDebugger\cli-venv will be
    managed. Defaults to %LOCALAPPDATA% (or %USERPROFILE%\AppData\Local).
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [string]$Python = "python",
    [string]$InstallRoot = ""
)

$ErrorActionPreference = "Stop"

# Resolve effective root and managed app-owned environment paths
$effectiveRoot = if ($InstallRoot) {
    $InstallRoot
} else {
    if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $env:USERPROFILE "AppData\Local" }
}

$appDir = Join-Path $effectiveRoot "AgenticDebugger"
$cliVenv = Join-Path $appDir "cli-venv"
$launcherDir = Join-Path $cliVenv "Scripts"
$targetUnhyphenated = Join-Path $launcherDir "agenticdebugger.exe"
$targetHyphenated = Join-Path $launcherDir "agentic-debugger.exe"
$markerFile = Join-Path $cliVenv ".agentic-debugger-managed"

# Helper to verify Agentic Debugger ownership of an existing directory
function Test-AppVenvOwnership {
    param([string]$VenvPath, [string]$Marker)
    if (-not (Test-Path $VenvPath)) {
        return $false
    }
    if (-not (Test-Path $Marker)) {
        return $false
    }
    $markerContent = Get-Content -Path $Marker -Raw -ErrorAction SilentlyContinue
    return ($markerContent -and $markerContent -match "managed-by=AgenticDebugger")
}

# Dynamically resolve repository root without hardcoded machine paths
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

# -------------------------------------------------------------------------
# UNINSTALL MODE
# -------------------------------------------------------------------------
if ($Uninstall) {
    Write-Host "Uninstalling Agentic Debugger global launcher..."

    # 1. Remove app-owned venv directory with strict ownership verification
    if (Test-Path $cliVenv) {
        if (-not (Test-AppVenvOwnership -VenvPath $cliVenv -Marker $markerFile)) {
            Write-Error "Target directory '$cliVenv' exists but is missing the Agentic Debugger ownership marker ('$markerFile'). Refusing destructive removal to prevent data loss."
            exit 1
        }
        try {
            Remove-Item -Recurse -Force $cliVenv -ErrorAction Stop
            Write-Host "Removed app-owned virtual environment at '$cliVenv'."
        } catch {
            Write-Error "Failed to remove app-owned virtual environment at '$cliVenv': $_"
            exit 1
        }
    } else {
        Write-Host "App-owned virtual environment was not present at '$cliVenv'."
    }

    # 2. Remove ONLY the exact app-owned launcher directory from User PATH
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath) {
        $userEntries = $userPath.Split(';', [System.StringSplitOptions]::RemoveEmptyEntries)
        $filtered = @()
        $found = $false
        foreach ($entry in $userEntries) {
            if ($entry.Trim().TrimEnd('\') -ieq $launcherDir.Trim().TrimEnd('\')) {
                $found = $true
            } else {
                $filtered += $entry
            }
        }
        if ($found) {
            $newUserPath = [string]::Join(';', $filtered)
            try {
                [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
                Write-Host "Removed app-owned launcher directory from User PATH."
            } catch {
                Write-Error "Failed to update User PATH during uninstall: $_"
                exit 1
            }
        }
    }

    # 3. Clean up current process PATH if present
    $processEntries = $env:PATH.Split(';', [System.StringSplitOptions]::RemoveEmptyEntries)
    $filteredProcess = @()
    foreach ($entry in $processEntries) {
        if ($entry.Trim().TrimEnd('\') -ine $launcherDir.Trim().TrimEnd('\')) {
            $filteredProcess += $entry
        }
    }
    $env:PATH = [string]::Join(';', $filteredProcess)

    Write-Host "Agentic Debugger global launcher uninstall complete."
    exit 0
}

# -------------------------------------------------------------------------
# INSTALL MODE
# -------------------------------------------------------------------------

# 1. Verify base Python availability and version (3.11+)
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

# 2. Collision protection: check both alias names against external executables
foreach ($cmdName in @("agenticdebugger", "agentic-debugger")) {
    $existing = Get-Command $cmdName -ErrorAction SilentlyContinue
    if ($existing -and $existing.Source) {
        $existingDir = Split-Path $existing.Source -Parent
        if ($existingDir.TrimEnd('\') -ine $launcherDir.TrimEnd('\')) {
            Write-Error "Command '$cmdName' already resolves to an external location outside the app-owned launcher directory: '$($existing.Source)'. Aborting installation to prevent collision or shadowing."
            exit 1
        }
    }
}

# 3. Create or reuse app-owned virtual environment with ownership enforcement
Write-Host "Setting up app-owned virtual environment at '$cliVenv'..."
if (Test-Path $cliVenv) {
    if (-not (Test-AppVenvOwnership -VenvPath $cliVenv -Marker $markerFile)) {
        Write-Error "Target directory '$cliVenv' already exists but lacks the Agentic Debugger ownership marker ('$markerFile'). Refusing to adopt or overwrite an unmarked directory."
        exit 1
    }
    Write-Host "Existing verified app-owned virtual environment found; reinstalling."
} else {
    New-Item -ItemType Directory -Path $appDir -Force | Out-Null
    try {
        # Isolated standalone virtual environment
        & $Python -m venv $cliVenv
    } catch {
        Write-Error "Failed to create virtual environment at '$cliVenv': $_"
        exit 1
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Error "venv creation failed with exit code $LASTEXITCODE."
        exit $LASTEXITCODE
    }

    # Write non-secret ownership marker
    Set-Content -Path $markerFile -Value "managed-by=AgenticDebugger;version=1.0;created_at=$(Get-Date -Format 'o')" -Encoding utf8
}

$venvPython = Join-Path $launcherDir "python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "Virtual environment python executable was not found at '$venvPython'."
    exit 1
}

# 4. Install Agentic Debugger with [app,runtime] dependencies into app-owned environment
# Editable install links the app-owned launcher to this repository source tree
Write-Host "Installing Agentic Debugger with [app,runtime] dependencies into app-owned environment from '$repoRoot'..."
& $venvPython -m pip install -e "$repoRoot[app,runtime]"
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip installation failed with exit code $LASTEXITCODE."
    exit $LASTEXITCODE
}

# 5. Verification Gate: launchers, Textual/runtime import, and doctor readiness
if (-not (Test-Path $targetUnhyphenated) -or -not (Test-Path $targetHyphenated)) {
    Write-Error "Expected launchers ('agenticdebugger.exe' and 'agentic-debugger.exe') were not found in '$launcherDir' after installation."
    exit 1
}

try {
    & $venvPython -c "import textual, pytest, yaml"
} catch {
    Write-Error "Dependency verification failed in app-owned environment: $_"
    exit 1
}
if ($LASTEXITCODE -ne 0) {
    Write-Error "Dependency verification failed in app-owned environment."
    exit 1
}

$doctorOutput = & $targetUnhyphenated --doctor | Out-String
if ($doctorOutput -notmatch "Status: READY") {
    Write-Error "Launcher doctor readiness check failed:\n$doctorOutput"
    exit 1
}

# 6. Non-admin User PATH management (HKCU\Environment)
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$userEntries = @()
if ($userPath) {
    $userEntries = $userPath.Split(';', [System.StringSplitOptions]::RemoveEmptyEntries)
}

$alreadyInUserPath = $false
foreach ($entry in $userEntries) {
    if ($entry.Trim().TrimEnd('\') -ieq $launcherDir.Trim().TrimEnd('\')) {
        $alreadyInUserPath = $true
        break
    }
}

if (-not $alreadyInUserPath) {
    $newEntries = $userEntries + $launcherDir
    $newUserPath = [string]::Join(';', $newEntries)
    try {
        [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
        Write-Host "Added app-owned launcher directory '$launcherDir' to User PATH."
        Write-Host "Note: Newly opened PowerShell and CMD terminals will recognize 'agenticdebugger' globally."
        Write-Host "Restart open terminal windows to pick up the updated User PATH."
    } catch {
        Write-Error "Failed to register launcher directory in User PATH: $_"
        exit 1
    }
} else {
    Write-Host "App-owned launcher directory '$launcherDir' is already configured in User PATH."
}

# 7. Update current process PATH so command can be exercised immediately
$processEntries = $env:PATH.Split(';', [System.StringSplitOptions]::RemoveEmptyEntries)
$alreadyInProcessPath = $false
foreach ($entry in $processEntries) {
    if ($entry.Trim().TrimEnd('\') -ieq $launcherDir.Trim().TrimEnd('\')) {
        $alreadyInProcessPath = $true
        break
    }
}
if (-not $alreadyInProcessPath) {
    $env:PATH = "$launcherDir;$env:PATH"
}

# 8. Final launch resolution check in current process
$resolvedUnhyphenated = Get-Command "agenticdebugger" -ErrorAction SilentlyContinue
$resolvedHyphenated = Get-Command "agentic-debugger" -ErrorAction SilentlyContinue
if (-not $resolvedUnhyphenated -or -not $resolvedHyphenated) {
    Write-Warning "Launchers installed at '$launcherDir' but could not be resolved in current process session."
} else {
    Write-Host "Success! 'agenticdebugger' and 'agentic-debugger' are installed and available globally."
}
