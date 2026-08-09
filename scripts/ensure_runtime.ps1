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
    $env:Path = "$machine;$user;$env:LOCALAPPDATA\Programs\Ollama"
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

$pythonOk = $false
try { & py -3.12 --version | Out-Host; if ($LASTEXITCODE -eq 0) { $pythonOk = $true } } catch {}
if (-not $pythonOk) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw "Python 3.12 is required. Install it and run EIRVEN again." }
    Invoke-EirvenRetry -Name "Python 3.12 installation" -Action {
        & winget install --id Python.Python.3.12 --exact --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -ne 0) { throw "Failed to install Python 3.12." }
        Refresh-Path
        & py -3.12 --version | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "Python installed but is not ready yet." }
    }
}

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
            if ($currentOllama -lt [version]'0.12.7') {
                Invoke-EirvenRetry -Name "Ollama update" -Action {
                    Invoke-RestMethod https://ollama.com/install.ps1 | Invoke-Expression
                    Refresh-Path
                }
            }
        }
    } catch {
        Write-Host "Could not auto-update Ollama; continuing with the installed runtime." -ForegroundColor Yellow
    }
}

# Pillow is the only tiny dependency needed before the main venv exists: it lets the
# first installer frame render the supplied EIRVEN artwork with high-quality scaling.
Invoke-EirvenRetry -Name "Installer visual dependency" -Action {
    & py -3.12 -m pip install --disable-pip-version-check --quiet "Pillow>=11,<13"
    if ($LASTEXITCODE -ne 0) { throw "Could not prepare the installer visual layer." }
}

# bootstrap.py owns the visible installation window and retries a failed installation
# attempt automatically. Already downloaded models/venv files are intentionally preserved.
& py -3.12 "$Root\scripts\bootstrap.py"
if ($LASTEXITCODE -ne 0) { throw "EIRVEN bootstrap failed after automatic recovery attempts" }
