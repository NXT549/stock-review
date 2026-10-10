<#
    build_exe.ps1 - build the Stock Review desktop app, end to end.

    Usage (from the project root):
        powershell -ExecutionPolicy Bypass -File packaging/build_exe.ps1

    Steps:
      1. Ensure PyInstaller is installed in .venv313.
      2. Clean build/, dist/, packaging/installer_output/.
      3. Freeze both exes with packaging/stock_review_gui.spec ->
         dist/Stock Review/{Stock Review.exe, Stock Review Terminal.exe}.
      4. If Inno Setup (ISCC.exe) is available, package a single
         "Stock Review Setup.exe" (packaging/installer_output/).

    Final artifact: packaging/installer_output/Stock Review Setup.exe
    (If Inno Setup isn't installed, zip 'dist/Stock Review' to distribute
    the folder as-is.)
#>
$ErrorActionPreference = "Stop"

# Resolve the project root (this script's parent's parent) and the venv python.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv313\Scripts\python.exe"
if (-not (Test-Path $py)) {
    throw "Expected interpreter not found: $py  (see memory: use .venv313, Python 3.13)."
}

$installerOut = Join-Path $root "packaging\installer_output"

Write-Host "Ensuring PyInstaller is installed..." -ForegroundColor Cyan
& $py -m pip install --quiet --disable-pip-version-check "pyinstaller>=6.6" pyinstaller-hooks-contrib

Write-Host "Cleaning previous build..." -ForegroundColor Cyan
Remove-Item -Recurse -Force "$root\build", "$root\dist", $installerOut -ErrorAction SilentlyContinue

Write-Host "Building (this takes a few minutes)..." -ForegroundColor Cyan
& $py -m PyInstaller "packaging\stock_review_gui.spec" --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)." }

$exeGui = Join-Path $root "dist\Stock Review\Stock Review.exe"
$exeTerm = Join-Path $root "dist\Stock Review\Stock Review Terminal.exe"
foreach ($e in @($exeGui, $exeTerm)) {
    if (-not (Test-Path $e)) { throw "Build finished but an exe is missing: $e" }
}
Write-Host "`nBuilt both exes in 'dist\Stock Review\'." -ForegroundColor Green

# --- Package into a single Setup.exe with Inno Setup (if available) --------- #
$iss = Join-Path $root "packaging\installer.iss"
$iscc = $null
$cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($cmd) { $iscc = $cmd.Source }
if (-not $iscc) {
    foreach ($p in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 5\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 5\ISCC.exe"
    )) { if ($p -and (Test-Path $p)) { $iscc = $p; break } }
}

if ($iscc) {
    Write-Host "`nPackaging installer with Inno Setup ($iscc)..." -ForegroundColor Cyan
    & $iscc $iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup (ISCC) failed (exit $LASTEXITCODE)." }
    $setup = Join-Path $installerOut "Stock Review Setup.exe"
    if (Test-Path $setup) {
        Write-Host "`n==> Installer ready: $setup" -ForegroundColor Green
        Write-Host "    Hand this ONE file to a user; they double-click it to install." -ForegroundColor Green
    } else {
        throw "ISCC finished but the installer is missing: $setup"
    }
} else {
    Write-Host "`nInno Setup (ISCC.exe) not found - skipped the installer step." -ForegroundColor Yellow
    Write-Host "  Install it:  winget install JRSoftware.InnoSetup" -ForegroundColor Yellow
    Write-Host "  Then re-run this script, or zip 'dist\Stock Review' to share the folder." -ForegroundColor Yellow
}
