param([Parameter(ValueFromRemainingArguments)][string[]]$PassThrough)

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv   = Join-Path $ProjectDir ".venv-win"
$Python = Join-Path $Venv "Scripts\python.exe"

# Bootstrap Windows venv on first run
if (-not (Test-Path $Python)) {
    Write-Host "[setup] Creating Windows virtual environment..."

    # Prefer Python 3.11 if installed, fall back to default py
    $null = & py -3.11 -m venv $Venv 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[setup] Python 3.11 not found, using default Python..."
        & py -m venv $Venv
        if ($LASTEXITCODE -ne 0) { Write-Error "Could not create venv. Is Python installed?"; exit 1 }
    }

    Write-Host "[setup] Installing dependencies..."
    & $Python -m pip install --upgrade pip --quiet

    $ReqFile = if (Test-Path (Join-Path $ProjectDir "requirements-win.txt")) {
        Join-Path $ProjectDir "requirements-win.txt"
    } else {
        Join-Path $ProjectDir "requirements.txt"
    }
    & $Python -m pip install -r $ReqFile
    if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed. Check output above."; exit 1 }

    Write-Host "[setup] Done. Run start.ps1 again to launch the algo."
    exit 0
}

# Kill any process holding the dashboard port
$DashPort = if ($env:DASH_PORT) { [int]$env:DASH_PORT } else { 8080 }

$listeners = Get-NetTCPConnection -LocalPort $DashPort -State Listen -ErrorAction SilentlyContinue
if ($listeners) {
    $owningPids = $listeners | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($pid in $owningPids) {
        Write-Host "Killing PID $pid on port $DashPort..."
        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Waiting 5s for Angel One WebSocket to release..."
    Start-Sleep -Seconds 5
}

Set-Location $ProjectDir
& $Python main.py @PassThrough
