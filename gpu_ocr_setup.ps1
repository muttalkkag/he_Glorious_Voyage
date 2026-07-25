$ErrorActionPreference = "Stop"
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Base
$Python = Join-Path $Base ".venv\Scripts\python.exe"
$Log = Join-Path $Base "gpu_ocr_setup.log"
$Marker = Join-Path $Base ".gpu_ocr_enabled"

try {
    Start-Transcript -Path $Log -Append | Out-Null
    if (-not (Test-Path $Python)) {
        throw "Local Python environment is missing. Run SETUP.cmd first."
    }

    Write-Host "Installing ONNX Runtime CUDA support for RTX fallback OCR..."
    & $Python -m pip uninstall -y onnxruntime onnxruntime-gpu
    & $Python -m pip install "onnxruntime-gpu[cuda,cudnn]==1.20.1"
    if ($LASTEXITCODE -ne 0) {
        throw "onnxruntime-gpu installation failed."
    }

    $Check = @'
import onnxruntime as ort
if hasattr(ort, "preload_dlls"):
    try:
        ort.preload_dlls(directory="")
    except Exception:
        pass
providers = ort.get_available_providers()
print("Available providers:", providers)
if "CUDAExecutionProvider" not in providers:
    raise SystemExit("CUDAExecutionProvider was not detected.")
'@
    & $Python -c $Check
    if ($LASTEXITCODE -ne 0) {
        throw "CUDA provider check failed. Check gpu_ocr_setup.log."
    }

    Set-Content -Path $Marker -Value "enabled" -Encoding ASCII
    Write-Host ""
    Write-Host "RTX fallback OCR setup completed." -ForegroundColor Green
    Write-Host "Start the program and enable 'RTX 보조 OCR(오류칸만)'."
}
catch {
    Write-Host ""
    Write-Host "GPU OCR SETUP FAILED:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "The normal game-font OCR still works on CPU."
    Write-Host "See gpu_ocr_setup.log in this folder."
}
finally {
    try { Stop-Transcript | Out-Null } catch {}
    Read-Host "Press Enter to close"
}
