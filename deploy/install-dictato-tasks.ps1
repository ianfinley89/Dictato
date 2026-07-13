#Requires -RunAsAdministrator
<#
  Installs Dictato as two auto-starting Windows scheduled tasks:

    Dictato         -> run-dictato.ps1  (the uvicorn app)
    Dictato-Tunnel  -> run-tunnel.ps1   (the Cloudflare tunnel)

  Both start AT BOOT and run as you WITHOUT a stored password (S4U logon), so
  the site survives unattended reboots with no one logged in. The wrapper
  scripts loop, so each process also restarts if it crashes.

  Run once from an ELEVATED PowerShell (Run as administrator):
      cd C:\Users\ianfi\workspace\Dictato
      powershell -ExecutionPolicy Bypass -File deploy\install-dictato-tasks.ps1

  Re-run any time to update. Remove with:
      Unregister-ScheduledTask -TaskName Dictato,Dictato-Tunnel -Confirm:$false

  See HOSTING.md Part 3.
#>
$ErrorActionPreference = 'Stop'
$deploy = $PSScriptRoot

function Install-DictatoTask {
    param([string]$Name, [string]$Script, [string]$Description)
    $wrapper = Join-Path $deploy $Script
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $wrapper)
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    # S4U = "run whether logged on or not" with NO stored password.
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
    # ExecutionTimeLimit 0 = never time out (these are long-running).
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit 0 `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description $Description -Force | Out-Null
    Start-ScheduledTask -TaskName $Name
    Write-Host "  registered + started: $Name"
}

Write-Host 'Installing Dictato auto-start tasks (app + tunnel)...'
Install-DictatoTask -Name 'Dictato'        -Script 'run-dictato.ps1' -Description 'Dictato app (uvicorn) - auto-start at boot'
Install-DictatoTask -Name 'Dictato-Tunnel' -Script 'run-tunnel.ps1'  -Description 'Dictato Cloudflare tunnel - auto-start at boot'

Start-Sleep -Seconds 8
Write-Host ''
Get-ScheduledTask -TaskName 'Dictato','Dictato-Tunnel' | Get-ScheduledTaskInfo |
    Select-Object TaskName, LastRunTime, @{n = 'Result'; e = { $_.LastTaskResult } } | Format-Table -AutoSize
Write-Host 'Result 267009 = running (good); 0 = exited. Logs: dictato-app.log, dictato-tunnel.log in the repo root.'
Write-Host 'Now open https://dictato.levelup-ai.com'
