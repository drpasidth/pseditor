# PSEditor installer for another Windows PC.
# Double-click install.bat (do not run this script by itself).
#Requires -Version 5.1
$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

try { chcp 65001 | Out-Null } catch {}
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$LogPath = Join-Path $Root "install.log"
try { Start-Transcript -Path $LogPath -Force | Out-Null } catch {}

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-PythonExe([string]$Exe) {
    if ([string]::IsNullOrWhiteSpace($Exe)) { return $false }
    if ($Exe -match "WindowsApps") { return $false }
    if (-not (Test-Path -LiteralPath $Exe)) { return $false }
    & $Exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
    return ($LASTEXITCODE -eq 0)
}

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[string]

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $raw = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $raw) {
            $line = @($raw) | Where-Object { $_ -and "$_".Trim() } | Select-Object -Last 1
            if ($line) { $candidates.Add(("$line").Trim()) }
        }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python -and $python.Source) { $candidates.Add($python.Source) }

    foreach ($ver in @("313", "312", "311", "310")) {
        $candidates.Add((Join-Path $env:LocalAppData "Programs\Python\Python$ver\python.exe"))
        $candidates.Add((Join-Path $env:ProgramFiles "Python$ver\python.exe"))
        $candidates.Add((Join-Path ${env:ProgramFiles(x86)} "Python$ver\python.exe"))
        $candidates.Add("C:\Python$ver\python.exe")
    }

    foreach ($candidate in $candidates) {
        if (Test-PythonExe $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Invoke-Native([scriptblock]$Command, [string]$What) {
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit code $LASTEXITCODE)."
    }
}

try {
    Write-Host ""
    Write-Host "PSEditor installer" -ForegroundColor Green
    Write-Host "Folder: $Root"

    foreach ($required in @("manage.py", "requirements.txt", "run.bat")) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $required))) {
            throw "Missing $required. Unzip the full PSEditor folder, then run install.bat from inside it."
        }
    }

    Write-Step "Looking for Python 3.10 or newer"
    $Python = Find-Python
    if (-not $Python) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if ($winget) {
            $answer = Read-Host "Python 3.10+ was not found. Install Python 3.13 with winget now? [Y/N]"
            if ($answer -match "^[Yy]") {
                Write-Step "Installing Python 3.13 (user scope)"
                & winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
                if ($LASTEXITCODE -ne 0) {
                    throw "winget could not install Python. Download it from https://www.python.org/downloads/windows/ and tick 'Add python.exe to PATH', then run install.bat again."
                }
                $Python = Find-Python
            }
        }
    }
    if (-not $Python) {
        throw "Python 3.10 or newer is required. Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then run install.bat again."
    }
    $versionLine = & $Python -c "import sys; print(sys.version.split()[0])"
    Write-Host "Using Python $versionLine"
    Write-Host $Python

    $VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
    $VenvDir = Join-Path $Root ".venv"
    $recreate = $true
    if (Test-Path -LiteralPath $VenvPython) {
        if (Test-PythonExe $VenvPython) { $recreate = $false }
    }
    if ($recreate) {
        Write-Step "Creating virtual environment"
        if (Test-Path -LiteralPath $VenvDir) {
            Remove-Item -LiteralPath $VenvDir -Recurse -Force
        }
        Invoke-Native { & $Python -m venv $VenvDir } "Creating the virtual environment"
    } else {
        Write-Step "Reusing the existing virtual environment"
    }

    Write-Step "Installing Python packages"
    $Requirements = Join-Path $Root "requirements.txt"
    $Wheels = Join-Path $Root "wheels"
    $installed = $false
    if (Get-ChildItem -LiteralPath $Wheels -Filter *.whl -ErrorAction SilentlyContinue) {
        Write-Host "Installing offline from $Wheels"
        & $VenvPython -m pip install --no-index --find-links $Wheels -r $Requirements
        if ($LASTEXITCODE -eq 0) {
            $installed = $true
        } else {
            Write-Host "The bundled packages do not cover this Python version. Trying the internet..." -ForegroundColor Yellow
        }
    }
    if (-not $installed) {
        & $VenvPython -m pip install -r $Requirements
        if ($LASTEXITCODE -ne 0) {
            throw ("Could not download the Python packages. This PC cannot reach pypi.org (no internet, DNS, or a proxy is required). " +
                "Fix: on a PC with internet run package.bat so the zip includes the 'wheels' folder, then copy that zip here. " +
                "Behind a proxy, set HTTPS_PROXY=http://proxy:port before running install.bat.")
        }
    }
    Invoke-Native { & $VenvPython -c "import django, requests, winpty, webview" } "Checking Django, requests, pywinpty, and pywebview"

    Write-Step "Preparing the database"
    Invoke-Native { & $VenvPython (Join-Path $Root "manage.py") migrate --noinput } "Database migration"

    $AgentQuery = @"
import os, sys, django
sys.path.insert(0, os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pseditor.settings')
django.setup()
from editor.models import Agent
a = Agent.objects.filter(is_default=True).first() or Agent.objects.first()
key = os.environ.get('PSEDITOR_OPENROUTER_KEY', '').strip()
if a is None or 'openrouter.ai' not in a.base_url:
    print('skip')
elif key:
    a.api_key = key
    a.save(update_fields=['api_key'])
    print('saved')
else:
    print('has-key' if a.api_key else 'no-key')
"@
    $env:PSEDITOR_OPENROUTER_KEY = ""
    $state = (& $VenvPython -c $AgentQuery | Select-Object -Last 1)
    if ($state -eq "no-key") {
        Write-Step "OpenRouter agent (default)"
        Write-Host "Paste your OpenRouter API key (https://openrouter.ai/keys), or press Enter to add it later in Agents."
        $secure = Read-Host "OpenRouter API key" -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        if ($plain -and $plain.Trim()) {
            $env:PSEDITOR_OPENROUTER_KEY = $plain.Trim()
            $state = (& $VenvPython -c $AgentQuery | Select-Object -Last 1)
            $env:PSEDITOR_OPENROUTER_KEY = ""
            if ($state -eq "saved") { Write-Host "API key saved." } else { Write-Host "Could not save the key; add it later in Agents." -ForegroundColor Yellow }
        } else {
            Write-Host "Skipped. Add the key later: click Agents > OpenRouter > API key > Save."
        }
        $plain = $null
    }

    Write-Step "Creating shortcuts"
    $shell = New-Object -ComObject WScript.Shell
    $icon = Join-Path $Root "pseditor.ico"
    foreach ($place in @("Desktop", "Programs")) {
        $dir = [Environment]::GetFolderPath($place)
        if (-not $dir) { continue }
        $linkPath = Join-Path $dir "PSEditor.lnk"
        $shortcut = $shell.CreateShortcut($linkPath)
        $shortcut.TargetPath = Join-Path $Root "run.bat"
        $shortcut.WorkingDirectory = $Root
        $shortcut.WindowStyle = 1
        $shortcut.Description = "PSEditor"
        if (Test-Path -LiteralPath $icon) { $shortcut.IconLocation = $icon }
        $shortcut.Save()
        Write-Host "Shortcut: $linkPath"
    }

    Write-Host ""
    Write-Host "PSEditor is installed." -ForegroundColor Green
    Write-Host "Start it with the PSEditor shortcut or by double-clicking run.bat."
    Write-Host "It opens in its own window (no web browser needed). Use run_browser.bat for the browser version."
    Write-Host "The first launch needs internet so Monaco and xterm.js can load from the CDN."
    Write-Host ""
    $start = Read-Host "Start PSEditor now? [Y/N]"
    if ($start -match "^[Yy]") {
        Start-Process -FilePath (Join-Path $Root "run.bat") -WorkingDirectory $Root
    }
    exit 0
}
catch {
    Write-Host ""
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "Details were saved to $LogPath"
    Read-Host "Press Enter to close"
    exit 1
}
finally {
    try { Stop-Transcript | Out-Null } catch {}
}
