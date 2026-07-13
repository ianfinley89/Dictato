# Auto-restart wrapper for the Dictato app (uvicorn). Launched at boot by the
# "Dictato" scheduled task (see install-dictato-tasks.ps1 + HOSTING.md Part 3).
# Loops so the app comes back if it ever exits; logs to dictato-app.log.
$ErrorActionPreference = 'Continue'
$repo = Split-Path $PSScriptRoot -Parent
Set-Location $repo
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
$log = Join-Path $repo 'dictato-app.log'
while ($true) {
    "[$(Get-Date -Format s)] starting uvicorn" | Out-File -Append -Encoding utf8 $log
    & $uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 *>> $log
    "[$(Get-Date -Format s)] uvicorn exited ($LASTEXITCODE) - restarting in 5s" | Out-File -Append -Encoding utf8 $log
    Start-Sleep -Seconds 5
}
