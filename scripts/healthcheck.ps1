<#
.SYNOPSIS
    健康检查：调用 /api/status 并断言关键安全不变量。

.DESCRIPTION
    适合放在计划任务里，异常时返回非 0 退出码，便于外部监控告警。
    检查项：
      1. Web 服务可访问且返回 200
      2. 审计哈希链完整
      3. paper 模式下 real_order_authorized 必须为 false（安全不变量）
      4. 紧急停止状态会打印告警（不视为故障）

.PARAMETER Url
    管理台地址，默认 http://127.0.0.1:8781。

.PARAMETER Password
    WEB_ADMIN_PASSWORD（如需鉴权）。
#>
[CmdletBinding()]
param(
    [string]$Url = "http://127.0.0.1:8781",
    [string]$Password = ""
)

$ErrorActionPreference = "Stop"
$fail = 0

function Check($name, $ok, $detail) {
    if ($ok) {
        Write-Host ("  [ OK ] {0}: {1}" -f $name, $detail) -ForegroundColor Green
    } else {
        Write-Host ("  [FAIL] {0}: {1}" -f $name, $detail) -ForegroundColor Red
        $script:fail++
    }
}

Write-Host "健康检查 $Url/api/status" -ForegroundColor Cyan

# 本机探测必须绕过系统/环境代理（否则 127.0.0.1 会被代理拦截）
$origProxy = [System.Net.WebRequest]::DefaultWebProxy
try {
    [System.Net.WebRequest]::DefaultWebProxy = New-Object System.Net.WebProxy
    $web = $null
    try {
        $web = Invoke-RestMethod -Uri "$Url/api/status" -TimeoutSec 10 -Method Get
    } catch {
        Check "http" $false $_.Exception.Message
        exit 1
    }
} finally {
    [System.Net.WebRequest]::DefaultWebProxy = $origProxy
}

Check "http" ($null -ne $web) "已连通"
Check "audit_chain" ($web.audit.chain.ok -eq $true) "total=$($web.audit.chain.total)"
Check "paper_mode" ($web.safety.paper_trading -eq $true) "paper_trading=$($web.safety.paper_trading)"

if ($web.safety.paper_trading -eq $true) {
    Check "no_real_order_in_paper" ($web.safety.real_order_authorized -eq $false) `
        "real_order_authorized=$($web.safety.real_order_authorized)"
}

Check "broker_kind" ($web.broker.kind -eq "paper" -or $web.broker.connected -eq $true) `
    "kind=$($web.broker.kind) connected=$($web.broker.connected)"

if ($web.safety.kill_switch -eq $true) {
    Write-Host ("  [WARN] 紧急停止处于触发状态（来源={0}，原因={1}）" -f `
        $web.safety.halt_source, $web.safety.halt_reason) -ForegroundColor Yellow
}
if ($web.safety.halt_store_failed -eq $true) {
    Check "halt_storage" $false "急停哨兵异常：停止状态可能不可靠"
}

# ---- worker 心跳：判断"系统还在跑"，而不是"进程还活着" ----
$heartbeatPath = Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) "data\worker_heartbeat.json"
$maxStaleMinutes = 10
if (Test-Path $heartbeatPath) {
    try {
        $hb = Get-Content $heartbeatPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $ageMin = ((Get-Date) - [datetime]::Parse($hb.ts)).TotalMinutes
        $fresh = $ageMin -le $maxStaleMinutes
        Check "worker_heartbeat" $fresh ("status={0} age={1:N1}min" -f $hb.status, $ageMin)
        if (-not $fresh) {
            Write-Host "  [WARN] worker 心跳已过期，检查计划任务与 logs\worker-*.log" -ForegroundColor Yellow
        }
    } catch {
        Check "worker_heartbeat" $false "心跳文件无法解析：$($_.Exception.Message)"
    }
} else {
    Write-Host "  [WARN] 未找到 worker 心跳文件（worker 可能从未运行）" -ForegroundColor Yellow
}

Write-Host ""
if ($fail -gt 0) {
    Write-Host "健康检查失败：$fail 项" -ForegroundColor Red
    exit 1
}
Write-Host "健康检查通过。" -ForegroundColor Green
exit 0
