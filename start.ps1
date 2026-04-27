$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

& "$ScriptDir\venv\Scripts\Activate.ps1"

Write-Host "Demarrage MLB Analyzer sur http://localhost:5001 ..."
Start-Process "http://localhost:5001"
python app.py
