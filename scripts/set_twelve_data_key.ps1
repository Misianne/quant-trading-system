#Requires -Version 5.1
<#
.SYNOPSIS
    安全配置 Twelve Data API Key 到 Windows 用户环境变量。

.DESCRIPTION
    密钥通过掩码输入（Read-Host -AsSecureString）读取：
      * 不出现在屏幕、命令历史、聊天记录里；
      * 只写入用户环境变量 TWELVE_DATA_API_KEY（HKCU\Environment）；
      * 不写入任何文件、不写入本仓库、不进日志；
      * 配置完成后立刻调用官方 API 验证，并把脱敏后的证据写到 reports\marketdata_probe\。

.NOTES
    在**你自己的** PowerShell 窗口里运行：
        powershell -ExecutionPolicy Bypass -File .\scripts\set_twelve_data_key.ps1
#>

$ErrorActionPreference = 'Stop'
$KeyName = 'TWELVE_DATA_API_KEY'
$Root    = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python  = Join-Path $Root '.venv\Scripts\python.exe'
$Probe   = Join-Path $Root 'tools\twelvedata_probe.py'

function Get-Fingerprint([string]$value) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($value)
        $hash  = $sha.ComputeHash($bytes)
        return ([BitConverter]::ToString($hash) -replace '-', '').Substring(0, 12)
    } finally { $sha.Dispose() }
}

Write-Host ''
Write-Host '=== Twelve Data API Key 安全配置 ===' -ForegroundColor Cyan
Write-Host ''

# ---- 1) 检查现状，不覆盖其他密钥 ----
$existing = [Environment]::GetEnvironmentVariable($KeyName, 'User')
if (-not [string]::IsNullOrEmpty($existing)) {
    Write-Host ("已存在 {0}：" -f $KeyName) -ForegroundColor Yellow
    Write-Host ("  length={0}  fingerprint={1}" -f $existing.Length, (Get-Fingerprint $existing))
    Write-Host '  如需替换，请输入新 key；直接回车则保留现有配置并只做验证。'
} else {
    Write-Host ("当前未配置 {0}（不会影响已有的 FSOPENAPI_* / AI_* 等变量）。" -f $KeyName)
}

# ---- 2) 掩码输入 ----
Write-Host ''
Write-Host '请粘贴 API Key（输入时不显示，也不会进入命令历史）：' -ForegroundColor Green
$secure = Read-Host -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if ([string]::IsNullOrWhiteSpace($plain)) {
    if ([string]::IsNullOrEmpty($existing)) {
        Write-Host '未输入任何内容，已取消（未做任何修改）。' -ForegroundColor Red
        exit 2
    }
    Write-Host '未输入新 key，沿用现有配置继续验证。' -ForegroundColor Yellow
    $plain = $existing
} else {
    $plain = $plain.Trim()
    if ($plain.Length -lt 16) {
        Write-Host ("警告：key 长度只有 {0}，Twelve Data 的 key 通常为 32 位。仍会按输入配置。" -f $plain.Length) -ForegroundColor Yellow
    }
    # ---- 3) 写入用户环境变量（HKCU\Environment），并让当前进程立即生效 ----
    [Environment]::SetEnvironmentVariable($KeyName, $plain, 'User')
    $env:TWELVE_DATA_API_KEY = $plain
    Write-Host ''
    Write-Host '已写入用户环境变量：' -ForegroundColor Green
    Write-Host ("  {0}  length={1}  fingerprint={2}" -f $KeyName, $plain.Length, (Get-Fingerprint $plain))
    Write-Host '  （指纹 = SHA256 前 12 位，可用来核对"配的是哪把 key"，无法反推出 key 本身）'
}

# ---- 4) 立即验证 ----
if (-not (Test-Path $Python)) {
    Write-Host ("找不到 Python：{0}" -f $Python) -ForegroundColor Red
    Write-Host '已写入环境变量，但未能自动验证；可稍后手动运行 tools\twelvedata_probe.py。'
    exit 3
}

Write-Host ''
Write-Host '=== 调用官方 API 验证 ===' -ForegroundColor Cyan
$env:TWELVE_DATA_API_KEY = $plain
& $Python $Probe
$code = $LASTEXITCODE

$plain = $null
Remove-Variable plain -ErrorAction SilentlyContinue

Write-Host ''
if ($code -eq 0) {
    Write-Host '验证通过。' -ForegroundColor Green
} else {
    Write-Host ("验证未通过（exit={0}）。请检查 key 是否正确、额度是否用尽。" -f $code) -ForegroundColor Red
}
Write-Host '提示：新开的终端才能读到用户环境变量；本窗口已临时设置。'
exit $code
