Param(
    [string]$Python = "python",
    [string]$VenvPath = ".venv"
)

Write-Host "[setup] Using Python executable: $Python"
Write-Host "[setup] Creating virtual environment at $VenvPath" -ForegroundColor Cyan

# Create venv
& $Python -m venv $VenvPath
if ($LASTEXITCODE -ne 0) { Write-Error "Failed to create virtual environment"; exit 1 }

$activate = Join-Path $VenvPath "Scripts/Activate.ps1"
Write-Host "[setup] Activating virtual environment" -ForegroundColor Cyan
. $activate

Write-Host "[setup] Upgrading pip/setuptools/wheel" -ForegroundColor Cyan
pip install --upgrade pip setuptools wheel

Write-Host "[setup] Installing requirements" -ForegroundColor Cyan
pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Error "Failed to install requirements"; exit 1 }

Write-Host "[setup] Environment ready." -ForegroundColor Green
Write-Host "Run: .\\$VenvPath\\Scripts\\Activate.ps1  然后  python .\\main.py --help" -ForegroundColor Yellow
