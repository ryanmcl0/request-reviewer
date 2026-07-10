# RequestReviewerTray.ps1
# A Windows system tray icon that shows how many Claude Code permission
# prompts request-reviewer has auto-approved on your behalf. Reads the same
# JSONL audit log the Python hook writes (~/.claude/request-reviewer.log or
# $REVIEWER_LOG). Windows analog of the macOS menu bar app.
#
# Uses only what ships with Windows (.NET / Windows Forms) — no build step.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$logPath = if ($env:REVIEWER_LOG) {
    $env:REVIEWER_LOG
} else {
    Join-Path $env:USERPROFILE ".claude\request-reviewer.log"
}

function Get-ClicksSaved {
    if (-not (Test-Path $logPath)) { return 0 }
    $count = 0
    # Stream the file line by line so a large log never loads fully into memory.
    foreach ($line in [System.IO.File]::ReadLines($logPath)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        try {
            $record = $line | ConvertFrom-Json
            if ($record.final -eq "allow") { $count++ }
        } catch {
            # Ignore any malformed line, same as the macOS app.
        }
    }
    return $count
}

$notifyIcon = New-Object System.Windows.Forms.NotifyIcon
$notifyIcon.Icon = [System.Drawing.SystemIcons]::Shield
$notifyIcon.Text = "Claude Permission Reviewer"
$notifyIcon.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip

$header = New-Object System.Windows.Forms.ToolStripMenuItem
$header.Text = "Claude Permission Reviewer"
$header.Enabled = $false
[void]$menu.Items.Add($header)

[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))

$countItem = New-Object System.Windows.Forms.ToolStripMenuItem
$countItem.Text = "…"
$countItem.Enabled = $false
[void]$menu.Items.Add($countItem)

[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))

$quitItem = New-Object System.Windows.Forms.ToolStripMenuItem
$quitItem.Text = "Quit"
$quitItem.Add_Click({
    $notifyIcon.Visible = $false
    [System.Windows.Forms.Application]::Exit()
})
[void]$menu.Items.Add($quitItem)

# Recompute only when the menu is actually opened — no polling, no timers.
$menu.Add_Opening({
    $countItem.Text = ("{0:N0} clicks saved" -f (Get-ClicksSaved))
})

$notifyIcon.ContextMenuStrip = $menu

# Pump the Windows message loop so the tray icon stays responsive.
[System.Windows.Forms.Application]::Run()
