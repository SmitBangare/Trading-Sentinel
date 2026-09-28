# Keeps python-engine (uvicorn) running: restarts it automatically if it
# ever exits/crashes. Registered as a Windows Scheduled Task (trigger:
# "At log on") so the whole thing comes back up on its own after a
# reboot, without anyone needing to open a terminal.
$ErrorActionPreference = "Continue"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
$env:FYERS_CLIENT_ID = "OTNO8IKLWZ-100"
$root = "D:\Trading Sentinel\python-engine"
$log = "D:\Trading Sentinel\run\logs\python_engine.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

while ($true) {
    Add-Content -Path $log -Value "[$(Get-Date -Format o)] starting python-engine"
    Set-Location $root
    & ".\venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000 *>> $log
    Add-Content -Path $log -Value "[$(Get-Date -Format o)] python-engine exited (code $LASTEXITCODE) -- restarting in 5s"
    Start-Sleep -Seconds 5
}
