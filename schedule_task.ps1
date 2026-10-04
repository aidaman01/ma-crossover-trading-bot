# Registers the "Trading Bot" task in Windows Task Scheduler (current user).
#
# The task fires Mon-Fri every 5 minutes in a local-time window that covers the US
# market open (9:30 ET) in both US daylight and standard time, whatever this PC's
# time zone is. Each trigger runs `bot.py --scheduled`, which exits unless it is
# >= SCHEDULED_RUN_TIME_ET, the Alpaca clock says the market is open, and it has not
# already run today - so the bot runs once per trading day at ~9:35 ET all year.
#
# Assumes 9:30 ET falls on the same weekday in local time (true for the Americas,
# Europe, Africa and most of Asia). Re-run this script any time to update the task.

$TaskName   = "Trading Bot"
$ProjectDir = $PSScriptRoot
$Python     = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\pythonw.exe"  # no console window

if (-not (Test-Path $Python)) { throw "Python not found at $Python - edit `$Python in this script" }

# Local time of 9:30 ET on a summer (EDT) and a winter (EST) date.
$eastern = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$localOpens = foreach ($date in "2025-07-15", "2025-01-15") {
    $et = [DateTime]::SpecifyKind([DateTime]::Parse("$date 09:30"), "Unspecified")
    [TimeZoneInfo]::ConvertTime($et, $eastern, [TimeZoneInfo]::Local).TimeOfDay
}
$windowStart = ($localOpens | Measure-Object -Minimum).Minimum.Subtract([TimeSpan]::FromMinutes(5))
$windowMins  = [int](($localOpens | Measure-Object -Maximum).Maximum - $windowStart).TotalMinutes + 60
$startAt     = [DateTime]::Today.Add($windowStart)

$action = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$ProjectDir\bot.py`" --scheduled" -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $startAt
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $startAt `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Minutes $windowMins)).Repetition

$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Trend allocation bot (Alpaca paper). Runs once per trading day at/after 9:35 AM ET." `
    -Force | Out-Null

"Trigger window: {0:hh\:mm} local, every 5 min for {1} min, Mon-Fri" -f $windowStart, $windowMins
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo |
    Select-Object TaskName, NextRunTime, LastRunTime, LastTaskResult
