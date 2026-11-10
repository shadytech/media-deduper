# Downloads the DINOv2-small vision model (about 84 MB, Apache-2.0) used by the AI modes.
# The model is not stored in the repository because of its size. Run once:
#   powershell -ExecutionPolicy Bypass -File get_model.ps1
#
# The download is pinned to one exact revision of the model repository and checked
# against its SHA-256, so a changed or tampered file is never installed.
#   -VerifyOnly   check the installed file and download nothing (used by build.ps1)
param([switch]$VerifyOnly)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$dest = "models\dinov2-small.onnx"
$revision = "8b1f705a3a7f6f062f6bdd21986c1583d3ef105d"   # huggingface.co/onnx-community/dinov2-small
$sha256 = "f22797eabf810a75e41de68d378541ebea372122b25c4ce3ef25ff618250c20a"
$url = "https://huggingface.co/onnx-community/dinov2-small/resolve/$revision/onnx/model.onnx"

function Test-Model([string]$path) {
    return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -eq $sha256   # (-eq ignores case)
}

if (Test-Path $dest) {
    if (Test-Model $dest) { Write-Host "Model present and verified: $dest"; exit 0 }
    Write-Host "ERROR: $dest is not the expected model file (SHA-256 mismatch)."
    Write-Host "Delete it and run get_model.ps1 again to download the right one."
    exit 1
}
if ($VerifyOnly) { Write-Host "ERROR: $dest is missing. Run get_model.ps1 to download it."; exit 1 }

New-Item -ItemType Directory -Force models | Out-Null
Write-Host "Downloading $url ..."
Invoke-WebRequest -Uri $url -OutFile "$dest.part" -UseBasicParsing
if (-not (Test-Model "$dest.part")) {
    Remove-Item "$dest.part" -Force
    Write-Host "ERROR: the downloaded file failed its SHA-256 check and was deleted. Nothing was installed."
    exit 1
}
Move-Item "$dest.part" $dest -Force
Write-Host ("Saved and verified {0} ({1:N1} MB)" -f $dest, ((Get-Item $dest).Length / 1MB))
