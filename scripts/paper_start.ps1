# 星财富 · 本地 Paper/Replay 启动脚本
#
# 用法（任意目录双击或右键运行均可，脚本自己定位项目根）：
#   .\scripts\paper_start.ps1                 # 默认 30 分钟
#   .\scripts\paper_start.ps1 -Minutes 5      # 跑 5 分钟
#   .\scripts\paper_start.ps1 -Replay all     # 只跑一遍全部场景
#
# 硬性保护：本脚本只启动**本地模拟盘**，不连接任何真实券商、不发真实订单。

[CmdletBinding()]
param(
    [double]$Minutes = 30,
    [string]$Replay = "",
    [double]$TickInterval = 0.0,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host "[FAIL] 找不到虚拟环境解释器：$Py" -ForegroundColor Red
    exit 2
}

$RunRoot = Join-Path $Root "data\paper_runs"
New-Item -ItemType Directory -Force -Path $RunRoot | Out-Null
$PidFile = Join-Path $RunRoot "supervisor.pid"
$LogFile = Join-Path $RunRoot ("supervisor-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))

# 1) 已在运行则拒绝（幂等性：不重复启动）
if (Test-Path $PidFile) {
    $oldPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($oldPid) {
        $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "[BLOCKED] 已有 Paper 进程在运行 (PID=$oldPid)。先执行 paper_stop.ps1。" -ForegroundColor Yellow
            exit 3
        } else {
            Write-Host "[info] 清理失效 PID 文件：$oldPid"
            Remove-Item $PidFile -Force
        }
    }
}

# 2) 硬性保护再检查一遍（不依赖 Python 内部检查）
$cfgPath = Join-Path $Root "config\config.yaml"
if (Test-Path $cfgPath) {
    $cfgText = Get-Content $cfgPath -Raw
    if ($cfgText -match "(?m)^us_phase2:\s*\r?\n\s*\r?\n\s*enabled:\s*true") {
        Write-Host "[FAIL] us_phase2.enabled=true —— 本轮必须为 false，拒绝启动。" -ForegroundColor Red
        exit 2
    }
}
if ($env:LIVE_TRADING -match "^(1|true|yes)$") {
    Write-Host "[FAIL] 环境变量 LIVE_TRADING 为真，拒绝启动。" -ForegroundColor Red
    exit 2
}

# 3) 组装参数（--paper 是强制项）
$env:PYTHONPATH = (Join-Path $Root "src")
$argList = @("-m", "ai_trader.paper_runtime", "--paper", "--out", $RunRoot)
if ($Replay) {
    $argList += @("--replay", $Replay)
    if ($TickInterval -gt 0) { $argList += @("--tick-interval", $TickInterval) }
} else {
    $argList += @("--soak-minutes", $Minutes)
    if ($TickInterval -gt 0) { $argList += @("--tick-interval", $TickInterval) }
}

Write-Host "[start] $Py $($argList -join ' ')"
Write-Host "[start] 日志：$LogFile"

$p = Start-Process -FilePath $Py -ArgumentList $argList -WorkingDirectory $Root `
    -RedirectStandardOutput $LogFile -RedirectStandardError "$LogFile.err" `
    -PassThru -WindowStyle Hidden
$p.Id | Out-File -FilePath $PidFile -Encoding ascii

Write-Host "[OK] 已启动 PID=$($p.Id)" -ForegroundColor Green
Write-Host "     状态：.\scripts\paper_status.ps1"
Write-Host "     停止：.\scripts\paper_stop.ps1"
exit 0
