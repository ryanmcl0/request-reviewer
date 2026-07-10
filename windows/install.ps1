# Installer for the request-reviewer system tray app (Windows only).
#
# Copies the tray script to ~/.claude/tray/ and registers a Scheduled Task
# that starts it at logon and restarts it if it crashes — the same
# "just works, don't think about it" behavior as the reviewer hook.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install.ps1
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"

$installDir = Join-Path $env:USERPROFILE ".claude\tray"
$scriptDest = Join-Path $installDir "RequestReviewerTray.ps1"
$taskName   = "RequestReviewerTray"
$srcDir     = Split-Path -Parent $MyInvocation.MyCommand.Definition

function Stop-TrayProcess {
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
        Where-Object { $_.CommandLine -like "*RequestReviewerTray.ps1*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Stop-TrayProcess
    if (Test-Path $installDir) { Remove-Item -Recurse -Force $installDir }
    Write-Host "Removed scheduled task '$taskName' and $installDir."
    exit 0
}

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item (Join-Path $srcDir "RequestReviewerTray.ps1") $scriptDest -Force

# Replace any existing instance, then (re)register the logon task.
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Stop-TrayProcess

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptDest`""
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "request-reviewer tray icon" -Force | Out-Null

Start-ScheduledTask -TaskName $taskName

Write-Host ""
Write-Host "Installed and started. It will now:"
Write-Host "  - start automatically at logon"
Write-Host "  - restart itself if it crashes (up to 3 tries, 1 min apart)"
Write-Host "  - stay quit if you Quit it from the tray menu"
Write-Host ""
Write-Host "Look for the shield icon in the notification area (near the clock)."
Write-Host "If it's hidden, drag it out of the tray overflow (the ^ chevron)."
Write-Host "To remove: powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall"
