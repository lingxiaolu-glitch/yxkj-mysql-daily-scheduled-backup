# 用法: powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Config configs\instance-a.toml -Time 02:00
# 说明: 注册 Windows 计划任务，每实例一个任务，避免多实例互相覆盖。
param(
    [string]$Config = "configs\instance-a.toml",
    [string]$Time = "02:00"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$ConfigPath = Join-Path $Root $Config
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Error "配置文件不存在: $ConfigPath"
    exit 2
}

$Instance = [System.IO.Path]::GetFileNameWithoutExtension($Config)
$TaskName = "mysql-daily-backup-$Instance"
$Runner = Join-Path $Root "scripts\run_backup.ps1"

# 创建动作：每天指定时间运行 wrapper，wrapper 会加载 configs/.env。
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" `"$Config`""

$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew

# 注册或覆盖同名任务。
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "MySQL daily backup: $ConfigPath" -Force | Out-Null

Write-Output "已注册计划任务: $TaskName (每日 $Time)"