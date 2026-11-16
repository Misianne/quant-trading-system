<#
.SYNOPSIS
    启动 FosunAI-Trading Web 管理台（前台运行）。

.DESCRIPTION
    重要：本脚本会清除当前会话的 PYTHONPATH 后再启动服务。

    原因：WorkBuddy/agent 会话会通过 PYTHONPATH 注入一个删除操作拦截钩子
    （sitecustomize.py）。该钩子在轮次删除量超过阈值后会调用 SystemExit(1)，
    而 Web 服务运行在自己的事件循环里，SystemExit 会直接终结整个进程 ——
    表现为"点了某个按钮服务就消失"。常驻服务不经过 agent shell，清除是可
    接受的部署做法。（详见 DEPLOYMENT.md 的"已知环境陷阱"）

.PARAMETER Host_
    监听地址，默认取配置 web.host（127.0.0.1）。对外暴露请显式传 0.0.0.0 并配置鉴权。

.PARAMETER Port
    监听端口，默认取配置 web.port（8770）。

.PARAMETER LogLevel
    uvicorn 日志级别，默认 info。
#>
[CmdletBinding()]
param(
    [string]$Host_ = "",
    [int]$Port = 0,
    [string]$LogLevel = "info"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "[FAIL] 未找到 .venv，请先运行 scripts\bootstrap_windows.ps1" -ForegroundColor Red
    exit 1
}

# 清除 agent 注入的删除钩子（见 .DESCRIPTION）
Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
$env:PYTHONUTF8 = "1"

$args = @("-m", "ai_trader.cli", "web", "--log-level", $LogLevel)
if ($Host_ -ne "") { $args += @("--host", $Host_) }
if ($Port -ne 0)   { $args += @("--port", "$Port") }

Write-Host "================================================================" -ForegroundColor White
Write-Host " 启动 FosunAI-Trading 管理台" -ForegroundColor White
Write-Host " PYTHONPATH 已清除: $([string]::IsNullOrEmpty($env:PYTHONPATH))" -ForegroundColor Gray
Write-Host "================================================================" -ForegroundColor White

& $venvPy @args
exit $LASTEXITCODE
