# 星财富 · 本地 Paper/Replay 状态检查
#
# 输出：进程是否在、心跳是否新鲜、最近一次运行报告摘要。
# 判定：RUNNING（进程在 + 心跳新鲜）/ STALLED（进程在但心跳过期）/ DEAD（进程不在）

[CmdletBinding()]
param(
    [int]$StaleSeconds = 60
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$RunRoot = Join-Path $Root "data\paper_runs"
$PidFile = Join-Path $RunRoot "supervisor.pid"

$result = [ordered]@{
    root = $RunRoot
    pid = $null
    pid_alive = $false
    verdict = "UNKNOWN"
    heartbeat = $null
    heartbeat_age_s = $null
    latest_run = $null
    report = $null
}

if (Test-Path $PidFile) {
    $raw = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($raw) {
        $result.pid = [int]$raw
        $result.pid_alive = [bool](Get-Process -Id ([int]$raw) -ErrorAction SilentlyContinue)
    }
}

$runDirs = Get-ChildItem -Path $RunRoot -Directory -Filter "run-*" -Recurse -ErrorAction SilentlyContinue |
           Sort-Object LastWriteTime -Descending
if ($runDirs -and $runDirs.Count -gt 0) {
    $latest = $runDirs[0]
    $result.latest_run = $latest.FullName
    $hbPath = Join-Path $latest.FullName "heartbeat.json"
    if (Test-Path $hbPath) {
        try {
            $hb = Get-Content $hbPath -Raw | ConvertFrom-Json
            $result.heartbeat = $hb
            $at = [datetimeoffset]::Parse($hb.at)
            $age = ([datetimeoffset]::Now - $at).TotalSeconds
            $result.heartbeat_age_s = [math]::Round($age, 1)
            if ($result.pid_alive -and $age -lt $StaleSeconds) {
                $result.verdict = "RUNNING"
            } elseif ($result.pid_alive) {
                $result.verdict = "STALLED"
            } else {
                $result.verdict = "DEAD"
            }
        } catch {
            $result.verdict = "BAD_HEARTBEAT"
        }
    } elseif ($result.pid_alive) {
        $result.verdict = "STARTING"
    } else {
        $result.verdict = "DEAD"
    }
    $repPath = Join-Path $latest.FullName "run_report.json"
    if (Test-Path $repPath) {
        try { $result.report = Get-Content $repPath -Raw | ConvertFrom-Json } catch { }
    }
} else {
    $result.verdict = "NO_RUNS"
}

$result | ConvertTo-Json -Depth 6

switch ($result.verdict) {
    "RUNNING" { exit 0 }
    "STALLED" { exit 5 }
    "DEAD"    { exit 6 }
    default   { exit 0 }
}
