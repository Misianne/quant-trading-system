# 星财富 · 本地 Paper/Replay 安全停止脚本
#
# 语义：**先优雅、后强制**。
#   1) 写 stop.request，让主循环在当前 tick 结束后自行退出（会落盘、会释放组件）
#   2) 等待最多 -TimeoutSeconds 秒
#   3) 仍未退出才 Stop-Process（并明确记录"强制终止"，不假装是优雅停止）

[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 30,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$RunRoot = Join-Path $Root "data\paper_runs"
$PidFile = Join-Path $RunRoot "supervisor.pid"

if (-not (Test-Path $PidFile)) {
    Write-Host "[info] 没有 PID 文件，视为已停止。"
    exit 0
}

$raw = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
if (-not $raw) {
    Write-Host "[info] PID 文件为空，清理后视为已停止。"
    Remove-Item $PidFile -Force
    exit 0
}

$ProcId = [int]$raw
$proc = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
if (-not $proc) {
    Write-Host "[info] 进程 $ProcId 不存在（已退出），清理 PID 文件。"
    Remove-Item $PidFile -Force
    exit 0
}

# 找到该进程对应的运行目录（最新的 run-soak-*），写停止请求
$runDirs = Get-ChildItem -Path $RunRoot -Directory -Filter "run-soak-*" -ErrorAction SilentlyContinue |
           Sort-Object LastWriteTime -Descending
if ($runDirs -and $runDirs.Count -gt 0) {
    $req = Join-Path $runDirs[0].FullName "stop.request"
    "requested_at=$(Get-Date -Format o)`nrequested_by=paper_stop.ps1`npid=$ProcId" |
        Out-File -FilePath $req -Encoding utf8
    Write-Host "[stop] 已写入优雅停止请求：$req"
} else {
    Write-Host "[warn] 未找到 run-soak-* 目录，直接等待进程退出。"
}

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    $still = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
    if (-not $still) {
        Write-Host "[OK] 进程 $ProcId 已优雅退出。" -ForegroundColor Green
        Remove-Item $PidFile -Force
        exit 0
    }
    Start-Sleep -Milliseconds 500
}

if ($Force) {
    Write-Host "[WARN] 超时 ${TimeoutSeconds}s，强制终止 PID=$ProcId（非优雅停止，已记录）。" -ForegroundColor Yellow
    Stop-Process -Id $ProcId -Force -ErrorAction SilentlyContinue
    Add-Content -Path (Join-Path $RunRoot "forced_kills.log") -Value (
        "{0} forced kill pid={1}" -f (Get-Date -Format o), $ProcId)
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    exit 4
}

Write-Host "[TIMEOUT] ${TimeoutSeconds}s 内未退出。加 -Force 可强制终止。" -ForegroundColor Red
exit 3
