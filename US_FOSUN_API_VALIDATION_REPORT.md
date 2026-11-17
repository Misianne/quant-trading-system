# 复星 OpenAPI 真实调用验证报告

- 项目：`C:\FosunAI-Trading`
- 代码提交：`a6cda18`（协议/传输/Provider）、`e0e8f24`（SIT 护栏）
- 验证日期：2026-11-17
- **真实 API 请求次数：0**
- **结论：BLOCKED —— 本机没有任何凭据**

---

## 0. 一句话

第四阶段的所有动作都**没有执行**，因为四个必需的环境变量全部未设置。
本报告不伪造"已连通"，只给出：当前状态、缺少什么、拿到之后怎么执行、
以及执行时必须遵守的边界。

---

## 1. 凭据检查结果（只查存在性，未读取值）

```
FSOPENAPI_API_KEY              NOT_SET
FSOPENAPI_SERVER_PUBLIC_KEY    NOT_SET
FSOPENAPI_CLIENT_PRIVATE_KEY   NOT_SET
FSOPENAPI_BASE_URL             NOT_SET
```

检查方式：`os.environ.get(k)` 判断是否为 `None`，只输出 `SET/NOT_SET`。
未打印、未记录、未写入任何文件。未搜索磁盘、未读取密码文件、
未扫描 `.env`。

`~/.fsopenapi` 之类的配置文件也未读取（本适配器**只认环境变量**，
这是刻意设计：不给自己留"从磁盘找密钥"的口子）。

---

## 2. 需要你提供的四样东西

| 变量 | 内容 | 从哪来 |
|---|---|---|
| `FSOPENAPI_BASE_URL` | **生产** REST 网关根地址（如 `https://<host>`，末尾不带 `/`） | 开放平台 / 客户经理 |
| `FSOPENAPI_API_KEY` | API Key | 开放平台下发 |
| `FSOPENAPI_CLIENT_PRIVATE_KEY` | 客户端**长期私钥** PEM（PKCS#8） | 开放平台随密钥对下发 |
| `FSOPENAPI_SERVER_PUBLIC_KEY` | 服务端**长期公钥** PEM | 同上 |

四项缺一，`_build_transport_from_env()` 会抛 `ProviderNotConfigured` 并列出缺哪些。

### 2.1 为什么私钥是"下发"而不是"自己生成"

握手是**双向认证**：客户端用长期私钥签 `临时公钥||nonce`，服务端用登记过的
公钥验签。所以这把私钥必须与平台上登记的密钥对匹配——不是你本地随便生成的。

### 2.2 必须同时确认的四件事

| 待确认 | 为什么 |
|---|---|
| 生产 base_url（**不是** `openapi-sit.*`） | 测试环境行情不代表真实市场 |
| 账户是否已开通**美股 API 行情**权限 | SDK 的 403 文案："请检查账户是否已开通对应行情服务" |
| 单次调用是否计费/消耗额度 | 若可能产生费用，必须先停下来等你批准 |
| 限流（QPS、日调用量、`codes` 批量上限） | 官方未公布，我们默认只跑 2 req/s |

> 关于"App 有实时行情权限"：这**不等于** API 有行情权限。两者是不同通道，
> 不能直接推断。必须由平台确认。

---

## 3. 配置方式（不落盘到项目）

PowerShell（当前会话有效，不写入用户环境变量、不进 Git）：

```powershell
$env:FSOPENAPI_BASE_URL            = "https://<生产网关>"
$env:FSOPENAPI_API_KEY             = "<api-key>"
$env:FSOPENAPI_CLIENT_PRIVATE_KEY  = Get-Content -Raw "<客户端私钥.pem>"
$env:FSOPENAPI_SERVER_PUBLIC_KEY   = Get-Content -Raw "<服务端公钥.pem>"
```

注意：`Get-Content -Raw` 读取文件是**你**的操作；本程序不读文件。

**禁止**：
- 写进 `config.yaml`、`.env`、测试文件、报告
- 通过命令行参数传（会进进程列表与 shell 历史）
- 提交到 Git

---

## 4. 拿到凭据后的执行脚本（本轮未运行）

### 4.1 单次报价验证

```python
# tools/fosun_smoke.py（下一阶段新建，本轮未创建）
import json, time
from datetime import datetime, timezone
from ai_trader.market.providers import FosunProvider

prov = FosunProvider({"max_age_seconds": 60})
prov.connect()

t0 = time.perf_counter()
snap = prov.snapshot_quote("AAPL")          # 唯一一次行情调用
elapsed_ms = (time.perf_counter() - t0) * 1000

record = {
    "request_started_at": datetime.now(timezone.utc).isoformat(),
    "elapsed_ms": round(elapsed_ms, 1),
    "raw_fields": snap.raw,                 # 原始字段，原样保存
    "last": snap.last, "bid": snap.bid, "ask": snap.ask,
    "volume": snap.volume,
    "event_time": None,                     # 官方未提供
    "time_semantics": snap.time_semantics,  # "unknown"
    "receive_time": snap.receive_time.isoformat(),
    "realtime": "UNKNOWN",                  # 无官方证据前不判定
}
print(json.dumps(record, ensure_ascii=False, indent=2, default=str))
```

**范围限制（硬约束）**：

- 只查 `usAAPL`
- 只调用 `snapshot_quote`
- 不调用 `account` / `portfolio` / `trade` / 任何订单接口
- 不订阅付费服务、不改账户权限
- 不轮询（单次）

### 4.2 判定标准

| 结果 | 条件 |
|---|---|
| **PASS** | `code==0` 且返回 `last/bid/ask`，能确认是生产环境，且记录完整 |
| **PARTIAL** | 调用成功但字段与文档不符，或无法确认是生产环境 |
| **BLOCKED** | 403 / 70003（权限未开通）、401（密钥无效）、网关不可达 |

### 4.3 若返回 403

立即停止，不要重试（代码里已强制：`FosunPermissionError` 不重试，
且 `permission_denied=True` 后不再发请求）。回到 §2.2 补授权。

---

## 5. 为什么"没有凭据"不等于"什么都做不了"

本轮把能离线验证的部分全部做完了：

| 能离线验证 | 证据 |
|---|---|
| 协议实现正确性（HKDF 对称、HMAC 可验、AES-GCM 往返） | 27 项协议/传输测试 |
| 握手签名与验签 | `test_handshake_sign_and_verify` |
| 请求签名覆盖范围 | `test_signature_is_deterministic_and_verifies`（改任一项即验签失败） |
| 解析器对官方字段形状的处理 | 51 项 Provider 测试 |
| 超时/重试/限流/重连/缺口 | 传输层测试 |
| 与网关、风控、账本集成 | 11 项风控门禁测试 |

**不能**离线验证：真实延迟、真实更新频率、真实权限、真实限流、
四段时段覆盖、WS 实际推送。这些全部依赖 §2。

---

## 6. 未执行清单

- 未发送任何 HTTP 请求到 `fosunxcz.com` 或任何复星域名
- 未建立 WebSocket 连接
- 未读取任何密钥文件
- 未调用账户/持仓/资金/订单/交易接口
- 未下单
- 未订阅任何服务
- 未产生任何费用

---

## 7. 下一步（等你）

1. 提供 §2 的四项配置（或告诉我"暂时拿不到"，那就停在这里）
2. 我执行 §4.1 的单次查询
3. 若 PASS，再提"是否进入 5 分钟低频观察"（第五阶段）

**在拿到凭据之前，本项目对复星行情的接入状态是：代码就绪、验证未开始。**
