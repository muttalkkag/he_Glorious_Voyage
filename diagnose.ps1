$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Base
$Python = Join-Path $Base ".venv\Scripts\python.exe"

Write-Host "=== Sea Trade Planner diagnostics ==="
Write-Host "Folder: $Base"

if (-not (Test-Path $Python)) {
    Write-Host "Local Python environment is missing. Run SETUP.cmd first." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit
}

& $Python -c "import sys; print(sys.version); print(sys.executable)"
& $Python -c "import tkinter,cv2,mss,numpy,pyautogui,PIL,scipy; import hex_map,game_digit_ocr; print('Core imports OK')"
& $Python -c "from pathlib import Path; from game_digit_ocr import GameDigitOCR; GameDigitOCR(Path('ocr_digit_templates.npz')); print('Game digit templates OK')"
& $Python -c "import winocr; print('Windows OCR import OK')"
& $Python -c "from rapidocr_onnxruntime import RapidOCR; print('RapidOCR import OK')"
& $Python -c "import onnxruntime as ort; print('ONNX Runtime providers:', ort.get_available_providers())"

& $Python -m py_compile app.py launcher.py hex_map.py game_digit_ocr.py
& $Python -c "import json; json.load(open('config.json', encoding='utf-8')); json.load(open('trade_data.json', encoding='utf-8')); print('Python and JSON checks OK')"

foreach ($name in @("startup_error.log", "runtime_error.log", "setup.log", "gpu_ocr_setup.log")) {
    $path = Join-Path $Base $name
    if (Test-Path $path) {
        Write-Host ""
        Write-Host "=== $name ==="
        Get-Content $path -Tail 100
    }
}

Read-Host "Press Enter to close"
