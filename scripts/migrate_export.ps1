<#
.SYNOPSIS
    迁移导出：把密钥、配置与业务数据打包成一个可搬到新 VPS 的迁移包。

.DESCRIPTION
    产出 dist\migrate-yyyyMMdd-HHmmss\：
      secrets.enc       口令派生(scrypt) + AES-256-GCM 加密的密钥包
      bundle.zip        config.yaml / audit.db / paper_state.json / stops.json
      MIGRATE.txt       目标机上的还原步骤

    secrets.enc **不是** DPAPI，因此可在任何 Windows 用户/机器上用同一口令解开。

.PARAMETER Passphrase
    加密口令（必填，至少 12 字符）。请妥善保管，遗失无法恢复。

.PARAMETER OutDir
    输出目录，默认 dist。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Passphrase,
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if ($Passphrase.Length -lt 12) {
    Write-Host "[FAIL] 口令过短（至少 12 字符）" -ForegroundColor Red
    exit 1
}

if ($OutDir -eq "") { $OutDir = Join-Path $Root "dist" }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dest = Join-Path $OutDir "migrate-$stamp"
New-Item -ItemType Directory -Path $dest -Force | Out-Null

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "[FAIL] 未找到 .venv" -ForegroundColor Red
    exit 1
}
Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue

Write-Host "[1/3] 导出加密密钥包" -ForegroundColor Cyan
# 口令通过环境变量传入，避免出现在进程命令行（任务管理器可见）
$env:VAULT_PASSPHRASE = $Passphrase
$encPath = Join-Path $dest "secrets.enc"
# 口令只通过环境变量传入：绝不放在命令行（任务管理器 / 进程审计可见）
& $venvPy -m ai_trader.cli vault export --out $encPath
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] 密钥导出失败（保险箱可能为空或口令不符）" -ForegroundColor Red
    exit 1
}
$encSize = [math]::Round((Get-Item $encPath).Length / 1KB, 1)
Write-Host ("  -> secrets.enc ({0} KB)" -f $encSize) -ForegroundColor Green
Remove-Item Env:\VAULT_PASSPHRASE -ErrorAction SilentlyContinue

Write-Host "[2/3] 打包业务数据" -ForegroundColor Cyan
$stage = Join-Path $env:TEMP ("fosunai-migrate-" + $stamp)
New-Item -ItemType Directory -Path $stage -Force | Out-Null
$items = @("config\config.yaml", "data\audit.db", "data\paper_state.json", "data\stops.json")
foreach ($i in $items) {
    $p = Join-Path $Root $i
    if (Test-Path $p) {
        $sub = Split-Path $i -Parent
        $d = Join-Path $stage $sub
        New-Item -ItemType Directory -Path $d -Force | Out-Null
        Copy-Item $p -Destination $d -Force
        Write-Host "  + $i" -ForegroundColor Gray
    }
}
$bundle = Join-Path $dest "bundle.zip"
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $bundle -Force
Remove-Item $stage -Recurse -Force
Write-Host "  -> bundle.zip" -ForegroundColor Green

Write-Host "[3/3] 生成迁移说明" -ForegroundColor Cyan
$doc = @"
FosunAI-Trading 迁移包
======================
生成时间: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
源主机  : $env:COMPUTERNAME
内容    : secrets.enc（口令加密密钥）、bundle.zip（配置与业务数据）

在目标机上执行
--------------
1. 安装 Python 3.10+、Git
2. 获取代码（Git 或拷贝本工程目录，**不含** .venv / data / .env）
3. 引导环境：
     powershell -ExecutionPolicy Bypass -File scripts\bootstrap_windows.ps1 -SkipTests
4. 还原业务数据（覆盖前请先备份目标机现有 data\）：
     Expand-Archive bundle.zip -DestinationPath . -Force
5. 还原密钥（会要求输入同一口令）：
     $env:VAULT_PASSPHRASE = "<口令>"          # 先设到当前会话环境
     .venv\Scripts\python.exe -m ai_trader.cli vault import --infile secrets.enc
     Remove-Item Env://VAULT_PASSPHRASE
6. 校验：
     .venv\Scripts\python.exe -m ai_trader.cli check-env
     .venv\Scripts\python.exe -m ai_trader.cli status
7. 启动：
     powershell -ExecutionPolicy Bypass -File scripts\run_web.ps1

注意
----
* secrets.enc 用 scrypt+AES-GCM 保护，口令遗失无法恢复。
* 迁移完成后请删除本目录，不要长期留存明文口令或迁移包。
* 目标机默认仍是 PAPER_TRADING=true；如要真实交易，按 DEPLOYMENT.md 逐项确认。
"@
$doc | Out-File -FilePath (Join-Path $dest "MIGRATE.txt") -Encoding utf8
Write-Host "  -> MIGRATE.txt" -ForegroundColor Green

Write-Host "`n迁移包已生成: $dest" -ForegroundColor Green
exit 0
