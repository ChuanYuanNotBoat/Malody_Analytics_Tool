param(
  [string]$PythonExe = "python",
  [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
Set-Location $projectRoot

Write-Host "Project root: $projectRoot"

Write-Host "Step 1/3: compile check..."
& $PythonExe -m compileall -q main.py utils tests scripts

if (-not $SkipTests) {
  Write-Host "Step 2/3: unit tests..."
  & $PythonExe -m unittest discover -s tests -p "test_*.py" -v
} else {
  Write-Host "Step 2/3: skipped tests by -SkipTests"
}

Write-Host "Step 3/3: docs/i18n/exclusion verification..."
& $PythonExe scripts\verify_repo_docs.py

Write-Host ""
Write-Host "Precommit checks completed."
Write-Host "Next: review git status and create initial commit when ready."
