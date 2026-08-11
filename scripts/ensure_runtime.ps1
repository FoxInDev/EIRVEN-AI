$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$admin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    $args = @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + $MyInvocation.MyCommand.Path + '"'))
    try {
        $proc = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $args -WorkingDirectory $Root -Wait -PassThru
        exit $proc.ExitCode
    } catch {
        throw 'EIRVEN full-access mode requires approval of the standard Windows UAC prompt.'
    }
}

Set-Location -LiteralPath $Root
$env:NO_PROXY = "127.0.0.1,localhost,::1"
$env:no_proxy = "127.0.0.1,localhost,::1"

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user;$env:LOCALAPPDATA\Programs\Ollama;$env:USERPROFILE\.local\bin;$env:APPDATA\npm"
}

function Invoke-EirvenRetry {
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][scriptblock]$Action,
        [int]$Attempts = 3
    )
    $last = $null
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            & $Action
            return
        } catch {
            $last = $_
            if ($i -ge $Attempts) { break }
            $delay = if ($i -eq 1) { 3 } else { 8 }
            Write-Host "$Name failed temporarily. Retrying automatically in $delay sec ($($i+1)/$Attempts)..." -ForegroundColor Yellow
            Start-Sleep -Seconds $delay
            Refresh-Path
        }
    }
    throw $last
}

function Get-Python312Exe {
    # Prefer the Python launcher when it already knows about Python 3.12.
    try {
        $resolved = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null | Select-Object -Last 1)
        if ($LASTEXITCODE -eq 0 -and $resolved -and (Test-Path -LiteralPath $resolved.Trim())) {
            return $resolved.Trim()
        }
    } catch {}

    # Also support systems where Python exists but py.exe/the launcher is missing.
    $candidates = @(
        "$env:ProgramFiles\Python312\python.exe",
        "${env:ProgramFiles(x86)}\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    )
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) { continue }
        try {
            $version = (& $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null | Select-Object -Last 1)
            if ($LASTEXITCODE -eq 0 -and $version.Trim() -eq '3.12') { return $candidate }
        } catch {}
    }

    try {
        $cmd = Get-Command python.exe -ErrorAction Stop
        $candidate = $cmd.Source
        $version = (& $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null | Select-Object -Last 1)
        if ($LASTEXITCODE -eq 0 -and $version.Trim() -eq '3.12') { return $candidate }
    } catch {}

    return $null
}

function Install-Python312Direct {
    # winget can be present while its source database is broken (0x8a15000f).
    # Use the official CPython installer directly so EIRVEN startup does not depend on winget.
    $pythonVersion = '3.12.10'
    $url = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-amd64.exe"
    $installer = Join-Path $env:TEMP "eirven-python-$pythonVersion-amd64.exe"

    Write-Host "Python 3.12 not found. Downloading the official Python installer..." -ForegroundColor Cyan
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    } catch {}

    try {
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $installer -TimeoutSec 300
        if (-not (Test-Path -LiteralPath $installer) -or (Get-Item -LiteralPath $installer).Length -lt 10000000) {
            throw 'The downloaded Python installer is incomplete.'
        }

        $sig = Get-AuthenticodeSignature -FilePath $installer
        if ($sig.Status -ne 'Valid' -or -not $sig.SignerCertificate -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
            throw "The Python installer signature could not be trusted ($($sig.Status))."
        }

        $proc = Start-Process -FilePath $installer -ArgumentList @(
            '/quiet',
            'InstallAllUsers=1',
            'PrependPath=1',
            'Include_launcher=1',
            'Include_test=0',
            'Shortcuts=0'
        ) -Wait -PassThru
        if ($proc.ExitCode -ne 0) { throw "Official Python installer exited with code $($proc.ExitCode)." }
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }

    Refresh-Path
}

$PythonExe = Get-Python312Exe
if (-not $PythonExe) {
    $directError = $null
    try {
        Invoke-EirvenRetry -Name "Python 3.12 direct installation" -Attempts 2 -Action {
            Install-Python312Direct
            $script:PythonExe = Get-Python312Exe
            if (-not $script:PythonExe) { throw 'Python 3.12 installed but is not ready yet.' }
        }
    } catch {
        $directError = $_
    }

    # Last-resort fallback: repair winget sources and use winget only if the direct
    # python.org installer could not be obtained on this network.
    if (-not $PythonExe -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        try {
            Write-Host "Direct Python installation was unavailable. Repairing winget sources..." -ForegroundColor Yellow
            & winget source reset --force | Out-Host
            & winget source update | Out-Host
            & winget install --id Python.Python.3.12 --exact --accept-source-agreements --accept-package-agreements --disable-interactivity | Out-Host
            Refresh-Path
            $PythonExe = Get-Python312Exe
        } catch {}
    }

    if (-not $PythonExe) {
        if ($directError) { throw "Could not install Python 3.12 automatically. $($directError.Exception.Message)" }
        throw 'Could not install Python 3.12 automatically.'
    }
}

& $PythonExe --version | Out-Host

$ollamaOk = $false
try { & ollama --version | Out-Host; if ($LASTEXITCODE -eq 0) { $ollamaOk = $true } } catch {}
if (-not $ollamaOk) {
    Invoke-EirvenRetry -Name "Ollama installation" -Action {
        Write-Host "Installing Ollama..."
        Invoke-RestMethod https://ollama.com/install.ps1 | Invoke-Expression
        Refresh-Path
        & ollama --version | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "Ollama installation did not finish correctly yet." }
    }
} else {
    try {
        $versionText = (& ollama --version 2>&1 | Out-String)
        if ($versionText -match '(\d+\.\d+\.\d+)') {
            $currentOllama = [version]$Matches[1]
            if ($currentOllama -lt [version]'0.32.3') {
                Invoke-EirvenRetry -Name "Ollama update" -Attempts 3 -Action {
                    Write-Host "Updating Ollama to a current build (required for reliable large-model pulls)..." -ForegroundColor Cyan
                    Invoke-RestMethod https://ollama.com/install.ps1 | Invoke-Expression
                    Refresh-Path
                    $updatedText = (& ollama --version 2>&1 | Out-String)
                    Write-Host $updatedText.Trim()
                    if ($updatedText -match '(\d+\.\d+\.\d+)' -and [version]$Matches[1] -lt [version]'0.32.3') {
                        throw "Ollama update did not reach the minimum supported build 0.32.3."
                    }
                }
            }
        }
    } catch {
        Write-Host "Could not auto-update Ollama; continuing with the installed runtime." -ForegroundColor Yellow
    }
}

Refresh-Path
$claudeOk = $false
try { & claude --version | Out-Host; if ($LASTEXITCODE -eq 0) { $claudeOk = $true } } catch {}
if (-not $claudeOk) {
    Invoke-EirvenRetry -Name "Claude Code installation" -Attempts 3 -Action {
        Write-Host "Installing the official Claude Code CLI for the local Ollama backend..." -ForegroundColor Cyan
        Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
        Refresh-Path
        & claude --version | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "Claude Code CLI installation did not finish correctly yet." }
    }
}

# Pillow is the only tiny dependency needed before the main venv exists: it lets the
# first installer frame render the supplied EIRVEN artwork with high-quality scaling.
Invoke-EirvenRetry -Name "Installer visual dependency" -Action {
    & $PythonExe -m pip install --disable-pip-version-check --quiet "Pillow>=11,<13"
    if ($LASTEXITCODE -ne 0) { throw "Could not prepare the installer visual layer." }
}

# bootstrap_r27.py owns the visible installation window and retries a failed installation
# attempt automatically. Already downloaded models/venv files are intentionally preserved.
& $PythonExe "$Root\scripts\bootstrap_r27.py"
if ($LASTEXITCODE -ne 0) { throw "EIRVEN bootstrap failed after automatic recovery attempts" }
