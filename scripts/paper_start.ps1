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
$argList = @("-m", "ai_trader.paper_runtime", "--paper", "--out", $RunRoot,
             "--lock-root", $RunRoot, "--log-file", $LogFile)
if ($Replay) {
    $argList += @("--replay", $Replay)
    if ($TickInterval -gt 0) { $argList += @("--tick-interval", $TickInterval) }
} else {
    $argList += @("--soak-minutes", $Minutes)
    if ($TickInterval -gt 0) { $argList += @("--tick-interval", $TickInterval) }
}

Write-Host "[start] $Py $($argList -join ' ')"
Write-Host "[start] 日志：$LogFile"

# -Replay 是**前台批处理**：直接调用并透传退出码（参数错误必须能被调用方看到）。
if ($Replay) {
    & $Py @argList
    exit $LASTEXITCODE
}

# 不用 Start-Process：PowerShell 5.1 在同时存在 HTTPS_PROXY / https_proxy 时
# 复制环境变量会抛 "字典中的关键字重复"（ArgumentException）。
# 直接用 .NET ProcessStartInfo（UseShellExecute=false）启动，环境变量由子进程继承。
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $Py
$psi.Arguments = ($argList -join ' ')
$psi.WorkingDirectory = $Root
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi
if (-not $proc.Start()) {
    Write-Host "[FAIL] 进程启动失败" -ForegroundColor Red
    exit 5
}
$proc.Id | Out-File -FilePath $PidFile -Encoding ascii

# 4) 存活校验（仅 soak 模式）：分离启动无法同步拿到子进程退出码，
#    所以启动后短暂观察；若立刻退出，说明参数/保护检查失败 —— 必须报错而不是假装成功。
if (-not $Replay) {
    Start-Sleep -Milliseconds 1800
    $proc.Refresh()
    if ($proc.HasExited) {
        $tail = ""
        if (Test-Path $LogFile) {
            $tail = (Get-Content $LogFile -Tail 8 -ErrorAction SilentlyContinue) -join " | "
        }
        Write-Host "[FAIL] 进程启动后立即退出（exit=$($proc.ExitCode)）。日志：$tail" -ForegroundColor Red
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        exit 6
    }
}

Write-Host "[OK] 已启动 PID=$($proc.Id)" -ForegroundColor Green
Write-Host "     状态：.\scripts\paper_status.ps1"
Write-Host "     停止：.\scripts\paper_stop.ps1"
exit 0

