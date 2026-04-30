param(
    [string]$InstallDir = $(Join-Path $env:LOCALAPPDATA "TwitchFreedom"),
    [string]$RepoOwner = "ornab74",
    [string]$RepoName = "twitchfreedom",
    [string]$RepoRef = "main",
    [switch]$NoShortcut,
    [switch]$NoRun,
    [string]$SourceDir = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Write-Log {
    param([string]$Message)
    Write-Host "[TwitchFreedom] $Message"
}

function Throw-InstallError {
    param([string]$Message)
    throw "[TwitchFreedom] $Message"
}

function Refresh-Path {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Test-Command {
    param([string]$Command)
    return [bool](Get-Command $Command -ErrorAction SilentlyContinue)
}

function Install-WingetPackage {
    param(
        [string]$Command,
        [string]$PackageId
    )

    if (Test-Command $Command) {
        return
    }

    if (-not (Test-Command "winget")) {
        Throw-InstallError "Missing $Command and winget is not available. Install $PackageId manually, then rerun this script."
    }

    Write-Log "Installing $PackageId with winget"
    winget install -e --id $PackageId --accept-source-agreements --accept-package-agreements
    Refresh-Path
}

function Get-PythonExecutable {
    if (Test-Command "py") {
        try {
            & py -3 --version *> $null
            return "py -3"
        } catch {
        }
    }

    foreach ($candidate in @("python", "python3")) {
        if (Test-Command $candidate) {
            try {
                & $candidate --version *> $null
                return $candidate
            } catch {
            }
        }
    }

    $knownPaths = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:ProgramFiles\Python311\python.exe"
    )
    foreach ($candidate in $knownPaths) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    return ""
}

function Invoke-Python {
    param(
        [string]$PythonCommand,
        [string[]]$Arguments
    )

    if ($PythonCommand -eq "py -3") {
        & py -3 @Arguments
    } else {
        & $PythonCommand @Arguments
    }
}

function Ensure-SystemDependencies {
    if (-not (Get-PythonExecutable)) {
        Install-WingetPackage -Command "py" -PackageId "Python.Python.3.12"
    }
    if (-not (Test-Command "ffplay")) {
        Install-WingetPackage -Command "ffplay" -PackageId "Gyan.FFmpeg"
    }
    Refresh-Path

    if (-not (Get-PythonExecutable)) {
        Throw-InstallError "Python 3 was not found after installation."
    }
    if (-not (Test-Command "ffplay")) {
        Write-Log "ffplay was not found on PATH. FFmpeg may need a new terminal session after winget finishes."
    }
}

function Get-LocalSource {
    if ($SourceDir -and (Test-Path (Join-Path $SourceDir "main.py"))) {
        return (Resolve-Path $SourceDir).Path
    }

    if ($PSScriptRoot) {
        $candidate = Resolve-Path (Join-Path $PSScriptRoot "..") -ErrorAction SilentlyContinue
        if ($candidate -and (Test-Path (Join-Path $candidate.Path "main.py")) -and (Test-Path (Join-Path $candidate.Path "requirements.txt"))) {
            return $candidate.Path
        }
    }

    return ""
}

function Copy-SourceTree {
    param([string]$Source)

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $excludeNames = @(".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".codex")
    $excludePatterns = @("*.sqlite", "*.sqlite3", "*.db")

    foreach ($item in Get-ChildItem -LiteralPath $Source -Force) {
        $skip = $excludeNames -contains $item.Name
        foreach ($pattern in $excludePatterns) {
            if ($item.Name -like $pattern) {
                $skip = $true
            }
        }
        if ($skip) {
            continue
        }

        $target = Join-Path $InstallDir $item.Name
        Copy-Item -LiteralPath $item.FullName -Destination $target -Recurse -Force
    }
}

function Install-Source {
    $localSource = Get-LocalSource
    if ($localSource) {
        Write-Log "Installing from local checkout: $localSource"
        Copy-SourceTree -Source $localSource
        return
    }

    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("twitchfreedom-" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
    try {
        $zipPath = Join-Path $tempRoot "source.zip"
        $zipUrl = "https://github.com/$RepoOwner/$RepoName/archive/refs/heads/$RepoRef.zip"
        Write-Log "Downloading $zipUrl"
        Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath
        Expand-Archive -LiteralPath $zipPath -DestinationPath $tempRoot -Force
        $extracted = Get-ChildItem -LiteralPath $tempRoot -Directory | Where-Object { $_.Name -like "$RepoName-*" } | Select-Object -First 1
        if (-not $extracted) {
            Throw-InstallError "Could not find extracted source directory."
        }
        Copy-SourceTree -Source $extracted.FullName
    } finally {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Install-PythonDependencies {
    $python = Get-PythonExecutable
    if (-not $python) {
        Throw-InstallError "Python 3 is required."
    }

    $venvDir = Join-Path $InstallDir ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    $requirements = Join-Path $InstallDir "requirements.txt"
    if (-not (Test-Path $requirements)) {
        Throw-InstallError "Missing requirements.txt in $InstallDir"
    }

    if (-not (Test-Path $venvPython)) {
        Write-Log "Creating virtual environment"
        Invoke-Python -PythonCommand $python -Arguments @("-m", "venv", $venvDir)
    }

    Write-Log "Installing Python requirements"
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install --upgrade -r $requirements
}

function Quote-PowerShellString {
    param([string]$Value)
    return "'" + ($Value -replace "'", "''") + "'"
}

function Write-Launchers {
    $runnerPs1 = Join-Path $InstallDir "Start-TwitchFreedom.ps1"
    $runnerCmd = Join-Path $InstallDir "TwitchFreedom.cmd"
    $venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
    $mainPy = Join-Path $InstallDir "main.py"
    $quotedInstall = Quote-PowerShellString $InstallDir
    $quotedPython = Quote-PowerShellString $venvPython
    $quotedMain = Quote-PowerShellString $mainPy

    @"
`$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $quotedInstall
& $quotedPython $quotedMain
"@ | Set-Content -LiteralPath $runnerPs1 -Encoding UTF8

    @"
@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-TwitchFreedom.ps1"
"@ | Set-Content -LiteralPath $runnerCmd -Encoding ASCII

    Write-Log "Command launcher: $runnerCmd"
}

function New-Shortcut {
    param(
        [string]$Path,
        [string]$TargetPath,
        [string]$WorkingDirectory
    )

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $TargetPath
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.Description = "Minimal Twitch GUI without browser bloat"
    $shortcut.Save()
}

function Write-Shortcuts {
    if ($NoShortcut) {
        return
    }

    $runnerCmd = Join-Path $InstallDir "TwitchFreedom.cmd"
    $desktop = [Environment]::GetFolderPath("DesktopDirectory")
    $programs = [Environment]::GetFolderPath("Programs")
    $desktopShortcut = Join-Path $desktop "TwitchFreedom.lnk"
    $startShortcut = Join-Path $programs "TwitchFreedom.lnk"

    New-Item -ItemType Directory -Force -Path $programs | Out-Null
    New-Shortcut -Path $desktopShortcut -TargetPath $runnerCmd -WorkingDirectory $InstallDir
    New-Shortcut -Path $startShortcut -TargetPath $runnerCmd -WorkingDirectory $InstallDir
    Write-Log "Desktop shortcut: $desktopShortcut"
    Write-Log "Start Menu shortcut: $startShortcut"
}

function Main {
    Ensure-SystemDependencies
    Install-Source
    Install-PythonDependencies
    Write-Launchers
    Write-Shortcuts
    Write-Log "Installed to $InstallDir"

    if (-not $NoRun) {
        Start-Process -FilePath (Join-Path $InstallDir "TwitchFreedom.cmd")
        Write-Log "Started Twitch Freedom"
    }
}

Main
