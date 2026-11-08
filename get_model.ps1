# Downloads the DINOv2-small vision model (about 84 MB, Apache-2.0) used by the AI modes.
# The model is not stored in the repository because of its size. Run once:
#   powershell -ExecutionPolicy Bypass -File get_model.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$dest = "models\dinov2-small.onnx"
if (Test-Path $dest) { Write-Host "Model already present: $dest"; exit 0 }
New-Item -ItemType Directory -Force models | Out-Null
$url = "https://huggingface.co/onnx-community/dinov2-small/resolve/main/onnx/model.onnx"
Write-Host "Downloading $url ..."
Invoke-WebRequest -Uri $url -OutFile "$dest.part" -UseBasicParsing
Move-Item "$dest.part" $dest -Force
Write-Host ("Saved {0} ({1:N1} MB)" -f $dest, ((Get-Item $dest).Length / 1MB))
