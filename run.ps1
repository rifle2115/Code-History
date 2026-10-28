$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    python -m venv .venv
    & .\.venv\Scripts\pip.exe install -r requirements.txt
}
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }

& .\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --port 8000
