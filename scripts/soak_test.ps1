<#
.SYNOPSIS
    worker 连续运行压测（长期稳定性）。

.DESCRIPTION
    按固定间隔连续调用 scripts\run_worker.ps1，统计：
      * 每轮退出码
      * 心跳是否持续推进
      * 数据库审计链是否始终完整
      * 是否有轮次超时 / 并发跳过

    注意：本脚本只跑模拟盘，不涉及真实下单。默认 5 轮 × 3 秒。

.PARAMETER Iterations
    轮数，默认 5。

.PARAMETER IntervalSeconds
    轮间隔秒数，默认 3。

.PARAMETER MonitorTimeoutSeconds / ScanTimeoutSeconds
    透传给 run_worker.ps1 的单步墙钟上限。
#>
[CmdletBinding()]
param(
    [int]$Iterations = 5,
    [int]$IntervalSeconds = 3,
    [int]$MonitorTimeoutSeconds = 120,
    [int]$ScanTimeoutSeconds = 180
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$worker = Join-Path $Root "scripts\run_worker.ps1"
$heartbeat = Join-Path $Root "data\worker_heartbeat.json"
$py = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $worker)) { Write-Host "[FAIL] 找不到 run_worker.ps1" -ForegroundColor Red; exit 1 }

Write-Host "================================================================" -ForegroundColor White
Write-Host (" soak test: {0} 轮 × 间隔 {1}s" -f $Iterations, $IntervalSeconds) -ForegroundColor White
Write-Host "================================================================" -ForegroundColor White

$results = @()
$lastHeartbeatTs = $null
$monotonicOk = $true

for ($i = 1; $i -le $Iterations; $i++) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & powershell -NoProfile -ExecutionPolicy Bypass -File $worker `
        -MonitorTimeoutSeconds $MonitorTimeoutSeconds -ScanTimeoutSeconds $ScanTimeoutSeconds *> $null
    $code = $LASTEXITCODE
    $sw.Stop()

    $hb = $null
    if (Test-Path $heartbeat) {
        try { $hb = Get-Content $heartbeat -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
    }
    if ($hb) {
        if ($lastHeartbeatTs -and ([datetime]::Parse($hb.ts) -lt [datetime]::Parse($lastHeartbeatTs))) {
            $monotonicOk = $false
        }
        $lastHeartbeatTs = $hb.ts
    }

    $results += [pscustomobject]@{
        Round     = $i
        ExitCode  = $code
        ElapsedMs = [int]$sw.Elapsed.TotalMilliseconds
        Heartbeat = if ($hb) { $hb.status } else { "missing" }
        HbTs      = if ($hb) { $hb.ts } else { "" }
    }
    Write-Host ("  round {0}/{1}  exit={2}  {3,5}ms  heartbeat={4}" -f `
        $i, $Iterations, $code, [int]$sw.Elapsed.TotalMilliseconds,
        ($(if ($hb) { $hb.status } else { "missing" }))) -ForegroundColor Gray

    if ($i -lt $Iterations) { Start-Sleep -Seconds $IntervalSeconds }
}

Write-Host ""
$results | Format-Table -AutoSize | Out-String | Write-Host

$okCount = ($results | Where-Object { $_.ExitCode -eq 0 }).Count
$skipCount = ($results | Where-Object { $_.ExitCode -eq 4 }).Count
$timeoutCount = ($results | Where-Object { $_.ExitCode -eq 3 }).Count
$errCount = ($results | Where-Object { $_.ExitCode -notin @(0, 3, 4) }).Count

Write-Host ("汇总: 成功 {0} / 并发跳过 {1} / 超时 {2} / 失败 {3}" -f `
    $okCount, $skipCount, $timeoutCount, $errCount)

# 审计链校验
$chainOk = "unknown"
if (Test-Path $py) {
    Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
    $out = & $py -c @"
import json, sys
sys.path.insert(0, r'$Root/src')
from ai_trader.audit.journal import AuditJournal
j = AuditJournal(r'$Root/data/audit.db', None)
print(json.dumps(j.verify_chain()))
j.close()
"@ 2>&1
    try {
        $parsed = ($out | Out-String) | ConvertFrom-Json
        $chainOk = $parsed.ok
        Write-Host ("审计链: ok={0} total={1}" -f $parsed.ok, $parsed.total)
    } catch {
        Write-Host "审计链校验失败: $out" -ForegroundColor Yellow
    }
}

if (-not $monotonicOk) {
    Write-Host "[WARN] 心跳时间出现回退（可能有多实例或时钟问题）" -ForegroundColor Yellow
}

if ($errCount -gt 0 -or $timeoutCount -gt 0 -or $chainOk -ne $true) {
    Write-Host "SOAK_FAIL" -ForegroundColor Red
    exit 1
}
Write-Host "SOAK_OK" -ForegroundColor Green
exit 0
