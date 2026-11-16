\xef\xbb\xbf<#
.SYNOPSIS
    单次扫描 + 持仓监控（供 Windows 计划任务每分钟调用）。

.DESCRIPTION
    本轮加固点（对应审计 P1-4/P1-5）：

    1. **互斥**：同一时刻只允许一个 worker（命名 Mutex）。第二个实例立即退出，
       不排队、不叠加 —— 避免两个 worker 同时下单。
    2. **墙钟超时**：每一步都有硬超时，超时即 Kill 子进程并记 `worker_timeout`。
       SDK 自身没有 HTTP timeout（审计 P1-6），这一层是最后防线。
    3. **心跳**：成功后写 `data/worker_heartbeat.json`，供 healthcheck 判断
       "系统是否还在跑"（而不是"进程是否还活着"）。
    4. **顺序**：先 monitor（处理超时撤单/未知单对账/止损），再 scan（新机会）。

    退出码：
      0 = 正常
      1 = 有标的处理失败
      2 = 被紧急停止或门禁阻断
      3 = 超时（已 Kill）
      4 = 另一个 worker 正在运行（本次跳过）

.PARAMETER Symbols
    指定标的（逗号分隔）；留空使用 config.yaml 的 universe。

.PARAMETER MonitorTimeoutSeconds
    monitor 步骤的墙钟上限，默认 120 秒。

.PARAMETER ScanTimeoutSeconds
    scan 步骤的墙钟上限，默认 180 秒。
#>
[CmdletBinding()]
param(
    [string]$Symbols = "",
    [int]$MonitorTimeoutSeconds = 120,
    [int]$ScanTimeoutSeconds = 180,
    # 自检：用同一套超时逻辑跑一个必然超时的子进程，验证 Kill 与退出码是否正确
    [switch]$SelfTest
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "[FAIL] 未找到 .venv" -ForegroundColor Red
    exit 1
}

Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
$env:PYTHONUTF8 = "1"

New-Item -ItemType Directory -Path (Join-Path $Root "logs") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $Root "data") -Force | Out-Null
$logFile = Join-Path $Root ("logs\worker-{0}.log" -f (Get-Date -Format "yyyyMMdd"))
$heartbeat = Join-Path $Root "data\worker_heartbeat.json"

function Write-Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $logFile -Value $line -Encoding utf8
    Write-Host $line
}

function Write-Heartbeat($status, $detail) {
    $payload = [ordered]@{
        ts      = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
        status  = $status
        detail  = $detail
        pid     = $PID
        host    = $env:COMPUTERNAME
        symbols = $Symbols
    }
    try {
        $payload | ConvertTo-Json -Depth 4 | Set-Content -Path $heartbeat -Encoding utf8
    } catch {
        Write-Log "heartbeat write failed: $($_.Exception.Message)"
    }
}

function Invoke-StepWithTimeout {
    param(
        [string[]]$ArgList,
        [int]$TimeoutSeconds,
        [string]$Label
    )
    $stamp = Get-Date -Format "HHmmss"
    $outFile = Join-Path $Root ("logs\worker-{0}-{1}.out" -f $Label, $stamp)
    $errFile = Join-Path $Root ("logs\worker-{0}-{1}.err" -f $Label, $stamp)

    # 说明：这里不用 Start-Process。
    # 本机环境同时存在 HTTP_PROXY 与 http_proxy，Start-Process 构造环境字典时
    # 会抛 "已添加项。字典中的关键字…" —— 连 Env: 驱动器都不可用。
    # 直接用 System.Diagnostics.Process：UseShellExecute=false 时继承环境块，
    # 不建字典，既能拿到可靠 ExitCode，也能精确控制超时。
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $venvPy
    $psi.Arguments = ($ArgList | ForEach-Object {
        if ($_ -match '\s') { '"{0}"' -f $_ } else { $_ }
    }) -join ' '
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $false
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = $Root
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi

    $exited = $false
    $timedOut = $false
    $code = $null
    try {
        $null = $proc.Start()
        # 先异步读，避免管道缓冲区写满导致子进程阻塞（经典死锁）
        $outTask = $proc.StandardOutput.ReadToEndAsync()
        $errTask = $proc.StandardError.ReadToEndAsync()
        $exited = $proc.WaitForExit($TimeoutSeconds * 1000)
        if ($exited) {
            $code = $proc.ExitCode
        } else {
            $timedOut = $true
            Write-Log "$Label TIMEOUT after ${TimeoutSeconds}s (pid=$($proc.Id)) -> killing"
            try { $proc.Kill() } catch { Write-Log "kill failed: $($_.Exception.Message)" }
            try { $null = $proc.WaitForExit(5000) } catch { }
            $code = 124
        }
        $stdout = ""
        $stderr = ""
        try { $stdout = $outTask.GetAwaiter().GetResult() } catch { }
        try { $stderr = $errTask.GetAwaiter().GetResult() } catch { }
    } catch {
        Write-Log "$Label failed to start: $($_.Exception.Message)"
        $code = 125
        $stdout = ""
        $stderr = $_.Exception.Message
    } finally {
        try { $proc.Dispose() } catch { }
    }

    Set-Content -Path $outFile -Value $stdout -Encoding utf8
    Set-Content -Path $errFile -Value $stderr -Encoding utf8
    if (Test-Path $outFile) {
        Add-Content -Path $logFile -Value (Get-Content $outFile -Raw) -Encoding utf8
    }
    if ((Test-Path $errFile) -and ((Get-Item $errFile).Length -gt 0)) {
        Add-Content -Path $logFile -Value (Get-Content $errFile -Raw) -Encoding utf8
    }
    return [pscustomobject]@{ Code = $code; TimedOut = (-not $exited); Label = $Label }
}

# ---- 自检：验证超时监控真的能 Kill 并返回 124 ----
if ($SelfTest) {
    Write-Log "=== SELFTEST start: child sleeps 5s, timeout is 1s ==="
    $probe = Invoke-StepWithTimeout `
        -ArgList @("-c", "import time; time.sleep(5); print('SHOULD_NOT_PRINT')") `
        -TimeoutSeconds 1 -Label "selftest"
    Write-Log ("selftest code={0} timedOut={1}" -f $probe.Code, $probe.TimedOut)
    if ($probe.TimedOut -and $probe.Code -eq 124) {
        Write-Host "TIMEOUT_OK"
        exit 0
    }
    Write-Host "TIMEOUT_FAIL code=$($probe.Code) timedOut=$($probe.TimedOut)"
    exit 1
}

# ---- 互斥：同一时刻只能有一个 worker ----
$mutexName = "Global\FosunAI-Trading-Worker"
$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$owned = $false
try {
    $owned = $mutex.WaitOne(0)
    if (-not $owned) {
        Write-Log "=== worker skipped: another instance is running ==="
        Write-Heartbeat "skipped_concurrent" "another worker holds the mutex"
        exit 4
    }

    Write-Log "=== worker start (symbols='$Symbols' monitor<=${MonitorTimeoutSeconds}s scan<=${ScanTimeoutSeconds}s) ==="
    Write-Heartbeat "running" "started"

    # 0) 配置守卫（US-P1）：先确认这份配置还能安全交易，再碰任何交易步骤。
    #    不通过 → 直接退出，**不进入 monitor/scan**（避免"规则层被关掉却照常交易"）。
    $guard = Invoke-StepWithTimeout -ArgList @("-m", "ai_trader.cli", "guard") `
        -TimeoutSeconds 60 -Label "guard"
    Write-Log ("guard exit={0} timeout={1}" -f $guard.Code, $guard.TimedOut)
    if ($guard.TimedOut) {
        Write-Log "=== worker timeout in guard (exit 3) ==="
        Write-Heartbeat "timeout" "guard exceeded 60s"
        exit 3
    }
    if ($guard.Code -ne 0) {
        Write-Log "=== worker refused: config guard failed (exit 4) ==="
        Write-Heartbeat "guard_failed" "config guard rejected the configuration"
        exit 4
    }

    # 1) 先监控：超时撤单 / 未知单对账 / 止损止盈
    $monitor = Invoke-StepWithTimeout -ArgList @("-m", "ai_trader.cli", "monitor") `
        -TimeoutSeconds $MonitorTimeoutSeconds -Label "monitor"
    Write-Log ("monitor exit={0} timeout={1}" -f $monitor.Code, $monitor.TimedOut)

    if ($monitor.TimedOut) {
        Write-Log "=== worker timeout in monitor (exit 3) ==="
        Write-Heartbeat "timeout" "monitor exceeded ${MonitorTimeoutSeconds}s"
        exit 3
    }

    # 2) 再扫描
    $scanArgs = @("-m", "ai_trader.cli", "scan")
    if ($Symbols -ne "") { $scanArgs += @("--symbols", $Symbols) }
    $scan = Invoke-StepWithTimeout -ArgList $scanArgs `
        -TimeoutSeconds $ScanTimeoutSeconds -Label "scan"
    Write-Log ("scan exit={0} timeout={1}" -f $scan.Code, $scan.TimedOut)

    if ($scan.TimedOut) {
        Write-Log "=== worker timeout in scan (exit 3) ==="
        Write-Heartbeat "timeout" "scan exceeded ${ScanTimeoutSeconds}s"
        exit 3
    }

    if ($monitor.Code -eq 2 -or $scan.Code -eq 2) {
        Write-Log "=== worker halted (kill switch or gate) ==="
        Write-Heartbeat "halted" "kill switch or gate blocked"
        exit 2
    }
    if ($monitor.Code -ne 0 -or $scan.Code -ne 0) {
        Write-Log "=== worker finished with errors ==="
        Write-Heartbeat "error" ("monitor={0} scan={1}" -f $monitor.Code, $scan.Code)
        exit 1
    }
    Write-Log "=== worker ok ==="
    Write-Heartbeat "ok" "monitor+scan completed"
    exit 0
}
finally {
    if ($owned) {
        try { $mutex.ReleaseMutex() } catch { }
    }
    try { $mutex.Dispose() } catch { }
}
