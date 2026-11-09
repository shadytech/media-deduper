# Builds a standalone Windows app in dist\ImageDupeFinder\ImageDupeFinder.exe
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path models\dinov2-small.onnx)) {
    throw "AI model missing: download https://huggingface.co/onnx-community/dinov2-small/resolve/main/onnx/model.onnx to models\dinov2-small.onnx"
}
if (-not (Test-Path .venv\Scripts\python.exe)) {
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
}
.venv\Scripts\python.exe -m pip install --quiet pyinstaller
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed `
    --name ImageDupeFinder `
    --icon assets\app.ico `
    --add-data "assets\app.ico;assets" `
    --collect-all pillow_heif `
    --collect-binaries onnxruntime `
    --collect-submodules av --collect-binaries av `
    --add-data "models\dinov2-small.onnx;models" `
    --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets `
    --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick --exclude-module PySide6.QtPdf `
    --exclude-module tkinter `
    app.py
Write-Host "Built: $PSScriptRoot\dist\ImageDupeFinder\ImageDupeFinder.exe"
