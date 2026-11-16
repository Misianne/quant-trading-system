<#
.SYNOPSIS
    备份数据、日志、配置与（加密的）密钥保险箱。

.DESCRIPTION
    产出 data\backups\backup-yyyyMMdd-HHmmss.zip，内含：
      config\config.yaml（不含密钥）
      data\audit.db, data\paper_state.json, data\stops.json
      data\vault.json（DPAPI 密文，仅当前 Windows 用户可解）
      logs\ 最近 7 天日志

    ⚠️ vault.json 用 DPAPI 加密，**换机器后无法解密**。跨机器迁移请使用
    migrate_export.ps1（口令派生的加密包）。

.PARAMETER OutDir
    备份输出目录，默认 data\backups。

.PARAMETER Keep
    保留最近 N 份备份，默认 14；0 表示不清理。
#>
[CmdletBinding()]
param(
    [string]$OutDir = "",
    [int]$Keep = 14
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if ($OutDir -eq "") { $OutDir = Join-Path $Root "data\backups" }
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$zip = Join-Path $OutDir "backup-$stamp.zip"
$stage = Join-Path $env:TEMP ("fosunai-backup-" + $stamp)
New-Item -ItemType Directory -Path $stage -Force | Out-Null

Write-Host "[1/4] 暂存文件到 $stage" -ForegroundColor Cyan

$patterns = @(
    @{ Src = "config\config.yaml";      Dst = "config" },
    @{ Src = "data\audit.db";           Dst = "data"   },
    @{ Src = "data\audit.db-wal";       Dst = "data"   },
    @{ Src = "data\paper_state.json";   Dst = "data"   },
    @{ Src = "data\stops.json";         Dst = "data"   },
    @{ Src = "data\vault.json";         Dst = "data"   }
)
foreach ($p in $patterns) {
    $src = Join-Path $Root $p.Src
    if (Test-Path $src) {
        $dstDir = Join-Path $stage $p.Dst
        New-Item -ItemType Directory -Path $dstDir -Force | Out-Null
        Copy-Item $src -Destination $dstDir -Force
        Write-Host ("  + {0}" -f $p.Src) -ForegroundColor Gray
    }
}

$logSrc = Join-Path $Root "logs"
if (Test-Path $logSrc) {
    $logDst = Join-Path $stage "logs"
    New-Item -ItemType Directory -Path $logDst -Force | Out-Null
    Get-ChildItem $logSrc -File |
        Where-Object { $_.LastWriteTime -gt (Get-Date).AddDays(-7) } |
        Copy-Item -Destination $logDst -Force
    Write-Host "  + logs (7d)" -ForegroundColor Gray
}

# 附带一份运行时状态快照，便于事后追溯
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
    try {
        & $venvPy -m ai_trader.cli status | Out-File -FilePath (Join-Path $stage "status.json") -Encoding utf8
        Write-Host "  + status.json" -ForegroundColor Gray
    } catch {
        Write-Host "  ! status 快照失败：$($_.Exception.Message)" -ForegroundColor Yellow
    }
}

Write-Host "[2/4] 压缩" -ForegroundColor Cyan
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -CompressionLevel Optimal -Force
Remove-Item $stage -Recurse -Force
$size = [math]::Round((Get-Item $zip).Length / 1KB, 1)
Write-Host "  -> $zip ($size KB)" -ForegroundColor Green

Write-Host "[3/4] 清理旧备份（保留 $Keep）" -ForegroundColor Cyan
if ($Keep -gt 0) {
    $old = Get-ChildItem $OutDir -Filter "backup-*.zip" | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep
    foreach ($f in $old) {
        Remove-Item $f.FullName -Force
        Write-Host ("  - {0}" -f $f.Name) -ForegroundColor Gray
    }
}

Write-Host "[4/4] 完成" -ForegroundColor Cyan
Write-Host "提示：vault.json 为 DPAPI 密文，换机器无法解密；跨机迁移请跑 migrate_export.ps1" -ForegroundColor Yellow
exit 0
