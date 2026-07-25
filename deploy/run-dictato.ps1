# Auto-restart wrapper for the Dictato app (uvicorn). Launched at boot by the
# "Dictato" scheduled task (see install-dictato-tasks.ps1 + HOSTING.md Part 3).
# Loops so the app comes back if it ever exits; logs to dictato-app.log.
$ErrorActionPreference = 'Continue'
$repo = Split-Path $PSScriptRoot -Parent
Set-Location $repo
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
$log = Join-Path $repo 'dictato-app.log'
$port = 8000

# Stop-ScheduledTask kills THIS wrapper but not the uvicorn grandchild it spawned,
# so a stale server can keep holding the port. The new instance then fails to
# bind, exits, and the OLD CODE KEEPS SERVING — silently, because the app still
# answers 200 and static files are read from disk each request. That hid a
# ten-day-old build in production. Claim the port before starting.
function Clear-StalePort($p) {
    $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        if ($c.OwningProcess -eq $PID) { continue }
        "[$(Get-Date -Format s)] port $p held by PID $($c.OwningProcess) - stopping it" |
            Out-File -Append -Encoding utf8 $log
        try { Stop-Process -Id $c.OwningProcess -Force -ErrorAction Stop }
        catch { "[$(Get-Date -Format s)] could not stop PID $($c.OwningProcess): $($_.Exception.Message)" |
                    Out-File -Append -Encoding utf8 $log }
    }
    if ($conns) { Start-Sleep -Seconds 2 }
}

while ($true) {
    Clear-StalePort $port
    "[$(Get-Date -Format s)] starting uvicorn" | Out-File -Append -Encoding utf8 $log
    & $uv run uvicorn app.main:app --host 127.0.0.1 --port $port *>> $log
    "[$(Get-Date -Format s)] uvicorn exited ($LASTEXITCODE) - restarting in 5s" | Out-File -Append -Encoding utf8 $log
    Start-Sleep -Seconds 5
}
