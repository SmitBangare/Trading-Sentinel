# Keeps node-gateway running: restarts it automatically if it ever
# exits/crashes. Registered as a Windows Scheduled Task (trigger: "At
# log on") so it comes back up on its own after a reboot.
$ErrorActionPreference = "Continue"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
$root = "D:\Trading Sentinel\node-gateway\server"
$log = "D:\Trading Sentinel\run\logs\node_gateway.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

while ($true) {
    Add-Content -Path $log -Value "[$(Get-Date -Format o)] starting node-gateway"
    Set-Location $root
    & node index.js *>> $log
    Add-Content -Path $log -Value "[$(Get-Date -Format o)] node-gateway exited (code $LASTEXITCODE) -- restarting in 5s"
    Start-Sleep -Seconds 5
}
