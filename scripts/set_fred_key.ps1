#Requires -Version 5.1
<#
.SYNOPSIS
    安全配置 FRED API Key 到 Windows 用户环境变量并立即验证。

.DESCRIPTION
    密钥通过掩码输入读取：
      * 不显示在屏幕、不进命令历史、不进聊天记录；
      * 只写入用户环境变量 FRED_API_KEY（HKCU\Environment）；
      * 不写入仓库、不写入日志；
      * 只输出「已配置 / 格式校验结果 / 指纹」，绝不回显 key 本身；
      * 配置后立刻用新值启动子进程跑 tools\fred_probe.py（子进程继承本窗口环境）。

.NOTES
    在你自己的 PowerShell 窗口里运行：
        powershell -ExecutionPolicy Bypass -File .\scripts\set_fred_key.ps1
#>

$ErrorActionPreference = 'Stop'
$KeyName  = 'FRED_API_KEY'
$Root     = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python   = Join-Path $Root '.venv\Scripts\python.exe'
$Probe    = Join-Path $Root 'tools\fred_probe.py'

function Get-Fingerprint([string]$value) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $h = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($value))
        return ([BitConverter]::ToString($h) -replace '-', '').Substring(0, 12)
    } finally { $sha.Dispose() }
}

function Test-FredFormat([string]$value) {
    # FRED key = 32 位小写字母数字
    if ($value.Length -ne 32) { return $false }
    return ($value -cmatch '^[a-z0-9]{32}$')
}

Write-Host ''
Write-Host '=== FRED API Key 安全配置 ===' -ForegroundColor Cyan
Write-Host ''

$existing = [Environment]::GetEnvironmentVariable($KeyName, 'User')
if (-not [string]::IsNullOrEmpty($existing)) {
    $okFlag = Test-FredFormat $existing
    Write-Host ("已存在 {0}：长度={1} 格式校验={2} 指纹={3}" -f `
        $KeyName, $existing.Length, $(if ($okFlag) { '通过' } else { '不通过' }), (Get-Fingerprint $existing)) -ForegroundColor Yellow
    Write-Host '  直接回车 = 沿用现有值只做验证；输入新值 = 覆盖。'
    Write-Host '  （不会影响已有的 TWELVE_DATA_API_KEY 等其它变量）'
} else {
    Write-Host ("当前未配置 {0}（不会改动其它已存在的变量）。" -f $KeyName)
}

Write-Host ''
Write-Host '请粘贴 FRED API Key（输入不显示）：' -ForegroundColor Green
$secure = Read-Host -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if ([string]::IsNullOrWhiteSpace($plain)) {
    if ([string]::IsNullOrEmpty($existing)) {
        Write-Host '未输入任何内容，已取消（未做修改）。' -ForegroundColor Red
        exit 2
    }
    Write-Host '未输入新值，沿用现有配置继续验证。' -ForegroundColor Yellow
    $plain = $existing
} else {
    $plain = $plain.Trim()
    if (-not (Test-FredFormat $plain)) {
        Write-Host ''
        Write-Host ("格式校验不通过：长度={0}（FRED 要求 32 位小写字母数字）。" -f $plain.Length) -ForegroundColor Red
        Write-Host '未写入环境变量。请确认复制完整、没有多余空格或大写字母。'
        $plain = $null
        exit 4
    }
    [Environment]::SetEnvironmentVariable($KeyName, $plain, 'User')
    $env:FRED_API_KEY = $plain
    Write-Host ''
    Write-Host '已写入用户环境变量：' -ForegroundColor Green
    Write-Host ("  {0}  长度={1}  格式校验=通过  指纹={2}" -f $KeyName, $plain.Length, (Get-Fingerprint $plain))
    Write-Host '  （指纹 = SHA256 前 12 位，只用于核对配的是哪把 key）'
}

if (-not (Test-Path $Python)) {
    Write-Host ("找不到 Python：{0}" -f $Python) -ForegroundColor Red
    exit 3
}

Write-Host ''
Write-Host '=== 启动子进程实测（新进程读取新环境变量）===' -ForegroundColor Cyan
$env:FRED_API_KEY = $plain
& $Python $Probe
$code = $LASTEXITCODE

$plain = $null
Remove-Variable plain -ErrorAction SilentlyContinue

Write-Host ''
if ($code -eq 0) {
    Write-Host '实测完成。' -ForegroundColor Green
} else {
    Write-Host ("实测未完成（exit={0}）。" -f $code) -ForegroundColor Red
}
Write-Host '提示：新开的终端才能读到用户环境变量；本窗口已临时设置。'
exit $code
