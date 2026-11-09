# Creates a "Media Deduper" shortcut that runs run.bat with the app icon.
# (A .bat file can't have its own icon in Windows, so a shortcut carries it.)
#
#   powershell -ExecutionPolicy Bypass -File make_shortcut.ps1            # shortcut in this folder
#   powershell -ExecutionPolicy Bypass -File make_shortcut.ps1 -Desktop   # also on the Desktop
#   powershell -ExecutionPolicy Bypass -File make_shortcut.ps1 -StartMenu # also in the Start menu
param([switch]$Desktop, [switch]$StartMenu)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$targets = @(Join-Path $here "Media Deduper.lnk")
if ($Desktop) { $targets += Join-Path ([Environment]::GetFolderPath("Desktop")) "Media Deduper.lnk" }
if ($StartMenu) { $targets += Join-Path ([Environment]::GetFolderPath("Programs")) "Media Deduper.lnk" }

$shell = New-Object -ComObject WScript.Shell
foreach ($path in $targets) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = Join-Path $here "run.bat"
    $lnk.WorkingDirectory = $here
    $lnk.IconLocation = (Join-Path $here "assets\app.ico") + ",0"
    $lnk.Description = "Find duplicate photos and videos"
    $lnk.WindowStyle = 7   # start minimized, so run.bat's console doesn't flash up
    $lnk.Save()
    Write-Host "Created $path"
}
