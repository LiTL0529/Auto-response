# Registers the agent as a Windows scheduled task that starts at logon and restarts on failure.
# Usage (PowerShell, in the agent folder):
#   powershell -ExecutionPolicy Bypass -File install_windows.ps1
# Uninstall:
#   Unregister-ScheduledTask -TaskName AutoResponseAgent -Confirm:$false
param([string]$Config = "config.yaml", [string]$TaskName = "AutoResponseAgent")
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $here ".venv\Scripts\pythonw.exe"
if (!(Test-Path $pythonw)) { throw "Missing $pythonw. Run: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt" }
$configPath = (Resolve-Path (Join-Path $here $Config)).Path

& (Join-Path $here ".venv\Scripts\python.exe") -m bridge_agent --config $configPath --check
if ($LASTEXITCODE -ne 0) { throw "Self check failed, fix the items above first." }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m bridge_agent --config `"$configPath`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "WeCom requirements -> Claude Code agent" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

# Keep the PC awake while plugged in
powercfg /change standby-timeout-ac 0 | Out-Null
Write-Host "Installed and started task '$TaskName'. Log: $here\logs\agent.log"
