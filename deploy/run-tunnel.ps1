# Auto-restart wrapper for the Cloudflare tunnel. Launched at boot by the
# "Dictato-Tunnel" scheduled task. Uses the config + credentials in
# %USERPROFILE%\.cloudflared\ (config.yml supplies the tunnel + ingress).
$ErrorActionPreference = 'Continue'
$cloudflared = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cloudflared) { $cloudflared = 'C:\Program Files (x86)\cloudflared\cloudflared.exe' }
$log = Join-Path (Split-Path $PSScriptRoot -Parent) 'dictato-tunnel.log'
while ($true) {
    "[$(Get-Date -Format s)] starting cloudflared" | Out-File -Append -Encoding utf8 $log
    & $cloudflared tunnel run *>> $log
    "[$(Get-Date -Format s)] cloudflared exited - restarting in 5s" | Out-File -Append -Encoding utf8 $log
    Start-Sleep -Seconds 5
}
