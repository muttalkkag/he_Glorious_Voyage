
$ErrorActionPreference = "Stop"
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Base
$Log = Join-Path $Base "setup.log"

function Find-Python311 {
    try {
        $path = & py -3.11 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $path) {
            return ($path | Select-Object -Last 1).Trim()
        }
    } catch {}

    $candidate = Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"
    if (Test-Path $candidate) { return $candidate }
    return $null
}

try {
    Start-Transcript -Path $Log -Append | Out-Null
    Write-Host "Checking Python 3.11..."

    $Python = Find-Python311
    if (-not $Python) {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            throw "winget was not found. Install Python 3.11 x64 and run SETUP.cmd again."
        }

        Write-Host "Installing Python 3.11..."
        & winget install --id Python.Python.3.11 -e --scope user `
            --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) {
            throw "Python 3.11 installation failed."
        }

        Start-Sleep -Seconds 3
        $Python = Find-Python311
    }

    if (-not $Python) {
        throw "Python 3.11 could not be found after installation."
    }

    Write-Host "Python: $Python"
    $VenvPython = Join-Path $Base ".venv\Scripts\python.exe"

    if (-not (Test-Path $VenvPython)) {
        Write-Host "Creating local environment..."
        & $Python -m venv (Join-Path $Base ".venv")
        if ($LASTEXITCODE -ne 0) { throw "venv creation failed." }
    }

    Write-Host "Installing packages..."
    & $VenvPython -m pip install --upgrade pip setuptools wheel
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

    & $VenvPython -m pip install -r (Join-Path $Base "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "package installation failed." }

    $GpuMarker = Join-Path $Base ".gpu_ocr_enabled"
    if (Test-Path $GpuMarker) {
        Write-Host "Restoring optional RTX OCR runtime..."
        & $VenvPython -m pip uninstall -y onnxruntime onnxruntime-gpu
        & $VenvPython -m pip install "onnxruntime-gpu[cuda,cudnn]==1.20.1"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "RTX runtime restore failed; normal CPU OCR remains available." -ForegroundColor Yellow
        }
    }

    Write-Host "Checking imports..."
    & $VenvPython -c "import tkinter,cv2,mss,numpy,pyautogui,PIL,scipy; import hex_map,game_digit_ocr; print('Core imports OK')"
    if ($LASTEXITCODE -ne 0) { throw "core import test failed." }

    Write-Host ""
    Write-Host "Setup completed. Starting the program..."
    & $VenvPython (Join-Path $Base "launcher.py")
}
catch {
    Write-Host ""
    Write-Host "SETUP FAILED:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "See setup.log in this folder."
    Read-Host "Press Enter to close"
}
finally {
    try { Stop-Transcript | Out-Null } catch {}
}
