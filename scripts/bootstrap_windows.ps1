<#
.SYNOPSIS
    FosunAI-Trading 一键引导（Windows VPS / 本机）。

.DESCRIPTION
    1. 检查 Python / Git
    2. 创建或复用 .venv
    3. 安装依赖 + 以 editable 方式安装官方 SDK 与本工程
    4. 从模板生成 config\config.yaml（已存在则跳过，绝不覆盖）
    5. 生成 .env（已存在则跳过）
    6. 初始化密钥保险箱目录
    7. 运行测试（可跳过）
    8. 打印后续步骤

    本脚本 **不会** 开启任何真实交易开关，也不会写入任何假凭证。

.PARAMETER DryRun
    只打印将要执行的动作，不做任何修改。

.PARAMETER SkipTests
    跳过 pytest。

.PARAMETER MinPython
    最低 Python 版本，默认 3.10。
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipTests,
    [switch]$SkipAcl,
    [string]$MinPython = "3.10"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Write-Step($msg) {
    Write-Host ""
    Write-Host "[STEP] $msg" -ForegroundColor Cyan
}
function Write-Ok($msg)   { Write-Host "  [ OK ] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red }
function Write-Plan($msg) { Write-Host "  [PLAN] $msg" -ForegroundColor DarkGray }

function Invoke-Step {
    param([string]$Command, [string]$Description)
    if ($DryRun) { Write-Plan "$Description  ->  $Command"; return }
    Write-Host "  > $Command" -ForegroundColor DarkGray
    Invoke-Expression $Command
    if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) {
        throw "命令失败（exit $LASTEXITCODE）: $Command"
    }
}

Write-Host "================================================================" -ForegroundColor White
Write-Host " FosunAI-Trading · Windows 引导脚本" -ForegroundColor White
Write-Host " 工作目录: $Root" -ForegroundColor White
if ($DryRun) { Write-Host " 模式: DRY-RUN（不做任何修改）" -ForegroundColor Yellow }
Write-Host "================================================================" -ForegroundColor White

# ---------------------------------------------------------------- 1. 环境检查
Write-Step "1/8 检查 Python 与 Git"

function Get-PythonInfo {
    param([string]$Exe, [string[]]$PrefixArgs = @())
    try {
        $out = & $Exe @PrefixArgs -c "import sys;print('%d.%d.%d' % sys.version_info[:3])" 2>&1
        if ($LASTEXITCODE -ne 0) { return $null }
        $text = ($out | Out-String).Trim()
        if ($text -match "^(\d+)\.(\d+)\.(\d+)$") {
            return [pscustomobject]@{ Exe = $Exe; Prefix = $PrefixArgs; Version = $text }
        }
    } catch { }
    return $null
}

$pyInfo = $null
$venvCandidate = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $venvCandidate) {
    $pyInfo = Get-PythonInfo -Exe $venvCandidate
    if ($pyInfo) { Write-Ok "复用 .venv 中的 Python $($pyInfo.Version)" }
}
if (-not $pyInfo) {
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            $info = Get-PythonInfo -Exe $cmd.Source
            # Windows 应用商店的 python.exe 存根通常返回非 0 或空版本，上面已过滤
            if ($info) { $pyInfo = $info; break }
        }
    }
}
if (-not $pyInfo) {
    $cmd = Get-Command "py" -ErrorAction SilentlyContinue
    if ($cmd) { $pyInfo = Get-PythonInfo -Exe $cmd.Source -PrefixArgs @("-3") }
}
if (-not $pyInfo) {
    Write-Fail "未找到可用的 Python。请安装 Python $MinPython+ 并加入 PATH（注意排除 Microsoft Store 的存根版本）。"
    exit 1
}

$pyExe = $pyInfo.Exe
$pyPre = $pyInfo.Prefix
Write-Ok "Python: $($pyInfo.Version)  路径: $pyExe"

$verParts = $pyInfo.Version.Split(".")
$needParts = $MinPython.Split(".")
$major = [int]$verParts[0]; $minor = [int]$verParts[1]
$needMajor = [int]$needParts[0]; $needMinor = [int]$needParts[1]
if ($major -lt $needMajor -or ($major -eq $needMajor -and $minor -lt $needMinor)) {
    Write-Fail "Python 版本过低（$($pyInfo.Version) < $MinPython）"
    exit 1
}

try { $gitVer = (& git --version) -join ""; Write-Ok "Git: $gitVer" }
catch { Write-Warn2 "未检测到 Git（迁移/升级时需要）" }

try {
    Add-Type -AssemblyName System.Security -ErrorAction Stop
    $probe = [System.Security.Cryptography.ProtectedData]::Protect(
        [byte[]](1, 2, 3), [byte[]](4, 5, 6), "CurrentUser")
    Write-Ok ("DPAPI 可用（{0} 字节密文）" -f $probe.Length)
} catch {
    Write-Warn2 "DPAPI 探测失败：$($_.Exception.Message)（保险箱将退回口令加密）"
}

# ---------------------------------------------------------------- 2. 虚拟环境
Write-Step "2/8 准备虚拟环境 .venv"
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    Write-Ok "复用已存在的 .venv"
} else {
    $pre = ""
    if ($pyPre -and $pyPre.Count -gt 0) { $pre = ($pyPre -join " ") + " " }
    Invoke-Step ("& `"{0}`" {1}-m venv `"{2}`"" -f $pyExe, $pre, (Join-Path $Root ".venv")) "创建虚拟环境"
    if (-not $DryRun) { Write-Ok "已创建 .venv" }
}

# ---------------------------------------------------------------- 3. 安装依赖
Write-Step "3/8 安装依赖"
Invoke-Step "& `"$venvPy`" -m pip install --upgrade pip -q" "升级 pip"
Invoke-Step "& `"$venvPy`" -m pip install -r requirements.txt" "安装 requirements.txt"
Invoke-Step "& `"$venvPy`" -m pip install -e ./sdk_src" "安装官方 SDK (editable)"
Invoke-Step "& `"$venvPy`" -m pip install -e ." "安装本工程 (editable)"

# ---------------------------------------------------------------- 4. 配置
Write-Step "4/8 生成配置文件"
$cfgPath = Join-Path $Root "config\config.yaml"
if (Test-Path $cfgPath) {
    Write-Ok "config\config.yaml 已存在，保持不变（不会覆盖你的修改）"
} else {
    Write-Warn2 "config\config.yaml 不存在 —— 请从 config\config.template.yaml 复制"
    Write-Plan "Copy-Item config\config.template.yaml config\config.yaml"
}

$envPath = Join-Path $Root ".env"
$envExample = Join-Path $Root ".env.example"
if (Test-Path $envPath) {
    Write-Ok ".env 已存在，保持不变"
} elseif (Test-Path $envExample) {
    if (-not $DryRun) {
        Copy-Item $envExample $envPath
        Write-Ok "已从 .env.example 生成 .env（请手工填写凭证，脚本不会写入任何假值）"
    } else {
        Write-Plan "Copy-Item .env.example .env"
    }
} else {
    Write-Warn2 "缺少 .env.example"
}

# ---------------------------------------------------------------- 5. 目录
Write-Step "5/8 初始化数据与日志目录"
foreach ($d in @("data", "logs", "config")) {
    $full = Join-Path $Root $d
    if (Test-Path $full) { Write-Ok "$d\ 已存在" }
    elseif ($DryRun) { Write-Plan "New-Item -ItemType Directory $d" }
    else { New-Item -ItemType Directory -Path $full -Force | Out-Null; Write-Ok "创建 $d\" }
}

# ---- 5b. 收紧敏感文件权限（审计 P1-12）----
# 默认继承的 ACL 里 "Authenticated Users:(M)" 意味着任意本地用户都能改写 .env
# （进而把 LIVE_TRADING 改成 true）。这里去掉继承，只保留当前用户。
if ($SkipAcl) {
    Write-Warn2 "已按参数跳过 ACL 收紧"
} else {
    function Protect-Path {
        param([string]$Path, [switch]$IsDir)
        if (-not (Test-Path $Path)) { return }
        if ($DryRun) { Write-Plan "icacls `"$Path`" /inheritance:r /grant:r `"$env:USERNAME`""; return }
        $grant = if ($IsDir) { "$env:USERNAME:(OI)(CI)F" } else { "$env:USERNAME:F" }
        try {
            & icacls $Path /inheritance:r /grant:r $grant *> $null
            if ($LASTEXITCODE -eq 0) { Write-Ok ("已收紧权限: {0}" -f (Split-Path $Path -Leaf)) }
            else { Write-Warn2 ("icacls 返回 {0}: {1}" -f $LASTEXITCODE, $Path) }
        } catch {
            Write-Warn2 ("收紧权限失败 {0}: {1}" -f $Path, $_.Exception.Message)
        }
    }
    Protect-Path -Path $envPath
    Protect-Path -Path (Join-Path $Root "data") -IsDir
    Protect-Path -Path (Join-Path $Root "data\vault.json")
    Protect-Path -Path (Join-Path $Root "data\HALT.json")
}

# ---------------------------------------------------------------- 6. 自检
Write-Step "6/8 运行环境自检"
Invoke-Step "& `"$venvPy`" -m ai_trader.cli check-env" "打印门禁与凭证状态"

# ---------------------------------------------------------------- 7. 测试
Write-Step "7/8 运行测试套件"
if ($SkipTests) {
    Write-Warn2 "已按参数跳过测试"
} else {
    Invoke-Step "& `"$venvPy`" -m pytest -q" "pytest"
}

# ---------------------------------------------------------------- 8. 后续
Write-Step "8/8 后续步骤"
Write-Host @"
  1) 填写凭证（二选一，推荐保险箱）：
       .venv\Scripts\python.exe -m ai_trader.cli vault set --name FSOPENAPI_API_KEY --value <你的 key>
       .venv\Scripts\python.exe -m ai_trader.cli vault set --name FSOPENAPI_SERVER_PUBLIC_KEY --value "<PEM>"
       .venv\Scripts\python.exe -m ai_trader.cli vault set --name FSOPENAPI_CLIENT_PRIVATE_KEY --value "<PEM>"
     或直接编辑 .env。

  2) 启动管理台：
       powershell -ExecutionPolicy Bypass -File scripts\run_web.ps1

  3) 执行一次模拟扫描：
       .venv\Scripts\python.exe -m ai_trader.cli scan

  4) 真实交易默认关闭。开启前请阅读 RISK_CONTROL.md 与 DEPLOYMENT.md 的"开启真实交易"章节，
     并确认已获得星财富开放平台的交易权限。

  当前安全状态：LIVE_TRADING=false / ORDER_EXECUTION_ENABLED=false / PAPER_TRADING=true
"@ -ForegroundColor Gray
Write-Host "`n引导完成。" -ForegroundColor Green
