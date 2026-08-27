# 用法: powershell -ExecutionPolicy Bypass -File scripts\run_backup.ps1 configs\instance-a.toml
param([string]$Config = "configs\instance-a.toml")

$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root "configs\.env"
if (-not (Test-Path $envFile)) {
    Write-Error "缺少凭据文件 $envFile（请从 configs\.env.example 复制并填写）"
    exit 2
}

# 导入凭据（不打印，不回显）
Get-Content $envFile | Where-Object { $_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*=' } | ForEach-Object {
    $kv = $_ -split '=', 2
    Set-Item -Path "Env:$($kv[0].Trim())" -Value $kv[1].Trim()
}

& python (Join-Path $root "application\main.py") backup --config (Join-Path $root $Config)
exit $LASTEXITCODE