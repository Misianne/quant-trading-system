# DEPLOYMENT.md —— Windows VPS 部署与迁移

目标：一台 Windows Server 2019/2022（或 Windows 10/11）VPS，长期跑模拟盘 / 受控真实盘。

---

## 1. 前置条件

| 项 | 要求 | 校验命令 |
|---|---|---|
| OS | Windows 10/11 或 Server 2019+ | `[System.Environment]::OSVersion` |
| Python | **3.10+**（实测 3.13.14） | `python -c "import sys;print(sys.version)"` |
| Git | 任意近期版本（升级/迁移用） | `git --version` |
| 磁盘 | ≥ 2 GB（venv 约 300 MB） | — |
| 内存 | ≥ 2 GB | — |
| 时区 | `Asia/Shanghai`（交易时段判断依赖本机时区） | `tzutil /g` |

⚠️ **不要用 Microsoft Store 版 Python**（`C:\Users\<u>\AppData\Local\Microsoft\WindowsApps\python.exe`）。
它是应用商店存根，`--version` 可能返回非 0，且虚拟环境路径诡异。请装 python.org 官方安装包并勾选 "Add to PATH"。

---

## 2. 首次部署（约 3 分钟）

```powershell
# 1) 获取代码（二选一）
git clone <your-repo> C:\FosunAI-Trading
#   或者直接把整个目录拷过去（记得排除 .venv / data / logs / .env）

# 2) 克隆官方 SDK（本工程不把 sdk_src 入库）
cd C:\FosunAI-Trading
git clone https://github.com/fosunwealth/openapi-python-sdk.git sdk_src

# 3) 引导
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_windows.ps1
```

引导脚本做的事（幂等，可重复执行）：

1. 探测 Python（优先复用 `.venv`，自动跳过应用商店存根）
2. 检查 Git、探测 DPAPI 可用性
3. 创建/复用 `.venv`
4. `pip install -r requirements.txt` → `-e ./sdk_src` → `-e .`
   （`requirements.txt` 里含 **`tzdata`**：Windows 不自带 IANA 时区库，
   缺它美股/港股的 `zoneinfo` 时区换算会抛 `ZoneInfoNotFoundError`，
   交易时段判定将整体 fail-closed —— 表现为"永远不在交易时段"）
5. `config\config.yaml` 不存在时提示从 `config.template.yaml` 复制（**不会覆盖已有配置**）
6. `.env` 不存在时从 `.env.example` 复制（**只复制空模板，绝不写入假凭证**）
7. 创建 `data\` `logs\`
8. 打印 `check-env` 结果 + 跑 pytest

只想跳过测试：`-SkipTests`。只想看会做什么：`-DryRun`。

---

## 3. 配置凭证

### 3.1 方式 A：DPAPI 保险箱（推荐）

```powershell
$py = ".venv\Scripts\python.exe"

# API Key
& $py -m ai_trader.cli vault set --name FSOPENAPI_API_KEY --value "<平台下发的 key>"

# 两段 PEM（注意用双引号包裹，保留换行）
& $py -m ai_trader.cli vault set --name FSOPENAPI_SERVER_PUBLIC_KEY --value "$(Get-Content .\server_public.pem -Raw)"
& $py -m ai_trader.cli vault set --name FSOPENAPI_CLIENT_PRIVATE_KEY --value "$(Get-Content .\client_private.pem -Raw)"

# 网点地址与账户
& $py -m ai_trader.cli vault set --name FOSUN_BASE_URL --value "https://<网关域名>"

# 校验（只显示掩码和指纹，不显示明文）
& $py -m ai_trader.cli vault list
& $py -m ai_trader.cli vault get FSOPENAPI_API_KEY
```

保险箱落在 `data\vault.json`，每个值都是 `dpapi:` 前缀的密文。
**换 Windows 用户或换机器后无法解密** —— 这是特性不是缺陷，但迁移时必须走 §8。

### 3.2 方式 B：`.env`

直接编辑 `.env`，填 `FSOPENAPI_BASE_URL` / `FSOPENAPI_API_KEY` / 两段 PEM。
`python-dotenv` 风格的加载只做 `os.environ.setdefault`，**不覆盖已存在的进程环境变量**，也**不写系统环境**。

### 3.3 配置账户 ID

`config\config.yaml`：

```yaml
fosun:
  sdk_type: "ops"            # OPS 配对场景必须为 ops
  sub_account_id: "368241963"
  apply_account_id: "9969255001"   # OPS 必填
```

或设环境变量 `FOSUN_SUB_ACCOUNT_ID` / `FOSUN_APPLY_ACCOUNT_ID`。

---

## 4. 启动

### 4.1 前台（调试）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_web.ps1
# → http://127.0.0.1:8781
```

指定端口/地址：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_web.ps1 -Port 9000 -Host_ 0.0.0.0
```

### 4.2 计划任务（推荐，长期运行）

**任务 1：Web 管理台（开机自启，崩溃重启）**

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\FosunAI-Trading\scripts\run_web.ps1" `
  -WorkingDirectory "C:\FosunAI-Trading"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Days 365) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "FosunAI-Trading-Web" -Action $action -Trigger $trigger `
  -Settings $settings -RunLevel Highest -User "$env:USERNAME"
```

**任务 2：交易 worker（每分钟扫描 + 监控）**

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\FosunAI-Trading\scripts\run_worker.ps1" `
  -WorkingDirectory "C:\FosunAI-Trading"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
  -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "FosunAI-Trading-Worker" -Action $action -Trigger $trigger `
  -Settings $settings -RunLevel Highest -User "$env:USERNAME"
```

`-MultipleInstances IgnoreNew` 很重要：防止上一轮还没跑完又启动下一轮，导致重复下单（虽然风控的重复单检查会拦，但没必要制造冲突）。

**任务 3：健康检查（每 5 分钟）**

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\FosunAI-Trading\scripts\healthcheck.ps1"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "FosunAI-Trading-Health" -Action $action -Trigger $trigger `
  -RunLevel Highest -User "$env:USERNAME"
```

查看/删除：

```powershell
Get-ScheduledTask -TaskName "FosunAI-Trading-*" | Format-Table TaskName, State
Unregister-ScheduledTask -TaskName "FosunAI-Trading-Web" -Confirm:$false
```

---

## 5. 端口与网络

| 项 | 值 |
|---|---|
| 默认监听 | `127.0.0.1:8781`（仅本机） |
| 修改方式 | `config\config.yaml` → `web.host` / `web.port`，或环境变量 `WEB_HOST` / `WEB_PORT` |
| 健康检查 | `GET /api/status` |
| 接口文档 | `GET /api/docs`（OpenAPI，仅调试期开放） |

> **端口 8781 而非 8770**：实测本机 8770 已被其他进程占用（`pythonw.exe`）。部署前请用 `netstat -ano | findstr :8781` 确认空闲。

### 对外暴露（必须走反代，应用层不允许）

**审计 P1-11 收尾后，应用只允许监听回环地址。** 绑定 `0.0.0.0` 会直接拒绝启动：

```
$ ai-trader web --host 0.0.0.0
拒绝启动：管理台只能监听回环地址，收到 '0.0.0.0'。
→ 退出码 4
```

`--allow-insecure-bind` **已被移除**，没有逃生口。即使设置了 `WEB_ADMIN_PASSWORD`
也不允许非回环绑定 —— 因为"管理台有口令"不等于"整条链路是安全的"（明文 HTTP 下口令会被嗅探）。

需要公网访问时，按下面的架构：

```
Internet ── HTTPS:443 ──▶ Caddy / Nginx / IIS ARR
                              │  （TLS + 鉴权 + IP 白名单）
                              │  127.0.0.1:8781
                              ▼
                        FosunAI web（只听回环）
```

实施要点：

1. 应用保持 `web.host: "127.0.0.1"`，**不要改**
2. 反代层做 TLS 终止 + 强鉴权（Basic Auth / mTLS / SSO）
3. 仍建议设置 `WEB_ADMIN_PASSWORD` 与 `WEB_SESSION_SECRET` 作为第二层
4. 防火墙只放行 443，**不要**放行 8781

```powershell
New-NetFirewallRule -DisplayName "FosunAI Web (local only)" -Direction Inbound `
  -Protocol TCP -LocalPort 8781 -RemoteAddress 127.0.0.1 -Action Allow
```

Caddy 反代示例（`Caddyfile`）：

```
trade.example.com {
    reverse_proxy 127.0.0.1:8781
    basicauth {
        admin <bcrypt-hash>
    }
}
```

---

## 6. 加固清单

- [ ] `WEB_ADMIN_PASSWORD` + `WEB_SESSION_SECRET` 已设置且足够长
- [ ] `FSOPENAPI_CLIENT_SSL_VERIFY=true`（默认）
- [ ] `data\vault.json` 权限仅当前用户可读（脚本已尝试 `chmod 600`，Windows 上建议手工确认 ACL）
- [ ] VPS 登录启用强口令 + 密钥登录，禁用 RDP 公网暴露（或加白名单）
- [ ] Windows 自动更新开启，但**避开交易时段**重启
- [ ] 审计日志保留 ≥ 365 天（`audit.retention_days`）
- [ ] 定期跑 `healthcheck.ps1`（审计链断裂 = 有人动过库）
- [ ] `PAPER_TRADING=true` 时确认 `.env` 里没有任何真实凭证残留

检查 ACL：

```powershell
icacls C:\FosunAI-Trading\data\vault.json
icacls C:\FosunAI-Trading\.env
```

---

## 7. 备份

```powershell
powershell -ExecutionPolicy Bypass -File scripts\backup.ps1
# → data\backups\backup-20261108-120000.zip
```

包含：`config.yaml`、`audit.db`、`paper_state.json`、`stops.json`、`vault.json`（DPAPI 密文）、近 7 天日志、`status.json` 快照。

保留份数默认 14，`-Keep 0` 表示不清理。

计划任务（每天 23:50）：

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\FosunAI-Trading\scripts\backup.ps1"
$trigger = New-ScheduledTaskTrigger -Daily -At "23:50"
Register-ScheduledTask -TaskName "FosunAI-Trading-Backup" -Action $action -Trigger $trigger `
  -RunLevel Highest -User "$env:USERNAME"
```

> `vault.json` 是 DPAPI 密文，**备份到别的机器解不开**。跨机器恢复请用 §8 的迁移包。

---

## 8. 迁移到新 VPS

### 8.1 源机导出

```powershell
powershell -ExecutionPolicy Bypass -File scripts\migrate_export.ps1 `
  -Passphrase "一个至少12位的强口令"
# → dist\migrate-20261108-120500\
#      secrets.enc    scrypt + AES-256-GCM 加密的密钥包
#      bundle.zip     配置 + 审计库 + 模拟盘状态 + 止损表
#      MIGRATE.txt    目标机操作步骤
```

技术细节：`scrypt(N=2^15, r=8, p=1)` 派生 32 字节密钥 → `AES-256-GCM`，AAD 固定为 `ai_trader_vault_v1`，文件头 `FSTV1`。
所以 `secrets.enc` **不依赖 DPAPI**，可在任意 Windows 用户/机器上用同一口令解开。

### 8.2 目标机还原

```powershell
# 1) 装 Python/Git，拿代码，引导（跳过测试以求快）
git clone <your-repo> C:\FosunAI-Trading
cd C:\FosunAI-Trading
git clone https://github.com/fosunwealth/openapi-python-sdk.git sdk_src
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_windows.ps1 -SkipTests

# 2) 还原业务数据（先备份目标机现有 data\）
Expand-Archive dist\bundle.zip -DestinationPath . -Force

# 3) 还原密钥
.venv\Scripts\python.exe -m ai_trader.cli vault import `
  --infile dist\secrets.enc --passphrase "同一个口令"

# 4) 校验
.venv\Scripts\python.exe -m ai_trader.cli check-env
.venv\Scripts\python.exe -m ai_trader.cli status

# 5) 起服务
powershell -ExecutionPolicy Bypass -File scripts\run_web.ps1
```

迁移后**务必删除** `dist\migrate-*` 目录和任何写着口令的临时文件。

### 8.3 迁移核对表

| 项 | 期望 |
|---|---|
| `vault list` | 键数量与源机一致 |
| `check-env` → `safety` | 与源机完全一致（尤其 `paper_trading`） |
| `check-env` → `dpapi_available` | `true` |
| `auth` 审计链 | `chain.ok = true`，`total` 与源机一致 |
| `status` → `broker.kind` | `paper` |
| `stops.json` | 持仓止损仍在保护 |

---

## 9. 升级

```powershell
cd C:\FosunAI-Trading

# 1) 停下计划任务
Get-ScheduledTask -TaskName "FosunAI-Trading-*" | Disable-ScheduledTask

# 2) 备份
powershell -ExecutionPolicy Bypass -File scripts\backup.ps1

# 3) 更新代码与 SDK
git pull
cd sdk_src; git pull; cd ..
.venv\Scripts\python.exe -m pip install -e ./sdk_src
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -e .

# 4) 回归
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ai_trader.cli demo

# 5) 恢复任务
Get-ScheduledTask -TaskName "FosunAI-Trading-*" | Enable-ScheduledTask
```

SDK 升级后**必须**重新跑 `ai-trader sdk-audit` 并对比 `API_STATUS.md` 的接口表 —— 上游改字段名会静默破坏适配器的宽松解析。

---

## 10. 故障排查

### 10.1 服务起来后"点按钮进程就消失"（本机实测过）

**现象**：Web 服务正常运行，执行某些操作后进程直接退出，日志里出现：

```
[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":10309,"threshold":50,
 "scope":"turn","targets":["..."],"targetCount":1}
SystemExit: 1
```

**原因**：在被 agent/自动化注入的会话里，`PYTHONPATH` 指向一个 `sitecustomize.py` 删除钩子。它按"轮次"累计删除量，超过阈值后调用 `SystemExit(1)` —— 在常驻服务里等于自杀。

**处理**：启动脚本已内置

```powershell
Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
```

`run_web.ps1` 与 `run_worker.ps1` 都有这一行。手工启动时也应先清：

```powershell
$env:PYTHONPATH = ""
```

普通终端/Explorer 启动时 `PYTHONPATH` 本就为空，不受影响。

### 10.2 端口被占用

```
ERROR: [Errno 10048] error while attempting to bind on address ('127.0.0.1', 8781)
```

```powershell
netstat -ano | findstr :8781
Get-Process -Id <PID> | Select-Object Id, ProcessName, Path
```

改端口：`config\config.yaml` → `web.port`，或 `run_web.ps1 -Port 9000`。

### 10.3 PowerShell 脚本中文乱码 / 语法报错

Windows PowerShell 5.1 默认按 ANSI(GBK) 读 `.ps1`。含中文的脚本**必须保存为 UTF-8 with BOM**，否则会出现：

```
字符串缺少终止符: "
表达式或语句中包含意外的标记
```

本工程的脚本都已带 BOM。自己新增脚本时：

```powershell
$content = Get-Content my.ps1 -Raw -Encoding UTF8
[System.IO.File]::WriteAllText("$PWD\my.ps1", $content, (New-Object System.Text.UTF8Encoding $true))
```

### 10.4 `healthcheck.ps1` 报"无法连接到远程服务器"但服务明明在跑

系统代理（或 `http_proxy` 环境变量）把 `127.0.0.1` 也代理了。脚本已内置绕过：

```powershell
[System.Net.WebRequest]::DefaultWebProxy = New-Object System.Net.WebProxy
```

若你自写脚本，注意同样问题；`curl` 用 `--noproxy '*'`。

### 10.5 `check-env` 显示 `configured=false`

| `blocked_reason` | 含义 | 处理 |
|---|---|---|
| `PAPER_TRADING=true，真实券商连接被禁用` | 正常（模拟盘期间本就不该连） | 无需处理 |
| `LIVE_TRADING=false，真实券商连接被禁用` | 正常 | 无需处理 |
| `缺少 LIVE_ACK_PHRASE 显式确认` | 已开 live 但没写确认短语 | 设 `LIVE_ACK_PHRASE` |
| `缺少凭证: FSOPENAPI_API_KEY, ...` | 凭证没配好 | 按 §3 补齐 |

### 10.6 扫描全部 `rejected`，原因都是"不在交易时段"

正常（周末/非交易时段）。要看完整链路，用 `ai-trader demo`（沙盒里 `enforce_market_hours=false`）。

### 10.7 审计链断裂

```powershell
.venv\Scripts\python.exe -m ai_trader.cli status | Select-String "chain" -Context 0,4
```

`chain.ok = false` + `broken_at_seq = N` 说明第 N 条记录被改过（或库被替换）。
处理：从备份恢复 `audit.db`，并核对 `logs\audit.jsonl`（JSONL 里保留了同一批哈希，可交叉验证）。

### 10.8 venv 损坏

```powershell
Remove-Item .venv -Recurse -Force
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_windows.ps1
```

`data\` 与 `config\` 不受影响。

### 10.9 `Start-Process` 报"已添加项。字典中的关键字…"（本机实测）

**现象**：

```
Start-Process : 已添加项。字典中的关键字:"http_proxy"所添加的关键字:"HTTP_PROXY"
```

连 `Get-Item Env:\http_proxy`、`Test-Path Env:\http_proxy` 都会抛同样的错。

**原因**：本机同时存在 `HTTP_PROXY` 与 `http_proxy`（大小写不同、值相同）。
Windows PowerShell 5.1 的 `Start-Process` 用**大小写敏感**的字典装环境变量，
`Env:` 驱动器则用**大小写不敏感**的 `Hashtable` —— 两边都会因重复键报错。

**处理**：`run_worker.ps1` 已改为直接用 `System.Diagnostics.Process`（`UseShellExecute=$false`），
继承环境块而不建字典，既能拿到可靠 `ExitCode`，也能精确控制超时。

自己写脚本时要避开 `Start-Process`，或先清掉重复的代理变量：

```powershell
foreach ($k in @("http_proxy","https_proxy","HTTP_PROXY","HTTPS_PROXY")) {
    [Environment]::SetEnvironmentVariable($k, $null, "Process")
}
```

### 10.10 `WaitForExit(ms)` 之后 `ExitCode` 是 `$null`

PS 5.1 已知行为：带超时的 `WaitForExit(ms)` 返回 `$true` 后，`Process.ExitCode` 仍可能为空。

**处理**（`run_worker.ps1` 已实现）：

```powershell
$exited = $proc.WaitForExit($TimeoutSeconds * 1000)
if ($exited) { $proc.WaitForExit() }   # 无参调用一次，把退出码读回来
$code = $proc.ExitCode
```

### 10.11 急停哨兵（`data\HALT.json`）运维

| 操作 | 命令 | 说明 |
|---|---|---|
| 查看是否停止 | `type data\HALT.json` | 文件存在 = 停止 |
| 触发急停 | `ai-trader kill --reason "..."` | 写入哨兵并立即生效 |
| 解除急停 | `ai-trader resume --confirm RESUME-TRADING` | **必须逐字**，否则 exit 2 |
| 查来源 | `ai-trader status` → `safety.halt_source` | `sentinel` / `config` / `storage_error` |

⚠️ **不要用删除文件来"恢复运行"**。虽然技术上有效，但会跳过人工确认与审计留痕。
正确做法是 `ai-trader resume --confirm RESUME-TRADING`，它会写 `HALT.resumed.json` 并记审计 `kill_switch_off`。

⚠️ 哨兵损坏（例如手工编辑出错）会被判为 `storage_error` 并**继续保持停止**，
此时 `ai-trader status` 的 `halt_store_failed` 为 `true`，修好文件后重启即可。

### 10.12 worker 行为与退出码

`scripts\run_worker.ps1` 本轮新增能力：

| 能力 | 说明 |
|---|---|
| 命名 Mutex | `Global\FosunAI-Trading-Worker`，第二个实例直接退出（exit 4），不排队 |
| 逐步墙钟超时 | `-MonitorTimeoutSeconds`（默认 120）/ `-ScanTimeoutSeconds`（默认 180），超时 Kill 并 exit 3 |
| 心跳 | 每次运行写 `data\worker_heartbeat.json`，`healthcheck.ps1` 检查时效（>10 分钟告警） |
| 自检 | `-SelfTest` 用 1s 超时跑一个必然超时的子进程，验证 Kill 与 124 退出码 |

| 退出码 | 含义 | 排查 |
|---|---|---|
| 0 | 正常 | — |
| 1 | 有标的处理失败 | `logs\worker-*.log` |
| 2 | 被急停/门禁阻断 | 先看 `HALT.json` |
| 3 | 超时（已 Kill） | 查 SDK 网络、`fosun.http_read_timeout` |
| 4 | 另一个 worker 在跑 | 正常跳过；若长期 4 说明上一轮卡死 |


---

## 11. 回滚

| 场景 | 动作 |
|---|---|
| 代码升级出问题 | `git reset --hard <上一个 commit>`，重跑 `pip install -e .` |
| 配置改坏 | 从 `backup-*.zip` 取回 `config\config.yaml` |
| 模拟盘状态异常 | `ai-trader` → Web → 「重置模拟盘」（需 confirm=RESET） |
| 数据全损 | 从备份恢复；审计链会指向断点，但业务可继续 |
| 想彻底停手 | `ai-trader kill --reason "永久停止"`，然后禁用全部计划任务 |

---

## 12. 部署验收（照抄执行）

```powershell
cd C:\FosunAI-Trading

# 1) 环境
.venv\Scripts\python.exe -V
.venv\Scripts\python.exe -m ai_trader.cli check-env

# 2) 测试
.venv\Scripts\python.exe -m pytest -q

# 3) 链路
.venv\Scripts\python.exe -m ai_trader.cli demo

# 4) 起服务 + 健康检查
powershell -ExecutionPolicy Bypass -File scripts\run_web.ps1 &
Start-Sleep -Seconds 6
powershell -ExecutionPolicy Bypass -File scripts\healthcheck.ps1

# 5) 备份 + 迁移包试跑
powershell -ExecutionPolicy Bypass -File scripts\backup.ps1
powershell -ExecutionPolicy Bypass -File scripts\migrate_export.ps1 -Passphrase "test-passphrase-123"
```

全部通过 = 部署完成。任一步失败，按 §10 排查。

---

## 13. 交易规则与行情推送的运维（B4 / B5）

### 13.1 启用 A 股交易：先录节假日表

默认 `market_rules.calendar.status: UNKNOWN` + `strict: true` → **A 股一律拒绝交易**。
这是有意的：离线无法核实休市安排，编一张假表比不交易更危险。

录入步骤：

1. 打开交易所官网的当年休市安排公告
2. 把每个休市日按 `YYYY-MM-DD` 填入 `config/config.yaml`：

```yaml
market_rules:
  calendar:
    status: VERIFIED                       # 或 UNVERIFIED + allow_unverified_rules=true
    strict: true
    source: "上交所/深交所 2026 年休市安排公告"
    effective_from: "2026-01-01"
    holidays:
      cn: ["2026-01-01", "2026-01-02", "2026-02-16", "2026-02-17"]
```

3. 验证：

```bash
ai-trader check-env
# 关注 market_rules.calendar_status / unverified_count
```

`unverified_count` 下降且 `calendar_status=VERIFIED` 后，A 股模拟交易才会放行。

### 13.2 登记 ST 股与停牌标的

系统无法从代码判断 ST 状态，必须人工登记：

```yaml
market_rules:
  st_symbols:
    cn600xxx: cn_st_main        # 主板 ST
    cn300xxx: cn_st_chinext     # 创业板 ST
  suspended_symbols: ["cn600yyy"]
```

未登记时按普通股规则处理（涨跌幅 ±10%/±20%），**可能算错限价**，因此
ST 标的在登记前不建议交易。

### 13.3 启用实时行情推送

```yaml
market_feed:
  enabled: true
  url: "wss://<gateway>/msg/v1/push"
  codes: ["cn600519", "cn000001"]   # 留空则用 universe
  required_for_entry: true
  enforce_markets: ["cn"]
```

启用前必须：

1. 确认已开通行情权限（否则 WS 握手会返回 403，SDK 里有对应提示）
2. **抓一次真实报文**核对字段名 —— 当前解析器是"候选字段名"式的，
   `check-env` 里 `market_feed.protocol` 会显示 `unverified`
3. 观察 `ai-trader status` 的 `feed.state` 与 `feed.stats`：
   - `state` 应为 `SUBSCRIBED`
   - `stats.bad_payload` 长期 > 0 说明字段名不匹配
   - `stats.heartbeat_timeouts` 频繁说明网络或网关有问题

### 13.4 推送与轮询的关系

| 场景 | 行为 |
|---|---|
| `market_feed.enabled=false`（默认） | 完全走 REST 轮询（`scan` 间隔），止损延迟 = 轮询间隔 |
| 启用推送 | 开新仓前要求该标的行情新鲜；止损仍由 `monitor()` 的 REST 行情驱动 |

注意：**止损不以推送为准**。原因是推送断了也必须能止损，所以退出路径永远走
`monitor()` + 券商 REST 行情。

### 13.5 常见现象

| 现象 | 原因 | 处理 |
|---|---|---|
| A 股全部 `rejected`，`rule:NOT_TRADING_DAY` | 日历 `UNKNOWN` | 按 13.1 录入 |
| `rule:LOT_SIZE`（买入 150 股） | 不是 100 的整数倍 | 改成整手 |
| `rule:T_PLUS_VIOLATION` | 当日买入当日卖 | 正常拦截，次日再卖 |
| `rule:NO_PRICE_LIMIT_DATA` | 行情缺 `prevClose` | 检查行情源；缺失时无法算涨跌停，只能拒单 |
| `FEED_NOT_FRESH` | 推送未连/过期/未 settle | 看 `status.feed.state` |
