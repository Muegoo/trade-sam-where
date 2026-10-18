param(
    [ValidateSet('simulation', 'mt5')][string]$Mode = 'simulation',
    [string]$Symbol = 'XAUUSD',
    [int]$Port = 8000
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$tradePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $tradePython)) {
    & py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'สร้าง virtual environment ไม่สำเร็จ ติดตั้ง Python 3.12 64-bit ก่อน' }
    & $tradePython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'ติดตั้ง dependencies ไม่สำเร็จ' }
}
Write-Host "Trade Workspace: http://127.0.0.1:$Port · Mode: $Mode"
& $tradePython -m trade_app.server --mode $Mode --symbol $Symbol --port $Port
