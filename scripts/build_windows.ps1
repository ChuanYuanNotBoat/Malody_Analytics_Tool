param(
  [string]$PythonExe = "python",
  [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
Set-Location $projectRoot

Write-Host "Project root: $projectRoot"

if (-not $SkipTests) {
  Write-Host "Running tests before packaging..."
  & $PythonExe -m unittest discover -s tests -p "test_*.py" -v
}

Write-Host "Ensuring PyInstaller is available..."
& $PythonExe -m pip install --upgrade pyinstaller

Write-Host "Building Windows package..."
& $PythonExe -m PyInstaller `
  --noconfirm `
  --clean `
  --name "MalodyAnalyticsDesktop" `
  --windowed `
  --collect-all "PySide6" `
  --hidden-import "openpyxl" `
  --add-data "docs;docs" `
  --add-data "translations;translations" `
  --add-data "resources_rc.py;." `
  main.py

Write-Host "Build completed."
Write-Host "Output: dist\\MalodyAnalyticsDesktop\\MalodyAnalyticsDesktop.exe"
