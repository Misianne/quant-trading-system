"""星财富（Fosun Wealth）官方 OpenAPI 适配器。

安全设计
--------
* 构造时**不会**抛出异常，而是把 ``connected=False`` 和 ``blocked_reason`` 记录下来，
  让 Web 面板能如实显示"未连接、原因是什么"。
* 任何需要真实网络的调用都先过 :class:`ai_trader.config.SafetyGate`；
  被拒时抛 :class:`BrokerNotConfigured`，绝不静默降级、绝不伪造成交。
* **未返回订单号时绝不编造**：抛 :class:`OrderStateUnknown`，状态标 ``unknown``，
  由调用方用 ``client_id`` 去券商侧对账。
* 持仓/行情解析不出来时**抛错**，不返回空集合（空集合会让风控 fail-open）。
* SDK 源码**不修改**（保持审计基线与上游一致）。行情加密问题通过官方 GET 端点
  ``/v1/market/min`` 做客户端兜底，并在 ``health()`` 中如实标注。

响应结构说明
-----------
SDK 的 ``client.*`` 返回的是完整信封 ``{"code":0,"data":{...},"requestId":...}``。
本适配器统一通过 :func:`_payload` 取出 ``data``，再用一组候选字段名做宽松解析；
但"宽松"只体现在字段名，**不体现在结构**：结构不符会抛 ``PARSE_MISMATCH``。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import AppConfig, dotenv_origin, internal_env_write
from ..secrets import SecretVault, redact_pem
from .base import (
    AccountInfo,
    AccountSummary,
    Broker,
    BrokerError,
    BrokerNotConfigured,
    Candle,
    OrderRequest,
    OrderResult,
    OrderStateUnknown,
    Position,
    Quote,
    now_iso,
)
from .paper import currency_of, market_of

logger = logging.getLogger("ai_trader.fosun")

# 候选字段名（不同接口/版本可能不一致，按优先级依次尝试）
_LAST_KEYS = ("lastPrice", "last", "price", "curPrice", "close", "nominalPrice")
_PREV_CLOSE_KEYS = ("prevClose", "preClose", "prevClosePrice", "yesterdayClose")
_OPEN_KEYS = ("open", "openPrice", "todayOpen")
_HIGH_KEYS = ("high", "highPrice", "todayHigh")
_LOW_KEYS = ("low", "lowPrice", "todayLow")
_VOLUME_KEYS = ("volume", "vol", "totalVolume")
_QTY_KEYS = ("qty", "quantity", "currentQty", "holdQty", "availableQty")
_COST_KEYS = ("avgCost", "costPrice", "averageCost", "openPrice", "cost")
_CASH_KEYS = ("cash", "availableCash", "cashBalance", "availableFund", "withdrawCash")
_EQUITY_KEYS = ("totalAsset", "netAsset", "equity", "totalEquity", "assetValue")
_MV_KEYS = ("marketValue", "totalMarketValue", "securitiesValue")
_DAY_PNL_KEYS = ("dayPnl", "todayPnl", "dailyPnl", "todayProfitLoss", "dayProfit")
_SYMBOL_KEYS = ("stockCode", "code", "symbol")
_ORDER_ID_KEYS = ("orderId", "order_id", "id")

#: 可以承载"列表"的键名
_LIST_KEYS = ("list", "items", "rows", "records", "positions", "holdings",
              "orders", "accounts", "result")

#: 认为是"客户端解密/解析失败"的错误特征 —— 行情兜底只在这种情况触发
_PAYLOAD_ERROR_HINTS = ("decrypt", "解密", "parse", "解析", "bad json")


def _first(d: Dict[str, Any], keys, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _has(d: Dict[str, Any], keys) -> bool:
    return any(k in d and d[k] not in (None, "") for k in keys)


def _fnum(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _payload(resp: Any) -> Any:
    """从 SDK 信封中取出业务数据。"""
    if isinstance(resp, dict):
        if "data" in resp:
            return resp["data"]
        return resp
    return resp


def _extract_rows(payload: Any, list_keys=_LIST_KEYS) -> Tuple[List[Dict[str, Any]], bool]:
    """把容器结构拍平成 ``(rows, shape_ok)``。

    ``shape_ok=False`` 表示"这个响应我们根本读不懂"，调用方必须抛错而不是当成空结果。
    """
    if payload is None:
        return [], False
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)], True
    if isinstance(payload, dict):
        for k in list_keys:
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)], True
        if any(k in payload for k in _LAST_KEYS + _QTY_KEYS + _ORDER_ID_KEYS):
            return [payload], True
        if not payload:
            # 空对象：没有任何字段可读，也无法断言"确实为空"
            return [], False
    return [], False


def _as_list(payload: Any, list_keys=_LIST_KEYS) -> List[Dict[str, Any]]:
    """兼容旧调用：只取 rows，不看 shape_ok（新代码请用 :func:`_extract_rows`）。"""
    return _extract_rows(payload, list_keys)[0]


def _looks_like_payload_error(exc: BaseException) -> bool:
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(h in msg for h in _PAYLOAD_ERROR_HINTS)


@dataclass
class _ConnState:
    connects: int = 0
    retries: int = 0
    last_reason: str = ""


class CredentialConflict(BrokerError):
    """进程内已存在**不同**的身份凭证，拒绝静默覆盖。"""

    def __init__(self, message: str):
        super().__init__(message, code="CREDENTIAL_CONFLICT")


class CredentialSourceMix(BrokerError):
    """三个身份要素来自**不同来源**（Vault / 显式环境 / dotenv / 进程注入）。

    Batch 4.1.1 / SR-B1-04：只允许"整套凭证来自同一来源"。混用会产生
    服务端无法验证的"混合身份"（签名密钥与 ApiKey 不属于同一账户），
    表现为难以排查的 401，且 Vault 的轮换不会生效。
    """

    def __init__(self, sources: Dict[str, str]):
        detail = ",".join(f"{k}={v}" for k, v in sorted(sources.items()))
        super().__init__(
            f"身份凭证来源不一致（{detail}），拒绝以混合身份初始化；"
            f"请让三个要素来自同一来源（Vault 或同一组环境变量）",
            code="CREDENTIAL_SOURCE_MIX")


class CredentialIncomplete(BrokerError):
    """三要素不完整（部分缺失），按 fail-closed 拒绝初始化。"""

    def __init__(self, missing: List[str]):
        super().__init__(
            f"身份凭证不完整：缺少 {', '.join(missing)}（fail-closed）",
            code="CREDENTIAL_INCOMPLETE")


class _CredEnvRegistry:
    """进程级身份环境变量的共享管理（引用计数 + 身份指纹 + 冲突检测）。

    为什么需要（独立审计 B-1 / B-1-a）
    --------------------------------
    官方 SDK **只从 ``os.environ`` 读取身份密钥**，同一进程内的多个适配器
    实例必然共享这一份全局状态。于是有两个必须同时解决的问题：

    1. 生命周期：A 释放不能清掉 B 仍在用的变量 → 引用计数；
    2. 身份：B 不能把"A 注入进环境的值"当成自己的凭证 → **来源判定**。

    第 2 点是 Batch 4.1 的核心。旧实现只比较"注入后的值"，而 B 解析凭证时
    读到的正是 A 注入的值，于是 B 的 Vault 配置被静默忽略、以 A 的身份完成
    初始化（实测：``configured=True`` 且 ``feed_auth_ready()=True``）。

    现在注册表只保存**凭证指纹**（``sha256(前缀 | 变量名 | 值)``，仅驻内存、
    绝不写日志/落盘/进异常），并通过 :meth:`is_ours` 让解析层区分：

    * 调用方**显式**设置的环境变量 → 环境优先（既有语义，不变）；
    * **本进程自己注入**的环境变量 → 不是本实例的凭证配置，必须回查 Vault；
    * Vault 有值但指纹不同 → :class:`CredentialConflict`（fail-closed）。

    其它保证：归零才清理、清理前按指纹核对、全程 ``RLock``、重复 release
    幂等、部分 acquire 失败可由调用方回滚。

    作用域（务必如实理解）
    --------------------
    这是**进程内**机制，``os.environ`` 本身也是进程私有的。跨进程之间既没有
    共享状态，也没有任何协调或冲突检测：两个独立进程各按自己的环境/保险箱
    工作。本注册表**不能**、也从未被设计用来协调多个独立进程。
    """

    #: 指纹前缀带上版本，避免与其它用途的 sha256 混淆
    _PREFIX = b"ai_trader.cred.v1\x00"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: Dict[str, Dict[str, Any]] = {}

    # ---- 指纹（不可逆；只驻内存，不写日志/异常） -------------------- #
    @classmethod
    def fingerprint(cls, name: str, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        h = hashlib.sha256()
        h.update(cls._PREFIX)
        h.update(str(name).encode("utf-8"))
        h.update(b"\x00")
        h.update(str(value).encode("utf-8"))
        return h.hexdigest()

    def is_ours(self, name: str, value: Optional[str]) -> bool:
        """``value`` 是否正是**本进程注入**的那个值。

        这是区分"调用方显式配置"与"我们自己写进环境"的唯一判据；不能用
        "env 里有值"来代替（那正是 B-1 串用的根因）。
        """
        if not value:
            return False
        fp = self.fingerprint(name, value)
        with self._lock:
            entry = self._entries.get(name)
            return bool(entry and entry.get("owners") and entry.get("fp") == fp)

    def is_self_owned(self, name: str, value: Optional[str], owner: str) -> bool:
        """``value`` 是否由 **``owner`` 自己**注入（B-1 严格化）。

        ``is_ours()`` 只回答"是不是本进程注入的"，无法区分"我注入的"和
        "另一个实例注入的"。凭据来源模型必须区分这两者：

        * 自己注入 → 可以在 ``health()`` 重解析时识别为 ``injected``；
        * 他人注入 → **禁止继承**（无 Vault 的实例不得顶用别人的身份）。

        兼容性：早期登记（未传 ``owner`` 语义的旧路径）一律由
        :meth:`acquire` 写入 owner，因此这里不存在"孤儿条目"。
        """
        if not value:
            return False
        fp = self.fingerprint(name, value)
        with self._lock:
            entry = self._entries.get(name)
            return bool(entry
                        and entry.get("fp") == fp
                        and owner in entry.get("owners", ()))

    def acquire_many(self, items: "List[Tuple[str, str]]", owner: str) -> Dict[str, str]:
        """**原子**登记一组身份变量：全成功或全不动。

        为什么需要（B-1 并发）
        --------------------
        三要素是"一个集合"，逐个 ``acquire`` 会在并发下互相穿插：A 拿到 PUB、
        B 拿到 PRIV，两边随后各自冲突 —— 环境被写成一个混合身份。这里在**同一
        次持锁**内先校验全部指纹，再统一写入：要么整套属于同一 owner，要么
        一个字节都不写（等价于回滚，不需要补偿逻辑）。

        返回 ``{变量名: "created" | "shared"}``；冲突抛 :class:`CredentialConflict`。
        """
        import os

        with self._lock:
            plans: List[Tuple[str, str, Optional[str], str]] = []
            for name, value in items:
                if not name or not value:
                    continue
                fp = self.fingerprint(name, value)
                entry = self._entries.get(name)
                if entry and entry.get("owners"):
                    if entry.get("fp") != fp:
                        # 只报变量名：不含值 / 不含指纹 / 不给可逆线索
                        raise CredentialConflict(
                            f"进程内已存在不同的 {name}（另一实例正在使用其它凭证），"
                            f"拒绝静默覆盖；请先释放旧实例，或让两个实例使用同一凭证")
                    plans.append((name, value, fp, "shared"))
                else:
                    plans.append((name, value, fp, "created"))

            result: Dict[str, str] = {}
            for name, value, fp, kind in plans:
                if kind == "created":
                    self._entries[name] = {"fp": fp, "owners": {owner},
                                           "preexisting": os.environ.get(name)}
                else:
                    self._entries[name]["owners"].add(owner)
                # 受控写入：这是我们自己注入的，不算"外部改写了 .env 的值"
                with internal_env_write():
                    os.environ[name] = value
                result[name] = kind
            return result

    def acquire(self, name: str, value: str, owner: str) -> str:
        """登记**单个**变量并写入环境。返回 ``"created"`` / ``"shared"``。"""
        return self.acquire_many([(name, value)], owner)[name]

    def release(self, owner: str) -> Dict[str, List[str]]:
        """释放该 owner 的全部引用；仅在归零时恢复/清理环境。"""
        import os

        popped: List[str] = []
        restored: List[str] = []
        with self._lock:
            for name in list(self._entries):
                entry = self._entries.get(name)
                if not entry or owner not in entry["owners"]:
                    continue
                entry["owners"].discard(owner)
                if entry["owners"]:
                    continue
                # 仅当环境值仍是"我们写入的那个"（按指纹比对）才动它，
                # 避免误删别人后来写入的值
                current = os.environ.get(name)
                if (current is not None
                        and self.fingerprint(name, current) == entry.get("fp")):
                    pre = entry.get("preexisting")
                    with internal_env_write():
                        if pre is not None and pre.strip():
                            os.environ[name] = pre
                            restored.append(name)
                        else:
                            os.environ.pop(name, None)
                            popped.append(name)
                self._entries.pop(name, None)
        return {"popped": popped, "restored": restored}

    def is_operator_value(self, name: str, value: Optional[str]) -> bool:
        """``value`` 是否在**我们写入之前**就已存在于环境中（运维自己提供的）。

        用途（B-1 来源模型的关键判据）：``acquire`` 会记录 ``preexisting``。
        若覆盖前的值与当前值相同，说明这个值本来就由运维 export（我们只是
        又写了一遍同样的内容），其它实例可以合法地把它当 ``explicit_env``；
        若 ``preexisting`` 为空/不同，则当前值纯粹是**某个实例注入**的，
        其它没有 Vault 的实例不得继承（SR-B1-05）。
        """
        if not value:
            return False
        fp = self.fingerprint(name, value)
        with self._lock:
            entry = self._entries.get(name)
            if not entry or entry.get("fp") != fp:
                return False
            pre = entry.get("preexisting")
            if not pre or not str(pre).strip():
                return False
            return self.fingerprint(name, pre) == fp

    def owners_of(self, name: str) -> int:
        with self._lock:
            entry = self._entries.get(name)
            return len(entry["owners"]) if entry else 0

    def snapshot(self) -> Dict[str, int]:
        """只返回"变量名 → 引用数"，不含任何值。"""
        with self._lock:
            return {n: len(e["owners"]) for n, e in self._entries.items()}

    def clear(self) -> None:
        """仅供测试夹具：强制清空注册表（不碰环境变量）。"""
        with self._lock:
            self._entries.clear()


class _MarketPushDisabled:
    """SDK 行情推送入口的禁调用哨兵（独立审计 A-1）。

    SDK 的 ``MarketPushAPI.connect()`` 会**自行**调用
    ``auth_manager.get_valid_session()``（可能触发 SessionCreate），并连到
    硬编码的生产地址 ``wss://openapi-push.fosunxcz.com/msg/v1/push``，
    完全绕过 ``feed_auth_ready()`` / ``_require()`` 与路由冲突校验。

    项目要求所有行情连接都经由 ``MarketFeed``（它走 ``feed_auth_headers()``），
    因此这里让任何"绕道"的调用**立即失败**，并给出明确错误码，且绝不发起
    任何网络请求。
    """

    #: 与 BrokerError.code 对齐，便于上层识别
    code = "FEED_AUTH_BYPASS"

    def __getattr__(self, item: str) -> Any:
        if item.startswith("_"):
            # 让 copy/inspect 之类的内省走正常 AttributeError，不抛业务错误
            raise AttributeError(item)
        raise BrokerError(
            f"禁止直接调用 SDK 行情推送接口 market_push.{item}（A-1）：该入口会绕过 "
            f"feed_auth_ready()/feed_auth_headers() 并自行发起会话与连接；"
            f"请使用 ai_trader.market.feed.MarketFeed",
            code="FEED_AUTH_BYPASS")

    def __repr__(self) -> str:  # pragma: no cover - 仅调试可见
        return "<market_push disabled: use ai_trader.market.feed.MarketFeed>"


#: 进程级单例
_CRED_ENV = _CredEnvRegistry()


# ---------------------------------------------------------------------- #
# 凭据来源模型（B-1 严格化）
# ---------------------------------------------------------------------- #
#: 来源标识。顺序即**优先级**：显式环境变量 > Vault > dotenv > 进程内注入。
SRC_EXPLICIT_ENV = "explicit_env"
SRC_VAULT = "vault"
SRC_DOTENV = "dotenv"
SRC_INJECTED = "injected"
SRC_MISSING = "missing"

#: 三要素必须同源的凭据字段：``(环境变量名, creds 键, 审计短名)``
_SDK_CRED_FIELDS = (
    ("FSOPENAPI_SERVER_PUBLIC_KEY", "server_public_key", "PUB_KEY"),
    ("FSOPENAPI_CLIENT_PRIVATE_KEY", "client_private_key", "PRIV_KEY"),
    ("FSOPENAPI_API_KEY", "api_key", "API_KEY"),
)

#: ``health()["credential_origin"]`` 的历史取值（键为环境变量名）。
#: ``explicit_env`` → ``env`` 供既有面板/测试兼容；``dotenv`` 单独暴露，
#: 这样"运维 export"与"应用读 .env"在审计字段里仍可区分（SR-B1-04）。
_LEGACY_ORIGIN = {
    SRC_EXPLICIT_ENV: "env",
    SRC_VAULT: "vault",
    SRC_DOTENV: "dotenv",
    SRC_INJECTED: "injected",
    SRC_MISSING: "missing",
}

#: 来源 → **信任域**。同域内的来源都是**运维本地配置**，不构成"混合身份"。
#:
#: 为什么 dotenv 与 explicit_env 属于同一域（Batch 4.1.2 / AUD2-B1-02）
#: -----------------------------------------------------------------
#: 两者都是"运维放在本机、由本项目读取的配置"：``.env`` 与 ``export`` 只是
#: 同一份本地凭据的两种输入方式。真实轮换场景（``.env`` 里保留未变的公钥、
#: 只 export 轮换后的私钥与 API_KEY）曾被判成 ``mixed`` 而**无法启动**。
#: 同源校验仍然存在，但只在"Vault ↔ 本地配置"这种**跨信任域**时才拒绝：
#: 那才是"一半来自保险箱、一半来自本地"的混合身份，必须 fail-closed。
_SOURCE_DOMAIN = {
    SRC_EXPLICIT_ENV: "local",
    SRC_DOTENV: "local",
    # ``injected`` 只在"我们自己写进环境、而 Vault 里没有"时出现 —— 那个值
    # 本来就来自本地配置（显式或 dotenv），因此仍属本地域。若把注入单列一域，
    # 成功初始化后的 health() 重解析会把正常的显式配置误报成 mixed。
    SRC_INJECTED: "local",
    SRC_VAULT: "vault",
    SRC_MISSING: "missing",
}


class _MarketPushGuard:
    """SDK 行情推送入口的**类级**拦截（A-1 补强）。

    为什么需要类级
    --------------
    SDK 的 ``SDKClient.__init__`` 会执行 ``self.market_push = MarketPushAPI(self)``
    （``fsopenapi/__init__.py:40``），因此**不能**拦 ``MarketPushAPI.__init__``——
    那样连 ``SDKClient(...)`` 都构造不出来。只能在**方法**层拦。

    实例级 ``_MarketPushDisabled`` 只保护"当前这个 client"，一旦出现第二个
    client、或 SDK 内部延迟创建 ``market_push``，实例替换就失效。类级替换覆盖
    **所有**实例（含未来创建的），且因为 ``subscribeXxx`` 全都是
    ``subscribe``/``unsubscribe`` 的薄包装，拦五个核心方法即可封死整族入口。

    约束
    ----
    * **同步**抛出：``async def`` 的包装在被 ``await`` 之前不会执行；这里用同步
      函数，调用即失败，不会留下"创建了协程但没人 await"的静默旁路。
    * 幂等 + 引用计数：多个 broker 实例各自登记 owner，只有最后一个释放时才
      恢复原始方法（避免 A 关闭把 B 的保护拆掉）。
    * **精确恢复**：只恢复"仍然是我们装上去的那个函数"的方法；若第三方已经
      改过，跳过并记录 ``tampered``，绝不覆盖别人的改动。
    * 安装失败（方法缺失/属性不可写）→ 全量回滚并返回 False，由调用方
      fail-closed。
    """

    #: 需要拦截的核心入口（其余 ``subscribeQuote`` 等均为薄包装）
    METHODS = ("connect", "subscribe", "unsubscribe", "close", "send_heartbeat")

    #: 打在包装函数上的标记（用于幂等/篡改检测/精确恢复）
    _FLAG = "_aitrader_mpa_guard"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cls: Any = None
        self._originals: Dict[str, Any] = {}
        self._owners: set = set()
        self._installed_names: List[str] = []

    # ---- 内部 -------------------------------------------------------- #
    @classmethod
    def _make_blocker(cls, name: str) -> Any:
        def _blocked(*args: Any, **kwargs: Any) -> Any:
            raise BrokerError(
                f"禁止直接调用 SDK 行情推送接口 market_push.{name}（A-1）："
                f"该入口会绕过 feed_auth_ready()/feed_auth_headers() 并自行发起"
                f"会话与连接；请使用 ai_trader.market.feed.MarketFeed",
                code="FEED_AUTH_BYPASS")

        _blocked.__name__ = f"_blocked_{name}"
        _blocked.__qualname__ = f"MarketPushAPI._blocked_{name}"
        _blocked.__doc__ = (f"A-1 拦截器：替代 MarketPushAPI.{name}，"
                            f"调用即抛 FEED_AUTH_BYPASS，不发起任何网络请求。")
        setattr(_blocked, cls._FLAG, True)
        setattr(_blocked, "_aitrader_mpa_name", name)
        return _blocked

    @classmethod
    def _is_guarded(cls, fn: Any) -> bool:
        return bool(getattr(fn, cls._FLAG, False))

    # ---- 安装 / 释放 -------------------------------------------------- #
    def install(self, target_cls: Any, owner: str) -> Tuple[bool, Dict[str, Any]]:
        """在 ``target_cls`` 上安装拦截；返回 ``(ok, detail)``。"""
        info: Dict[str, Any] = {"class": getattr(target_cls, "__name__", ""),
                                "methods": list(self.METHODS)}
        with self._lock:
            if self._cls is not None and self._cls is not target_cls:
                info.update(reason="class_mismatch",
                            installed_on=getattr(self._cls, "__name__", ""))
                return False, info
            if self._cls is target_cls:
                self._owners.add(owner)
                info.update(reason="already_installed", owners=len(self._owners))
                return True, info

            originals: Dict[str, Any] = {}
            missing: List[str] = []
            for name in self.METHODS:
                fn = getattr(target_cls, name, None)
                if not callable(fn):
                    missing.append(name)
                else:
                    originals[name] = fn
            if missing:
                info.update(reason="missing_methods", missing=missing)
                return False, info

            replaced: List[str] = []
            try:
                for name in self.METHODS:
                    setattr(target_cls, name, self._make_blocker(name))
                    replaced.append(name)
            except Exception as exc:  # noqa: BLE001 - 只读类/元类拦截等
                for name in replaced:
                    try:
                        setattr(target_cls, name, originals[name])
                    except Exception:  # noqa: BLE001
                        pass
                info.update(reason=f"setattr_failed:{type(exc).__name__}")
                return False, info

            # 回读校验：不假设替换成功
            bad = [n for n in self.METHODS
                   if not self._is_guarded(getattr(target_cls, n, None))]
            if bad:
                for name in self.METHODS:
                    try:
                        setattr(target_cls, name, originals[name])
                    except Exception:  # noqa: BLE001
                        pass
                info.update(reason="verify_failed", unguarded=bad)
                return False, info

            self._cls = target_cls
            self._originals = originals
            self._owners = {owner}
            self._installed_names = list(self.METHODS)
            info.update(reason="installed", owners=1)
            return True, info

    def release(self, owner: str) -> Dict[str, Any]:
        """释放该 owner 的引用；归零时才恢复，且只恢复仍属我们的方法。"""
        with self._lock:
            if self._cls is None:
                return {"installed": False, "released": False, "restored": [],
                        "skipped": [], "remaining": 0}
            self._owners.discard(owner)
            if self._owners:
                return {"installed": True, "released": False, "restored": [],
                        "skipped": [], "remaining": len(self._owners)}
            cls = self._cls
            restored: List[str] = []
            skipped: List[str] = []
            for name, fn in self._originals.items():
                current = getattr(cls, name, None)
                if self._is_guarded(current):
                    try:
                        setattr(cls, name, fn)
                        restored.append(name)
                    except Exception:  # noqa: BLE001
                        skipped.append(name)
                else:
                    # 第三方已改写 → 不覆盖，仅记录
                    skipped.append(name)
            self._cls = None
            self._originals = {}
            self._installed_names = []
            return {"installed": False, "released": True, "restored": restored,
                    "skipped": skipped, "remaining": 0}

    # ---- 状态 --------------------------------------------------------- #
    def tampered(self) -> List[str]:
        """已安装但方法被第三方改回/替换的入口（自愈检测）。"""
        with self._lock:
            if self._cls is None:
                return []
            return [n for n in self._installed_names
                    if not self._is_guarded(getattr(self._cls, n, None))]

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "installed": self._cls is not None,
                "class": getattr(self._cls, "__name__", ""),
                "methods": list(self._installed_names),
                "owners": len(self._owners),
                "tampered": [n for n in self._installed_names
                             if not self._is_guarded(getattr(self._cls, n, None))],
            }

    def clear(self) -> None:
        """仅供测试夹具：强制卸载（恢复原始方法）。"""
        with self._lock:
            if self._cls is None:
                return
            for name, fn in self._originals.items():
                try:
                    setattr(self._cls, name, fn)
                except Exception:  # noqa: BLE001
                    pass
            self._cls = None
            self._originals = {}
            self._owners = set()
            self._installed_names = []


#: 进程级单例（类级拦截是全局的，必须引用计数）
_MPA_GUARD = _MarketPushGuard()


def _register_secret_values(creds: Dict[str, Optional[str]]) -> None:
    """把已解析的凭证登记到日志脱敏表（F-2）。

    必须在"凭证可能被打印"之前完成：包括构造期的 warning、异常 traceback、
    以及后续任何 logger。登记失败绝不影响初始化（脱敏是增强，不是前置条件）。
    """
    try:
        from ..log_redaction import register_secret
    except Exception:  # pragma: no cover - 极端导入失败时降级
        return
    for key in ("server_public_key", "client_private_key", "api_key"):
        try:
            register_secret(creds.get(key))
        except Exception:  # noqa: BLE001
            pass


class FosunBroker(Broker):
    """星财富真实券商适配器（默认被安全门禁拦在门外）。"""

    venue = "fosun"
    is_live = True

    def __init__(self, cfg: AppConfig, vault: Optional[SecretVault] = None):
        self.cfg = cfg
        self.vault = vault
        self.client: Any = None
        self.connected = False
        self.blocked_reason = ""
        self.configured = False
        self.last_error = ""
        #: 不再保存 SDK 的 dump_session()：其中含 clientPrivateKey 明文 PEM，
        #: 且本项目没有任何代码读取它（审计 F-10）。
        #: 行情加密兜底状态（不改 SDK，走官方 GET 端点）
        self._quote_via_min = False
        self._market_fallback_reason = ""
        self._conn = _ConnState()
        #: 最近一次持仓查询实际使用的盘前/盘后/夜盘开关（US-P0-3 可审计）
        self._last_position_flags: Dict[str, bool] = {}
        #: 每个凭证的实际来源（env / vault / dotenv / injected / missing），只记来源不记值
        self._cred_origin: Dict[str, str] = {}
        #: 三要素来源（短名 PUB_KEY/PRIV_KEY/API_KEY），只记来源不记值（B-1）
        self._cred_sources: Dict[str, str] = {}
        #: 同源/完整性校验结果（含脱敏指纹），供 health() 如实展示
        self._cred_validation: Dict[str, Any] = {
            "status": "not_evaluated", "detail": "", "complete": False,
            "mixed": False, "sources": {}, "fingerprints": {},
        }
        #: 本实例在进程级凭证注册表中的所有者标识（B-1：引用计数）
        self._cred_owner: str = f"fosun-{uuid.uuid4().hex[:12]}"
        #: 本实例登记在类级行情推送拦截上的 owner（A-1：引用计数）
        self._mpa_guard_owner: str = ""
        #: 本次已写入进程环境的 SDK 变量名（不含值）
        self._sdk_env_injected: List[str] = []
        #: 本次**由我们创建**的变量（调用前不存在或为空）→ 失败/关闭时可安全删除
        self._sdk_env_owned: List[str] = []
        #: 调用前已存在且被我们覆盖的变量 → 仅保存"被覆盖前的值"，构造成功后立即丢弃
        self._sdk_env_prev: Dict[str, str] = {}
        self._init_client()

    # ------------------------------------------------------------------ #
    # 生产路径（/api 还是 /api/ops）
    # ------------------------------------------------------------------ #
    def _api_prefix(self) -> str:
        """SDK 实际会用的前缀。依据官方 README：``SDK_TYPE=ops`` → ``/api/ops``。"""
        sdk_type = str(self.cfg.fosun.get("sdk_type") or "").strip().lower()
        return "/api/ops" if sdk_type == "ops" else "/api"

    def routing_issue(self) -> str:
        """个人接口 + ``/api/ops`` 前缀的组合是配置冲突，返回原因；无冲突返回 ""。

        官方 SDK README「OPS 与个人接口分离」明确区分：

        * 个人用户 → ``client.trade`` / ``client.portfolio`` / ``client.account``
          （默认 ``/api``）
        * OPS 配对 → ``client.opsTrade`` / ``client.opsPortfolio`` /
          ``client.opsAccount``，且 ``SDK_TYPE=ops``（``/api/ops``）

        本适配器所有业务调用都走**个人**入口（``client.trade`` 等）。若把
        ``SDK_TYPE`` 设成 ``ops``，请求会被打到 ``/api/ops/v1/...`` 而调用的仍是
        个人方法 —— 路径与接口不匹配。这里**不擅自切换**，只把冲突报出来，
        由人工确认账户类型后再改配置。
        """
        sdk_type = str(self.cfg.fosun.get("sdk_type") or "").strip().lower()
        if sdk_type == "ops":
            return (
                "配置冲突：fosun.sdk_type=ops 会让 SDK 走 /api/ops 前缀，"
                "但本适配器只调用个人接口 client.trade/portfolio/account（官方 README "
                "标注个人路径为 /api）。个人证券账户应留空 sdk_type；OPS 配对账户才用 "
                "/api/ops 且必须走 client.opsTrade 等入口。需人工确认账户类型后修改配置。")
        return ""

    # ------------------------------------------------------------------ #
    # 行情推送认证（A-1：必须与业务接口走同一守卫）
    # ------------------------------------------------------------------ #
    def feed_auth_ready(self) -> Tuple[bool, str]:
        """行情推送认证是否可用。判据与 :meth:`_require` **完全相同**。

        独立审计 A-1：``build_feed_auth_provider()`` 原来直接摸
        ``client.auth_manager``，绕过了门禁与路由校验，在 ``sdk_type=ops``
        冲突下仍会触发 ``POST /api/ops/v1/auth/SessionCreate``。
        现在 provider 创建前必须先问这里。
        """
        ok, reason = self.cfg.gate.can_connect_broker()
        if not ok:
            return False, reason
        issue = self.routing_issue()
        if issue:
            return False, issue
        if self.client is None:
            return False, (self.blocked_reason or "SDK 未初始化")
        if getattr(self.client, "auth_manager", None) is None:
            return False, "SDK 未提供 auth_manager"
        return True, ""

    def feed_auth_headers(self) -> Dict[str, str]:
        """返回本次握手所需的认证头（每次建连现取，Session 会过期）。

        先过 :meth:`_require`（门禁 + 路由 + client），再取 Session；
        取不到或为空一律抛 :class:`BrokerError`（fail-closed，绝不匿名连接）。
        返回值只在调用栈内使用，**不落日志、不进快照**。
        """
        self._require()
        auth = getattr(self.client, "auth_manager", None)
        if auth is None:
            raise BrokerError("SDK 未提供 auth_manager，拒绝以匿名身份连接行情服务",
                              code="FEED_AUTH_UNAVAILABLE")
        try:
            session_id, _, _ = auth.get_valid_session()
        except Exception as exc:  # noqa: BLE001 - 不能把内部细节写进日志
            raise BrokerError(
                f"获取行情认证会话失败: {type(exc).__name__}",
                code="FEED_AUTH_UNAVAILABLE") from exc
        api_key = str(getattr(auth, "api_key", "") or "")
        if not api_key or not session_id:
            raise BrokerError("行情认证信息不完整，拒绝连接",
                              code="FEED_AUTH_UNAVAILABLE")
        return {"X-Api-Key": api_key, "X-Session": str(session_id)}

    # ------------------------------------------------------------------ #
    # 初始化 / 连接
    # ------------------------------------------------------------------ #
    #: 需要写进进程环境的身份密钥（官方 SDK **只从环境变量**读这两个）
    #:
    #: 注意：``FSOPENAPI_API_KEY`` **不在此列**。SDK 把它当构造参数接收
    #: （``SDKClient(api_key=...)``），不需要环境变量；写进环境只会把密钥扩散到
    #: 子进程，且让两个实例仅因 api_key 不同就互相冲突。三要素的完整性校验由
    #: :data:`_SDK_CRED_FIELDS` 负责，与注入清单分开。
    _SDK_ENV_MAP = (
        ("FSOPENAPI_SERVER_PUBLIC_KEY", "server_public_key"),
        ("FSOPENAPI_CLIENT_PRIVATE_KEY", "client_private_key"),
    )

    @staticmethod
    def _env_value(name: str) -> Optional[str]:
        """读取环境变量，并区分三种状态。

        * ``None`` → 变量**不存在**
        * ``""``   → 变量存在但为**空字符串**（``.env`` 里写成 ``KEY=`` 就是这种）
        * 其他     → **有效值**

        P0-1 的根因就在这个区分上：``os.environ.setdefault()` 只判断"键是否存在"，
        空字符串键会让它误判为"已配置"，从而挡住保险箱里的有效凭证。
        """
        import os

        if name not in os.environ:
            return None
        return os.environ.get(name)

    def _vault_value(self, env_name: str) -> Optional[str]:
        """从保险箱读一个条目；失败只记异常类型（不含值/路径），绝不静默。"""
        if self.vault is None:
            return None
        try:
            value = self.vault.get(env_name)
        except Exception as exc:  # noqa: BLE001 - 保险箱读取失败不能静默
            logger.warning("保险箱读取 %s 失败：%s", env_name, type(exc).__name__)
            return None
        return (value or None)

    def _classify_source(self, env_name: str) -> Tuple[Optional[str], str]:
        """判定单个身份要素的**来源**与取值（B-1 来源模型）。

        优先级（严格按此顺序，不允许跨来源拼接）
        --------------------------------------
        1. ``explicit_env`` 运维显式 export 的环境变量
        2. ``vault``        保险箱
        3. ``dotenv``       应用自己从 ``.env`` 载入的变量
        4. ``injected``     **本实例**自己注入进环境的变量
        5. ``missing``      都没有（调用方必须 fail-closed）

        两个关键判断
        -----------
        * ``dotenv`` 与 ``explicit_env`` 在 ``os.environ`` 里长得完全一样，必须靠
          :func:`ai_trader.config.dotenv_origin` 的**加载时来源元数据**区分：
          键名 + 载入后是否被外部重写过 + 值指纹 + 对象标记。
          Batch 4.1.2 / AUD2-B1-01 的根因就是只看"值指纹"：运维显式 export 一个
          与 ``.env`` 一模一样值之后，指纹判定仍说"来自 .env"，于是 explicit_env
          分支被跳过，Vault 越级覆盖了显式环境变量。现在同值重写会被判成
          ``explicit_env``，**它在 Vault 之前返回**，Vault 不可能再覆盖它。
        * 由**其它实例**注入的值（``_CRED_ENV.is_ours()`` 为真但不是自己）一律
          不采纳：没有 Vault 的实例不得继承别人的身份，否则 A 释放后 B 会以
          A 的身份继续跑（SR-B1-05）。
        """
        raw = self._env_value(env_name)
        nonempty = bool(raw and raw.strip())
        from_dotenv = nonempty and dotenv_origin(env_name, raw)
        self_owned = nonempty and _CRED_ENV.is_self_owned(
            env_name, raw, self._cred_owner)
        # "运维本来就 export 了这个值"（我们只是又写了一遍相同内容）
        operator_owned = nonempty and not self_owned and _CRED_ENV.is_operator_value(
            env_name, raw)
        # 纯注入值：不是自己的、也不是运维原有的 → 其它实例不得继承
        foreign = (nonempty and not self_owned and not operator_owned
                   and _CRED_ENV.is_ours(env_name, raw))
        accept_env = nonempty and not foreign

        if accept_env and not from_dotenv and not self_owned:
            return raw, SRC_EXPLICIT_ENV
        vault_value = self._vault_value(env_name)
        if vault_value:
            return vault_value, SRC_VAULT
        if accept_env and from_dotenv:
            return raw, SRC_DOTENV
        if accept_env and self_owned:
            return raw, SRC_INJECTED
        return None, SRC_MISSING

    @staticmethod
    def _credential_fingerprint(env_name: str, value: Optional[str]) -> str:
        """脱敏指纹：只暴露 sha256 前 12 位（48 bit），无法反推出原值。"""
        fp = _CredEnvRegistry.fingerprint(env_name, value)
        return f"sha256:{fp[:12]}" if fp else ""

    @staticmethod
    def _validate_source_model(sources: Dict[str, str]
                               ) -> Tuple[str, str, List[str], bool]:
        """同源 + 完整性校验，返回 ``(status, detail, missing, mixed)``。

        规则（SR-B1-04 / SR-B1-05，Batch 4.1.2 / AUD2-B1-02 修正）
        ---------------------------------------------------------
        * **跨信任域**才算 ``mixed``：只在 ``vault`` 与本地配置之间判定
          （见 :data:`_SOURCE_DOMAIN`）。``dotenv`` 与 ``explicit_env`` 同属
          ``local``，真实轮换场景不再被误杀；
        * 三要素里只要有一个缺失 → ``incomplete``（部分集合一律 fail-closed）；
        * 同时命中时先报 ``mixed``：混合身份比缺一个更难排查，也更危险。

        ``mixed`` 只表示**跨域**；域内混用（dotenv + explicit_env）会写进
        ``detail`` 供审计查看，但**不阻止**初始化 —— 前提是三要素齐全且同域。
        """
        missing = [n for n, s in sources.items() if s == SRC_MISSING]
        present = {n: s for n, s in sources.items() if s != SRC_MISSING}
        detail_src = ",".join(f"{n}={s}" for n, s in sorted(present.items()))
        domains = {_SOURCE_DOMAIN.get(s, s) for s in present.values()}
        mixed = len(domains) > 1
        if mixed:
            return "mixed", f"来源跨信任域（{detail_src}）", missing, True
        if missing:
            return "incomplete", f"缺少 {', '.join(missing)}", missing, False
        if len(set(present.values())) > 1:
            return ("ok",
                    f"本地配置内混用（{detail_src}）：三要素齐全且同属本地信任域",
                    missing, False)
        return "ok", "", [], False

    @staticmethod
    def _source_domain(sources: Dict[str, str]) -> str:
        """三要素整体所属的信任域（审计字段，不含任何值）。"""
        present = [s for s in sources.values() if s != SRC_MISSING]
        if not present:
            return "none"
        domains = {_SOURCE_DOMAIN.get(s, s) for s in present}
        if len(domains) > 1:
            return "mixed"
        return domains.pop()

    def _resolve_credentials(self, update_origin: bool = True,
                             strict: bool = True) -> Dict[str, Optional[str]]:
        """解析本实例的凭证，记录来源、完整性与同源校验状态。

        ``strict=True``（初始化路径）：来源混合抛 :class:`CredentialSourceMix`，
        要素缺失抛 :class:`CredentialIncomplete` —— 都是 fail-closed，绝不以
        "部分身份"或"混合身份"去构造 SDK。

        ``strict=False``（``health()``）：不抛异常，只把状态写入
        :attr:`_cred_validation`，让面板如实展示"为什么没连上"。

        ``update_origin=False`` 用于 ``health()``：初始化后凭证已被写进进程
        环境，再解析会把"自己注入的"当成当前来源；**决策来源**只在真正决定
        凭证的那一次（``_init_client``）记录。
        """
        import os

        creds: Dict[str, Optional[str]] = {}
        origin: Dict[str, str] = {}
        sources: Dict[str, str] = {}
        for env_name, key, short in _SDK_CRED_FIELDS:
            value, source = self._classify_source(env_name)
            creds[key] = value
            sources[env_name] = source
            origin[env_name] = _LEGACY_ORIGIN.get(source, source)

        if not update_origin and self._cred_sources:
            # health() 重解析的特殊处理（B1-02）：初始化时写进环境的 PUB/PRIV
            # 是**我们自己的注入**，直接按"当前环境"重新归类会把 explicit/dotenv
            # 误读成 vault/injected，进而把一次成功的本地配置误报成跨域 mixed。
            # 只要环境里的值仍是当初注入的那个（指纹 + owner 双重确认），就按
            # 决策当时的来源与取值汇报 —— 与 ``credential_sources`` 口径一致，
            # 也与 SDK 真正读到的环境一致。
            for env_name, key, short in _SDK_CRED_FIELDS:
                current = os.environ.get(env_name)
                if not _CRED_ENV.is_self_owned(env_name, current,
                                               self._cred_owner):
                    continue
                decided = self._cred_sources.get(short)
                if decided in _LEGACY_ORIGIN:
                    creds[key] = current
                    sources[env_name] = decided
                    origin[env_name] = _LEGACY_ORIGIN[decided]

        status, detail, missing, mixed = self._validate_source_model(sources)
        by_short = {short: sources[env_name]
                    for env_name, _key, short in _SDK_CRED_FIELDS}
        distinct = {s for s in sources.values() if s != SRC_MISSING}
        self._cred_validation = {
            "status": status,
            "detail": detail,
            "complete": not missing,
            "mixed": mixed,
            # B1-02 可审计：域内混用（dotenv+explicit_env）不阻止启动，但必须留痕
            "mixed_within_domain": bool(not mixed and len(distinct) > 1),
            "domain": self._source_domain(sources),
            # 身份一致性 = 三要素齐全且同属一个信任域（不是密码学配对校验）
            "identity_consistent": bool(status == "ok" and not missing),
            "sources": by_short,
            "fingerprints": {short: self._credential_fingerprint(env_name, creds[key])
                             for env_name, key, short in _SDK_CRED_FIELDS},
        }
        if update_origin or not self._cred_origin:
            self._cred_origin = origin
            self._cred_sources = dict(by_short)

        base_url = (os.getenv("FSOPENAPI_BASE_URL") or "").strip() \
            or str(self.cfg.fosun.get("base_url") or "").strip()
        if not base_url and self.vault is not None:
            try:
                base_url = (self.vault.get("FOSUN_BASE_URL") or "").strip()
            except Exception:  # noqa: BLE001
                base_url = ""
        creds["base_url"] = base_url
        origin["FOSUN_BASE_URL"] = "env" if os.getenv("FSOPENAPI_BASE_URL") else (
            "config" if str(self.cfg.fosun.get("base_url") or "").strip() else
            ("vault" if base_url else "missing"))
        # F-2：凭证一解析出来就登记脱敏字面量，保证"在任何日志/异常之前"生效
        _register_secret_values(creds)

        # B-1：fail-closed。缺要素 / 混来源都不得继续构造 SDK。
        if strict and status != "ok":
            if mixed:
                raise CredentialSourceMix({n: s for n, s in sources.items()
                                           if s != SRC_MISSING})
            raise CredentialIncomplete(missing)
        return creds

    def _inject_sdk_env(self, creds: Dict[str, Optional[str]]) -> List[str]:
        """把已解析的凭证写入**当前进程**环境变量，返回写入的变量名。

        与旧实现的差别（P0-1 修复点）：

        * 不再用 ``os.environ.setdefault()`` —— 它对"已存在但为空"的键无效；
        * 只写**非空**值，空值一律 ``pop``，保证 SDK 读到的要么是有效值，要么不存在；
        * 只返回变量名，**绝不**记录或返回任何值。

        B-1：三要素用 :meth:`_CredEnvRegistry.acquire_many` **原子**登记 ——
        并发下不会出现"A 的 PUB + B 的 PRIV"这种半套身份。
        """
        import os

        pairs = [(env_name, creds[key]) for env_name, key in self._SDK_ENV_MAP
                 if creds.get(key)]
        kinds: Dict[str, str] = {}
        if pairs:
            # 冲突（另一实例用不同凭证）会抛 CredentialConflict → 调用方 fail-closed
            kinds = _CRED_ENV.acquire_many(pairs, self._cred_owner)
        injected: List[str] = [name for name, _ in pairs]
        owned: List[str] = [name for name, kind in kinds.items() if kind == "created"]
        for env_name, key in self._SDK_ENV_MAP:
            if not creds.get(key) and os.environ.get(env_name) == "":
                # 解析不出凭证：只清掉空值，不动调用前已存在的有效值
                os.environ.pop(env_name, None)
        # 非密钥开关：仍用 setdefault（尊重运维显式设置），留空则用配置默认值
        sdk_type = str(self.cfg.fosun.get("sdk_type") or "").strip()
        if sdk_type and not (os.environ.get("SDK_TYPE") or "").strip():
            os.environ["SDK_TYPE"] = sdk_type
        lang = str(self.cfg.fosun.get("lang") or "").strip()
        if lang and not (os.environ.get("FSOPENAPI_LANG") or "").strip():
            os.environ["FSOPENAPI_LANG"] = lang
        self._sdk_env_injected = injected
        self._sdk_env_owned = owned
        # 旧值由注册表保管（仅引用归零时恢复），实例内不再保留私钥副本
        self._sdk_env_prev = {}
        return injected

    def release_credentials(self, restore_previous: bool = False) -> Dict[str, Any]:
        """按**进程级引用计数**释放本实例登记的身份变量（B-1）。

        * 只减少自己的引用；其他实例仍持有时，环境变量**保持不动**；
        * 引用归零时才清理：调用前有值→恢复旧值，否则删除；
        * 清理前核对环境值仍是我们写入的那个，避免误删；
        * 重复调用安全（幂等），返回值只含变量名与动作，不含任何值。

        ``restore_previous`` 保留为兼容参数：恢复语义已由注册表统一处理。
        """
        res = _CRED_ENV.release(self._cred_owner)
        touched = set(res.get("popped", [])) | set(res.get("restored", []))
        self._sdk_env_injected = [n for n in self._sdk_env_injected if n not in touched]
        self._sdk_env_owned = []
        self._sdk_env_prev = {}
        return res

    def _install_market_push_guard(self) -> bool:
        """掐断 SDK 自带的行情推送入口（A-1）：**类级 + 实例级**双层。

        为什么两层
        ----------
        * 实例级（``client.market_push = _MarketPushDisabled()``）只保护当前
          client：一旦再出现一个 client、或 SDK 延迟创建 ``market_push``，
          这层就绕过去了。
        * 类级（替换 ``MarketPushAPI`` 的 5 个核心方法）覆盖**所有**实例，
          包括未来创建的。``subscribeQuote/subscribeMin/...`` 全是
          ``subscribe`` 的薄包装，因此拦 5 个即可封死整族入口。
        * **不能**拦 ``MarketPushAPI.__init__``：``SDKClient.__init__`` 会执行
          ``self.market_push = MarketPushAPI(self)``（``fsopenapi/__init__.py:40``），
          拦构造等于连 ``SDKClient(...)`` 都建不出来。

        Fail-closed
        -----------
        类级安装失败（方法缺失 / 类不可写 / 回读校验不一致）→ 全量回滚并返回
        False，调用方据此拒绝完成初始化。实例级替换失败**不致命**（类级已拦），
        但会记录告警，并由 ``health()`` 的明细字段暴露。

        幂等
        ----
        重复调用只增加引用计数；``close()`` 释放本实例的引用，最后一个释放者
        才恢复原始方法（多 client 不会提前拆掉别人的保护）。
        """
        client = self.client
        if client is None:
            return False

        current = getattr(client, "market_push", None)
        target_cls = type(current) if current is not None else None
        if target_cls is not None and target_cls is not _MarketPushDisabled:
            ok, info = _MPA_GUARD.install(target_cls, self._cred_owner)
            self._mpa_guard_owner = self._cred_owner if ok else ""
            if not ok:
                logger.error("无法安装 SDK 行情推送类级拦截（%s），"
                             "拒绝以存在认证旁路的状态完成初始化",
                             info.get("reason", "unknown"))
                return False
        else:
            self._mpa_guard_owner = ""

        # 实例级哨兵（纵深防御）。替换失败不影响结论：类级已经拦住调用。
        if isinstance(getattr(client, "market_push", None), _MarketPushDisabled):
            return True
        try:
            setattr(client, "market_push", _MarketPushDisabled())
        except Exception as exc:  # noqa: BLE001 - 只读属性/__slots__ 等
            if _MPA_GUARD.status().get("installed"):
                logger.warning("client.market_push 实例属性不可替换（%s），"
                               "已由类级拦截兜底", type(exc).__name__)
                return True
            logger.error("无法替换 client.market_push（SDK 属性不可写），"
                         "拒绝以存在认证旁路的状态完成初始化")
            return False
        ok = isinstance(getattr(client, "market_push", None), _MarketPushDisabled)
        if not ok and not _MPA_GUARD.status().get("installed"):
            logger.error("client.market_push 替换后校验失败，拒绝继续（fail-closed）")
            return False
        return True

    def _remove_market_push_guard(self) -> Dict[str, Any]:
        """释放本实例的类级引用与实例级哨兵（精确恢复，幂等）。"""
        owner = getattr(self, "_mpa_guard_owner", "")
        info: Dict[str, Any] = {"released": False, "restored": [], "skipped": [],
                                "instance_restored": False}
        if owner:
            info.update(_MPA_GUARD.release(owner))
            self._mpa_guard_owner = ""
        if self.client is not None and isinstance(
                getattr(self.client, "market_push", None), _MarketPushDisabled):
            try:
                # 没有原始对象可恢复（构造时就被替换）→ 直接删除，回到"无此属性"
                delattr(self.client, "market_push")
                info["instance_restored"] = True
            except Exception:  # noqa: BLE001
                pass
        return info

    def close(self) -> Dict[str, Any]:
        """关闭适配器：释放凭证引用与行情推送拦截（幂等）。

        只清**本实例**登记的东西：

        * 凭证按进程级引用计数释放，且只在环境值仍等于我们写入的那个时才动；
        * 类级拦截按 owner 引用计数释放，最后一个释放者才恢复原始方法，
          且只恢复"仍然是我们的包装"的方法（第三方改过就跳过）。

        不触碰网络、不关闭 SDK 的 HTTP Session（本项目不持有长连接）。
        """
        released = self.release_credentials()
        guard = self._remove_market_push_guard()
        self.connected = False
        return {"credentials": released, "market_push_guard": guard,
                "configured": self.configured}

    def _init_client(self) -> None:
        ok, reason = self.cfg.gate.can_connect_broker()
        if not ok:
            self.blocked_reason = reason
            self.connected = False
            return

        # B-1：来源混合 / 三要素不完整 → 在构造 SDK 之前 fail-closed。
        # 构造期不抛异常（面板要能显示原因），但绝不进入"看起来已配置"的状态。
        try:
            creds = self._resolve_credentials()
        except BrokerError as exc:
            self.configured = False
            detail = self._cred_validation.get("detail") or str(exc)
            label = ("缺少凭证" if exc.code == "CREDENTIAL_INCOMPLETE"
                     else "凭证来源冲突")
            self.blocked_reason = (
                f"{label}: {detail}（{exc.code}，未连接，未读取任何账户数据）")
            return

        missing = [k for k, v in (
            ("FSOPENAPI_SERVER_PUBLIC_KEY", creds["server_public_key"]),
            ("FSOPENAPI_CLIENT_PRIVATE_KEY", creds["client_private_key"]),
            ("FSOPENAPI_API_KEY", creds["api_key"]),
            ("FOSUN base_url", creds["base_url"]),
        ) if not v]

        if missing:
            self.blocked_reason = f"缺少凭证: {', '.join(missing)}（未连接，未读取任何账户数据）"
            self.configured = False
            return

        try:
            # 官方 SDK 只认环境变量，这里注入的仅限当前进程（不落盘、不打印值）
            # 冲突（另一实例用不同凭证）会抛 CredentialConflict → 走下面的 fail-closed
            self._inject_sdk_env(creds)
            logger.info("SDK 身份变量已注入进程环境：%s（来源：%s）",
                        ",".join(self._sdk_env_injected),
                        ",".join(f"{k}={v}" for k, v in self._cred_origin.items()
                                 if k in self._sdk_env_injected))

            from fsopenapi import SDKClient  # 延迟导入，未安装也不影响模拟盘

            import os as _os

            self.client = SDKClient(
                base_url=creds["base_url"],
                api_key=creds["api_key"],
                logging_enable=True,
                log_body=False,
                ssl_verify=bool(self.cfg.fosun.get("ssl_verify", True)),
            )
            self._install_http_timeout()
            # P0-1 收口：确认 SDK 真能从进程环境读到**非空**身份密钥，
            # 读不到就 fail-closed，绝不带着空凭证进入"看起来已配置"的状态
            unreadable = [name for name, _ in self._SDK_ENV_MAP
                          if not (_os.environ.get(name) or "").strip()]
            if unreadable:
                raise RuntimeError(
                    f"进程环境缺少有效身份密钥: {', '.join(unreadable)}（fail-closed）")
            # 构造成功：不再需要"被覆盖前的旧值"，立即丢弃（减少内存中的凭证副本）
            self._sdk_env_prev = {}
            # A-1：禁用 SDK 自带的行情推送入口。必须在 configured=True 之前完成，
            # 且**不假设替换成功**：失败即 fail-closed（走下面的 except 回滚）。
            if not self._install_market_push_guard():
                raise RuntimeError(
                    "无法禁用 SDK 自带行情推送入口（market_push 属性不可替换），"
                    "拒绝以存在认证旁路的状态完成初始化")
            self.configured = True
            self.blocked_reason = ""
        except Exception as exc:  # noqa: BLE001 - 任何初始化失败都要如实暴露
            self.configured = False
            # P2（审计 F-1）：构造失败必须按所有权清理本次注入的身份变量，
            # 但**不得**误删调用前已存在的有效变量（restore_previous 会把旧值写回）。
            released = self.release_credentials(restore_previous=True)
            # A-1：失败路径同样要释放类级拦截的引用计数，否则会把"保护"
            # 永久留在 SDK 类上（后续实例都调不了真实推送，且无法自愈）。
            self._remove_market_push_guard()
            self.blocked_reason = (
                f"SDK 初始化失败: {type(exc).__name__}: {exc}"
                f"（已清理注入变量 {len(released['popped'])} 个，"
                f"回滚 {len(released['restored'])} 个）")
            self.client = None

    # ------------------------------------------------------------------ #
    # HTTP 超时（P1-3）
    # ------------------------------------------------------------------ #
    def _timeout_tuple(self) -> Tuple[float, float]:
        f = self.cfg.fosun
        return (float(f.get("http_connect_timeout", 10) or 10),
                float(f.get("http_read_timeout", 30) or 30))

    def _install_http_timeout(self) -> None:
        """给 SDK 的 Session 注入默认超时。

        官方 SDK **完全没有设置 timeout**（审计发现 P1-6），网关挂起时进程会永久
        阻塞。这里在**实例级别**包装 ``_http_session.request``，不改 SDK 源码：

        * 业务请求全部走这个 Session，因此覆盖 ``client.*`` 的所有调用
        * 握手走 ``requests.post``，另行用 :meth:`_create_session_guarded` 限时
        """
        if self.client is None:
            return
        sess = getattr(self.client, "_http_session", None)
        if sess is None:
            return
        original = sess.request
        if getattr(original, "_aitrader_timeout_patched", False):
            return
        connect_to, read_to = self._timeout_tuple()

        def _request_with_timeout(method, url, **kwargs):
            kwargs.setdefault("timeout", (connect_to, read_to))
            return original(method, url, **kwargs)

        _request_with_timeout._aitrader_timeout_patched = True  # type: ignore[attr-defined]
        sess.request = _request_with_timeout
        logger.info("已为 SDK HTTP 会话注入超时 connect=%.0fs read=%.0fs", connect_to, read_to)

    def _create_session_guarded(self, timeout: Optional[float] = None):
        """带墙钟超时的握手（SDK 的 ``requests.post`` 无 timeout）。"""
        import concurrent.futures as cf

        connect_to, read_to = self._timeout_tuple()
        limit = float(timeout if timeout is not None else (connect_to + read_to + 5))
        with cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="fs-handshake") as ex:
            fut = ex.submit(self.client.session.create_session)
            try:
                return fut.result(timeout=limit)
            except cf.TimeoutError as exc:
                raise BrokerError(
                    f"建连超时（>{limit:.0f}s），网关无响应", code="CONNECT_TIMEOUT",
                    retryable=True) from exc

    def connect(self) -> bool:
        """显式建连（会发真实网络请求）。被门禁拒绝时抛 BrokerNotConfigured。"""
        ok, reason = self.cfg.gate.can_connect_broker()
        if not ok:
            raise BrokerNotConfigured(reason)
        issue = self.routing_issue()
        if issue:
            raise BrokerNotConfigured(issue)
        if self.client is None:
            raise BrokerNotConfigured(self.blocked_reason or "SDK 未初始化")
        try:
            self._create_session_guarded()
            # 审计 F-10：不再保存 dump_session()（含 clientPrivateKey 明文 PEM，
            # 且全仓无人读取）。会话续期由 SDK 的 auth_manager 自己维护。
            self.connected = True
            self._conn.connects += 1
            return True
        except Exception as exc:  # noqa: BLE001
            self.connected = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise BrokerError(f"建连失败: {self.last_error}", code="CONNECT_FAILED",
                              retryable=True)

    def _require(self) -> Any:
        """所有个人接口入口的统一守卫。

        P1（审计 F-3）：路由冲突必须在这里拦住。``connect()`` 处的检查只覆盖
        建连，而 ``get_*``/``place_order`` 等入口并不经过 ``connect()`` ——
        只查一处等于没查。
        """
        ok, reason = self.cfg.gate.can_connect_broker()
        if not ok:
            raise BrokerNotConfigured(reason)
        issue = self.routing_issue()
        if issue:
            raise BrokerNotConfigured(issue)
        if self.client is None:
            raise BrokerNotConfigured(self.blocked_reason or "SDK 未初始化")
        return self.client

    # ------------------------------------------------------------------ #
    # 断线重连 / 有限重试（P1-7）
    # ------------------------------------------------------------------ #
    def _is_auth_error(self, exc: BaseException) -> bool:
        name = type(exc).__name__
        if name in ("AuthenticationError", "PermissionError"):
            return True
        msg = str(exc).lower()
        return ("session" in msg and ("invalid" in msg or "expired" in msg)) or \
               ("401" in msg) or ("鉴权" in str(exc))

    def _is_network_error(self, exc: BaseException) -> bool:
        if isinstance(exc, (BrokerError, BrokerNotConfigured)):
            return getattr(exc, "code", "") in ("NETWORK", "CONNECT_TIMEOUT", "EMPTY_QUOTE")
        name = type(exc).__name__
        return name in ("Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError",
                        "ChunkedEncodingError", "SSLError")

    def _reconnect(self, reason: str) -> bool:
        """重建会话。返回是否成功。"""
        if self.client is None:
            return False
        try:
            self._create_session_guarded()
            self.connected = True
            self._conn.connects += 1
            self._conn.last_reason = reason
            logger.warning("会话已重建（原因：%s）", reason)
            return True
        except Exception as exc:  # noqa: BLE001
            self.connected = False
            self.last_error = f"重连失败: {type(exc).__name__}: {exc}"
            logger.error("重连失败：%s", self.last_error)
            return False

    def _read(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        """**只读**操作的重试包装。

        只重试幂等读取；``place_order`` 绝不走这里（见 ``place_order`` 注释）。
        """
        max_attempts = int(self.cfg.fosun.get("read_retry_attempts", 2) or 2)
        last_exc: Optional[BaseException] = None
        for attempt in range(1, max_attempts + 1):
            try:
                return fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001
                last_exc = exc
                if self._is_auth_error(exc) and attempt < max_attempts:
                    if self._reconnect(f"鉴权失败: {exc}"):
                        self._conn.retries += 1
                        continue
                    raise
                if self._is_network_error(exc) and attempt < max_attempts:
                    self._conn.retries += 1
                    time.sleep(0.2 * attempt)
                    continue
                raise
        if last_exc:
            raise last_exc
        raise BrokerError("读取失败（未知原因）", code="READ_FAILED")

    # ------------------------------------------------------------------ #
    # 账户 / 组合
    # ------------------------------------------------------------------ #
    def get_accounts(self) -> List[AccountInfo]:
        client = self._require()
        payload = _payload(self._read(client.account.list_accounts))
        rows, shape_ok = _extract_rows(payload)
        if not shape_ok:
            raise BrokerError(
                f"账户列表响应结构无法解析（payload keys={list(payload)[:6] if isinstance(payload, dict) else type(payload).__name__}）",
                code="PARSE_MISMATCH")
        out = []
        for item in rows:
            out.append(AccountInfo(
                sub_account_id=str(_first(item, ("subAccountId", "accountId", "id"), "")),
                name=str(_first(item, ("accountName", "name", "subAccountName"), "")),
                currency=str(_first(item, ("currency", "ccy"), "")),
                account_type=str(_first(item, ("accountType", "type", "subAccountClass"), "")),
                market=str(_first(item, ("market", "marketCode"), "")),
                raw=item,
            ))
        return out

    def get_summary(self, currency: str = "") -> AccountSummary:
        client = self._require()
        sub = str(self.cfg.fosun.get("sub_account_id") or "") or None
        apply_acc = str(self.cfg.fosun.get("apply_account_id") or "") or None
        resp = self._read(
            client.portfolio.get_assets_summary,
            sub_account_id=sub, currency=currency or None, apply_account_id=apply_acc,
        )
        payload = _payload(resp) or {}
        rows, shape_ok = _extract_rows(payload, list_keys=("list", "items", "summaryList",
                                                          "currencyList"))
        if not shape_ok:
            raise BrokerError(
                f"资金汇总响应结构无法解析（payload keys={list(payload)[:6] if isinstance(payload, dict) else '?'}）",
                code="PARSE_MISMATCH")
        row = rows[0] if rows else (payload if isinstance(payload, dict) else {})
        cash = _fnum(_first(row, _CASH_KEYS))
        mv = _fnum(_first(row, _MV_KEYS))
        eq = _fnum(_first(row, _EQUITY_KEYS), cash + mv)
        # 关键：券商有没有真的给日内盈亏？没给就不能假装是 0
        day_pnl_known = _has(row, _DAY_PNL_KEYS)
        return AccountSummary(
            currency=currency or str(_first(row, ("currency", "ccy"), currency_of("hk"))),
            cash=cash, market_value=mv, equity=eq,
            day_pnl=_fnum(_first(row, _DAY_PNL_KEYS)) if day_pnl_known else 0.0,
            buying_power=_fnum(_first(row, ("buyingPower", "maxBuyQty", "availableFund")), cash),
            source=self.venue,
            day_pnl_known=day_pnl_known,
        )

    # ---- 美股盘前/盘后/夜盘持仓（US-P0-3）----------------------------- #
    def _position_session_flags(self, overrides: Optional[Dict[str, Any]] = None
                                ) -> Dict[str, bool]:
        """决定 ``useUsPre/useUsPost/useUsNight`` 三个开关。

        默认**全部为 True**：宁可多看到持仓（多一次风控），也不能漏掉某一段
        时段的敞口。配置可用 ``fosun.position_sessions`` 覆盖，
        但**不能关成"漏掉风险"** —— 若有人把它设成 False，health() 会显式报出来。
        """
        cfg = (self.cfg.fosun.get("position_sessions") or {})
        out: Dict[str, bool] = {}
        for key in ("use_us_pre", "use_us_post", "use_us_night"):
            val = (overrides or {}).get(key)
            if val is None:
                val = cfg.get(key, True)
            out[key] = bool(val)
        return out

    def get_positions(self, **kwargs: Any) -> List[Position]:
        """查询持仓（含美股盘前/盘后/夜盘）。

        US-P0-3：必须把 ``useUsPre/useUsPost/useUsNight`` 传给官方接口，
        否则盘前/盘后/夜盘建仓的持仓可能不在返回结果里 ——
        那些持仓会完全脱离止损与敞口控制。

        查询失败（网络、结构不符、缺关键字段）一律抛 :class:`BrokerError`，
        **绝不返回空列表**：空列表会被上层解读为"没有持仓"而 fail-open。
        """
        client = self._require()
        sub = str(self.cfg.fosun.get("sub_account_id") or "") or None
        apply_acc = str(self.cfg.fosun.get("apply_account_id") or "") or None
        flags = self._position_session_flags(kwargs)
        self._last_position_flags = dict(flags)
        resp = self._read(
            client.portfolio.get_holdings,
            sub_account_id=sub, start=0, count=200, apply_account_id=apply_acc,
            use_us_pre=flags["use_us_pre"],
            use_us_post=flags["use_us_post"],
            use_us_night=flags["use_us_night"],
        )
        payload = _payload(resp)
        rows, shape_ok = _extract_rows(payload)

        if not shape_ok:
            # 审计 P1-2：绝不能静默返回 []，否则风控会以为没有持仓而 fail-open
            raise BrokerError(
                "持仓响应结构无法解析，拒绝以「空持仓」继续交易"
                f"（payload 类型={type(payload).__name__}，"
                f"keys={list(payload)[:8] if isinstance(payload, dict) else 'n/a'}）",
                code="PARSE_MISMATCH")

        out: List[Position] = []
        for item in rows:
            sym_raw = _first(item, _SYMBOL_KEYS)
            if not sym_raw:
                raise BrokerError(
                    f"持仓行缺少标的代码字段（keys={list(item)[:8]}），拒绝以不完整持仓继续",
                    code="PARSE_MISMATCH")
            mkt = str(_first(item, ("marketCode", "market"), market_of(str(sym_raw))))
            sym = str(sym_raw)
            if mkt and not sym.lower().startswith(mkt.lower()):
                sym = f"{mkt}{sym}".lower()
            out.append(Position(
                symbol=sym,
                market=mkt,
                qty=_fnum(_first(item, _QTY_KEYS)),
                avg_cost=_fnum(_first(item, _COST_KEYS)),
                last_price=_fnum(_first(item, _LAST_KEYS)),
                currency=str(_first(item, ("currency", "ccy"), currency_of(mkt + "0"))),
                name=str(_first(item, ("stockName", "name"), "")),
                # 券商若提供时段字段则带上；未提供留空（不猜）
                session=str(_first(item, ("tradeSession", "sessionType", "session"), "")),
            ))
        return out

    # ------------------------------------------------------------------ #
    # 行情
    # ------------------------------------------------------------------ #
    def get_quote(self, symbol: str) -> Quote:
        client = self._require()
        workaround = str(self.cfg.fosun.get("market_encryption_workaround", "auto")).lower()

        if self._quote_via_min:
            return self._quote_from_min(client, symbol)

        try:
            payload = _payload(self._read(client.market.quote, codes=[symbol]))
            rows, shape_ok = _extract_rows(payload)
            if not shape_ok or not rows:
                raise BrokerError(f"未取到 {symbol} 行情（响应结构不符）",
                                  code="EMPTY_QUOTE", retryable=True)
            row = rows[0]
            last = _fnum(_first(row, _LAST_KEYS))
            if last <= 0:
                raise BrokerError(f"{symbol} 行情最新价为 0 或缺失",
                                  code="EMPTY_QUOTE", retryable=True)
            return self._quote_from_row(symbol, row, "fosun")
        except Exception as exc:  # noqa: BLE001
            if workaround == "off" or not _looks_like_payload_error(exc):
                raise
            logger.warning(
                "行情 POST 失败（%s: %s），改用官方 GET /v1/market/min 兜底。"
                "这是客户端绕行，未修改 SDK。", type(exc).__name__, exc)
            self._quote_via_min = True
            self._market_fallback_reason = f"{type(exc).__name__}: {exc}"
            return self._quote_from_min(client, symbol)

    def _quote_from_row(self, symbol: str, row: Dict[str, Any], source: str) -> Quote:
        return Quote(
            symbol=symbol, market=market_of(symbol),
            last=_fnum(_first(row, _LAST_KEYS)),
            open=_fnum(_first(row, _OPEN_KEYS)),
            high=_fnum(_first(row, _HIGH_KEYS)),
            low=_fnum(_first(row, _LOW_KEYS)),
            prev_close=_fnum(_first(row, _PREV_CLOSE_KEYS)),
            volume=_fnum(_first(row, _VOLUME_KEYS)),
            ts=now_iso(), source=source,
        )

    def _quote_from_min(self, client: Any, symbol: str) -> Quote:
        """用官方 GET ``/v1/market/min``（不加密）取最新价。

        这是**官方文档里的公开端点**，不是对 SDK 的私有补丁；只在
        POST 行情因加密问题不可用时作为降级取价源。
        """
        payload = _payload(self._read(client.market.min, symbol, count=1))
        rows, shape_ok = _extract_rows(payload, list_keys=("list", "items", "data", "min"))
        if not shape_ok or not rows:
            raise BrokerError(
                f"GET /v1/market/min 也未取到 {symbol} 行情（兜底失败）",
                code="EMPTY_QUOTE", retryable=True)
        row = rows[-1]
        last = _fnum(_first(row, _LAST_KEYS + ("curPrice", "now")))
        if last <= 0:
            raise BrokerError(f"{symbol} 兜底行情最新价缺失", code="EMPTY_QUOTE",
                              retryable=True)
        return Quote(
            symbol=symbol, market=market_of(symbol), last=last,
            open=_fnum(_first(row, _OPEN_KEYS)),
            high=_fnum(_first(row, _HIGH_KEYS)),
            low=_fnum(_first(row, _LOW_KEYS)),
            volume=_fnum(_first(row, _VOLUME_KEYS)),
            ts=now_iso(), source="fosun_market_min_fallback",
        )

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        for s in symbols:
            try:
                out[s] = self.get_quote(s)
            except BrokerError as exc:
                logger.warning("批量取价失败 %s: %s", s, exc)
        return out

    def get_klines(self, symbol: str, ktype: str = "day", num: int = 120) -> List[Candle]:
        client = self._require()
        payload = _payload(self._read(client.market.kline, symbol, ktype=ktype, num=num))
        rows, shape_ok = _extract_rows(payload, list_keys=("list", "items", "klines", "data"))
        if not shape_ok:
            raise BrokerError(
                f"{symbol} K 线响应结构无法解析（payload keys="
                f"{list(payload)[:8] if isinstance(payload, dict) else '?'}）",
                code="PARSE_MISMATCH")
        out: List[Candle] = []
        for row in rows:
            out.append(Candle(
                ts=str(_first(row, ("time", "ts", "date", "timestamp"), "")),
                open=_fnum(_first(row, _OPEN_KEYS)),
                high=_fnum(_first(row, _HIGH_KEYS)),
                low=_fnum(_first(row, _LOW_KEYS)),
                close=_fnum(_first(row, _LAST_KEYS)),
                volume=_fnum(_first(row, _VOLUME_KEYS)),
            ))
        if not out:
            raise BrokerError(f"{symbol} K 线为空，无法分析", code="EMPTY_KLINES")
        return out

    # ------------------------------------------------------------------ #
    # 交易
    # ------------------------------------------------------------------ #
    def _require_order_permission(self) -> None:
        ok, reason = self.cfg.gate.can_send_real_order()
        if not ok:
            raise BrokerNotConfigured(f"真实下单被安全门禁拒绝: {reason}")

    @staticmethod
    def make_client_id(intent: Any) -> str:
        """生成稳定 ``clientId``。

        同一次决策（decision_id 相同）永远得到同一个 clientId —— 这样即使
        上层重试，券商侧也能识别为同一笔，不会变成两笔。
        """
        seed = f"{getattr(intent, 'decision_id', '')}|{getattr(intent, 'signal_id', '')}|" \
               f"{getattr(intent, 'symbol', '')}|{getattr(intent, 'side', '')}"
        import hashlib

        h = int(hashlib.sha256(seed.encode()).hexdigest()[:12], 16)
        return str(h % 2_000_000_000)

    def place_order(self, req: OrderRequest) -> OrderResult:
        """真实下单。

        **绝不重试**：一旦请求发出，就无法知道券商是否受理；重试可能变成两笔单。
        结果不确定时抛 :class:`OrderStateUnknown`，由上层用 client_id 对账。

        绝不编造 ``orderId``：券商没给就是没给，状态标 ``unknown``。
        """
        self._require_order_permission()
        client = self._require()
        if not self.cfg.fosun.get("apply_account_id") and str(
            self.cfg.fosun.get("sdk_type", "")
        ).lower() == "ops":
            raise BrokerNotConfigured("OPS 模式必须配置 apply_account_id")

        client_id = self.make_client_id(req)
        direction = 1 if req.side == "buy" else 2
        order_type = 1 if (req.order_type or "limit").lower() == "limit" else 3
        kwargs: Dict[str, Any] = dict(
            sub_account_id=self.cfg.fosun.get("sub_account_id"),
            stock_code=req.symbol,
            direction=direction,
            order_type=order_type,
            quantity=str(req.qty),
            market_code=req.market or market_of(req.symbol),
            currency=req.currency or currency_of(req.symbol),
            client_id=int(client_id),
        )
        if req.price is not None:
            kwargs["price"] = str(req.price)
        if req.stop_loss:
            kwargs["stop_loss_trig_price"] = str(req.stop_loss)
        if req.take_profit:
            kwargs["profit_trig_price"] = str(req.take_profit)
        if self.cfg.fosun.get("apply_account_id"):
            kwargs["apply_account_id"] = self.cfg.fosun.get("apply_account_id")
        kwargs = {k: v for k, v in kwargs.items() if v is not None}

        logger.info("真实下单请求(已通过门禁) symbol=%s side=%s qty=%s clientId=%s",
                    req.symbol, req.side, req.qty, client_id)

        # ---- 唯一一次调用：不重试、不降级 ----
        try:
            resp = client.trade.create_order(**kwargs)
        except BaseException as exc:  # noqa: BLE001
            if self._is_auth_error(exc):
                raise BrokerNotConfigured(
                    f"下单前鉴权失败，未发出委托（可安全重试）: {exc}") from exc
            # 超时 / 断线 / 未知异常：券商可能已受理 → 状态未知，禁止重试
            raise OrderStateUnknown(
                f"下单请求结果未知（{type(exc).__name__}: {exc}），"
                f"必须以 clientId={client_id} 对账，禁止重试下单",
                client_id=client_id, symbol=req.symbol, side=req.side,
                qty=float(req.qty), price=req.price,
                code="ORDER_STATE_UNKNOWN", cause=f"{type(exc).__name__}: {exc}",
            ) from exc

        payload = _payload(resp) or {}
        row = payload if isinstance(payload, dict) else {}
        broker_oid = str(_first(row, _ORDER_ID_KEYS, "") or "")

        # 完全空响应 → 无从确认
        if not row:
            raise OrderStateUnknown(
                "券商返回空业务数据，无法确认订单是否受理",
                client_id=client_id, symbol=req.symbol, side=req.side,
                qty=float(req.qty), price=req.price,
                code="EMPTY_ORDER_RESPONSE", cause="empty payload",
            )

        if not broker_oid:
            # 有响应体但没订单号：状态未知，但已经拿到了服务端信息，交给对账
            return OrderResult(
                order_id=f"UNCONFIRMED-{client_id}",
                broker_order_id="",
                symbol=req.symbol, market=req.market, side=req.side,
                qty=float(req.qty), price=req.price,
                status="unknown",
                venue=self.venue, ts=now_iso(),
                client_id=client_id,
                reconcile_note=("券商响应未包含 orderId，状态标记为 unknown；"
                                "禁止当作 submitted 处理"),
                raw=row,
            )

        status = str(_first(row, ("status", "orderStatus"), "") or "").strip().lower()
        if not status:
            status = "unknown"
        return OrderResult(
            order_id=broker_oid,
            broker_order_id=broker_oid,
            symbol=req.symbol, market=req.market, side=req.side,
            qty=float(req.qty), price=req.price,
            status=status,
            filled_qty=_fnum(_first(row, ("filledQuantity", "dealtQty"))),
            avg_fill_price=_fnum(_first(row, ("avgPrice", "dealtPrice"))) or None,
            commission=_fnum(_first(row, ("totalFee", "commission"))),
            venue=self.venue, ts=now_iso(),
            client_id=client_id,
            raw=row,
        )

    def cancel_order(self, order_id: str) -> OrderResult:
        ok, reason = self.cfg.gate.can_cancel_real_order()
        if not ok:
            raise BrokerNotConfigured(f"真实撤单被安全门禁拒绝: {reason}")
        client = self._require()
        try:
            resp = client.trade.cancel_order(
                order_id=order_id,
                sub_account_id=self.cfg.fosun.get("sub_account_id"),
                apply_account_id=self.cfg.fosun.get("apply_account_id") or None,
            )
        except BaseException as exc:  # noqa: BLE001
            if self._is_auth_error(exc):
                raise BrokerNotConfigured(
                    f"撤单鉴权失败，未发出撤单请求: {exc}") from exc
            # 撤单是幂等动作，可以安全重试一次
            if self._is_network_error(exc):
                try:
                    resp = client.trade.cancel_order(
                        order_id=order_id,
                        sub_account_id=self.cfg.fosun.get("sub_account_id"),
                        apply_account_id=self.cfg.fosun.get("apply_account_id") or None,
                    )
                except BaseException as exc2:  # noqa: BLE001
                    raise BrokerError(
                        f"撤单请求失败（可重试）: {type(exc2).__name__}: {exc2}",
                        code="CANCEL_FAILED", retryable=True) from exc2
            else:
                raise
        payload = _payload(resp) or {}
        row = payload if isinstance(payload, dict) else {}
        status = str(_first(row, ("status", "orderStatus"), "") or "").strip().lower()
        return OrderResult(
            order_id=order_id, broker_order_id=order_id, symbol="", market="", side="",
            qty=0.0, price=None,
            status=status or "cancel_requested",
            venue=self.venue, ts=now_iso(),
            reconcile_note="撤单请求已发出；最终状态需再查一次订单详情",
            raw=row,
        )

    def get_order(self, order_id: str) -> Optional[OrderResult]:
        client = self._require()
        resp = self._read(
            client.trade.get_order_detail,
            sub_account_id=self.cfg.fosun.get("sub_account_id"),
            order_id=order_id,
            apply_account_id=self.cfg.fosun.get("apply_account_id") or None,
        )
        payload = _payload(resp) or {}
        if not isinstance(payload, dict) or not payload:
            return None
        return OrderResult(
            order_id=str(_first(payload, _ORDER_ID_KEYS, order_id)),
            broker_order_id=str(_first(payload, _ORDER_ID_KEYS, order_id)),
            symbol=str(_first(payload, _SYMBOL_KEYS, "")),
            market=str(_first(payload, ("marketCode",), "")),
            side="buy" if str(_first(payload, ("direction",), "1")) == "1" else "sell",
            qty=_fnum(_first(payload, ("quantity", "qty"))),
            price=_fnum(_first(payload, ("price",))),
            status=str(_first(payload, ("status", "orderStatus"), "") or "unknown").lower(),
            filled_qty=_fnum(_first(payload, ("filledQuantity", "dealtQty"))),
            avg_fill_price=_fnum(_first(payload, ("avgPrice", "dealtPrice"))) or None,
            commission=_fnum(_first(payload, ("totalFee", "commission"))),
            venue=self.venue, ts=now_iso(), raw=payload,
        )

    def list_orders(self, limit: int = 50) -> List[OrderResult]:
        client = self._require()
        resp = self._read(
            client.trade.list_orders,
            sub_account_id=self.cfg.fosun.get("sub_account_id"),
            apply_account_id=self.cfg.fosun.get("apply_account_id") or None,
            start=0, count=limit,
        )
        payload = _payload(resp)
        rows, shape_ok = _extract_rows(payload)
        if not shape_ok:
            raise BrokerError(
                f"订单列表响应结构无法解析（payload keys="
                f"{list(payload)[:8] if isinstance(payload, dict) else '?'}）",
                code="PARSE_MISMATCH")
        out: List[OrderResult] = []
        for row in rows:
            oid = str(_first(row, _ORDER_ID_KEYS, "") or "")
            out.append(OrderResult(
                order_id=oid, broker_order_id=oid,
                symbol=str(_first(row, _SYMBOL_KEYS, "")),
                market=str(_first(row, ("marketCode",), "")),
                side="buy" if str(_first(row, ("direction",), "1")) == "1" else "sell",
                qty=_fnum(_first(row, ("quantity", "qty"))),
                price=_fnum(_first(row, ("price",))),
                status=str(_first(row, ("status", "orderStatus"), "") or "unknown").lower(),
                filled_qty=_fnum(_first(row, ("filledQuantity", "dealtQty"))),
                avg_fill_price=_fnum(_first(row, ("avgPrice", "dealtPrice"))) or None,
                venue=self.venue,
                ts=str(_first(row, ("createTime", "orderTime"), now_iso())),
                client_id=str(_first(row, ("clientId", "client_id"), "") or ""),
                raw=row,
            ))
        return out

    # ------------------------------------------------------------------ #
    # 订单对账（P0-1）
    # ------------------------------------------------------------------ #
    def reconcile_order(self, client_id: str, symbol: str = "",
                        side: str = "", qty: float = 0.0) -> Optional[OrderResult]:
        """按 ``client_id`` 查券商侧真实订单。

        返回 ``None`` 仅代表"查询成功且确实没有这笔单"；
        查询本身失败时抛 :class:`BrokerError`（调用方必须继续视为 unknown）。
        """
        client = self._require()
        if not client_id:
            raise BrokerError("缺少 client_id，无法对账", code="NO_CLIENT_ID")

        # 1) 首选：SDK 支持的 clientId 过滤
        try:
            resp = self._read(
                client.trade.list_orders,
                sub_account_id=self.cfg.fosun.get("sub_account_id"),
                apply_account_id=self.cfg.fosun.get("apply_account_id") or None,
                start=0, count=50, client_id=int(client_id),
            )
            payload = _payload(resp)
            rows, shape_ok = _extract_rows(payload)
            if shape_ok and rows:
                row = rows[0]
                oid = str(_first(row, _ORDER_ID_KEYS, "") or "")
                return OrderResult(
                    order_id=oid or f"UNCONFIRMED-{client_id}",
                    broker_order_id=oid,
                    symbol=str(_first(row, _SYMBOL_KEYS, symbol)),
                    market=str(_first(row, ("marketCode",), "")),
                    side="buy" if str(_first(row, ("direction",), "1")) == "1" else "sell",
                    qty=_fnum(_first(row, ("quantity", "qty")), qty),
                    price=_fnum(_first(row, ("price",))),
                    status=str(_first(row, ("status", "orderStatus"), "") or "unknown").lower(),
                    filled_qty=_fnum(_first(row, ("filledQuantity", "dealtQty"))),
                    avg_fill_price=_fnum(_first(row, ("avgPrice", "dealtPrice"))) or None,
                    venue=self.venue,
                    ts=str(_first(row, ("createTime", "orderTime"), now_iso())),
                    client_id=client_id,
                    reconcile_note="按 clientId 匹配到券商订单",
                    raw=row,
                )
        except BrokerError:
            raise
        except BaseException as exc:  # noqa: BLE001
            raise BrokerError(
                f"按 clientId 对账失败（无法判断是否受理，必须保持 unknown）: "
                f"{type(exc).__name__}: {exc}",
                code="RECONCILE_FAILED", retryable=True) from exc

        # 2) 备用：拉最近订单，按 clientId 字段或 标的+方向+时间 近似匹配
        try:
            recent = self.list_orders(limit=50)
        except BrokerError:
            raise
        for od in recent:
            if od.client_id and str(od.client_id) == str(client_id):
                od.reconcile_note = "在最近订单列表中按 clientId 字段匹配"
                return od
        for od in recent:
            if symbol and od.symbol.lower() == symbol.lower() and od.side == side:
                if qty and abs(float(od.qty) - float(qty)) > 1e-9:
                    continue
                od.reconcile_note = ("按 标的+方向+数量 近似匹配（未能拿到 clientId 字段），"
                                     "需人工复核")
                return od
        return None

    # ------------------------------------------------------------------ #
    def health(self) -> Dict[str, Any]:
        # strict=False：健康检查绝不能因为"凭证不完整"而抛异常（面板要能显示原因）
        creds = self._resolve_credentials(update_origin=False, strict=False)
        ok, reason = self.cfg.gate.can_connect_broker()
        return {
            "venue": self.venue,
            "gate_allows_connect": ok,
            "gate_reason": reason,
            "configured": self.configured,
            "connected": self.connected,
            "blocked_reason": self.blocked_reason,
            "last_error": self.last_error,
            "base_url_set": bool(creds["base_url"]),
            "api_key_present": bool(creds["api_key"]),
            "server_public_key": redact_pem(creds["server_public_key"]),
            "client_private_key": redact_pem(creds["client_private_key"]),
            # P0-1 可审计：只报来源与变量名，不含任何值
            "credential_origin": dict(self._cred_origin),
            # B-1 可审计：三要素是否同源、是否完整、决策当时的来源与脱敏指纹
            "credential_sources": dict(self._cred_sources),
            "credential_mixed": bool(self._cred_validation.get("mixed", False)),
            "credential_validation": {
                "status": self._cred_validation.get("status", "not_evaluated"),
                "detail": self._cred_validation.get("detail", ""),
                "complete": bool(self._cred_validation.get("complete", False)),
                "mixed": bool(self._cred_validation.get("mixed", False)),
                # B1-02：域内混用与整体信任域（本地配置 / 保险箱 / 跨域）
                "mixed_within_domain": bool(
                    self._cred_validation.get("mixed_within_domain", False)),
                "domain": str(self._cred_validation.get("domain", "none")),
                "identity_consistent": bool(
                    self._cred_validation.get("identity_consistent", False)),
                "fingerprints": dict(self._cred_validation.get("fingerprints", {})),
            },
            "sdk_env_injected": list(self._sdk_env_injected),
            # A-1：SDK 自带推送入口是否已被禁用（True = 无旁路）
            "market_push_guard": (
                isinstance(getattr(self.client, "market_push", None), _MarketPushDisabled)
                or bool(_MPA_GUARD.status().get("installed"))),
            # A-1 明细：类级拦截装在哪个类、几个 owner、是否被第三方改回
            "market_push_guard_detail": _MPA_GUARD.status(),
            # 保险箱状态只报"可读/不存在/损坏/无权限"，不含任何条目内容
            "vault": (self.vault.diagnostics()
                      if hasattr(self.vault, "diagnostics")
                      else {"state": "absent", "kind": "", "exists": False,
                            "note": "未配置保险箱（凭证只能来自环境变量）"}),
            "credentials_released": not bool(self._sdk_env_owned),
            "sdk_type": str(self.cfg.fosun.get("sdk_type") or ""),
            # 生产路径核实：个人接口必须配 /api；/api/ops 仅适用于 OPS 配对入口
            "api_prefix": self._api_prefix(),
            "routing_issue": self.routing_issue(),
            "sub_account_id_set": bool(self.cfg.fosun.get("sub_account_id")),
            "apply_account_id_set": bool(self.cfg.fosun.get("apply_account_id")),
            "can_send_real_order": self.cfg.gate.can_send_real_order()[0],
            # 网络与行情兜底状态
            "http_timeout": {"connect": self._timeout_tuple()[0],
                             "read": self._timeout_tuple()[1]},
            "reconnects": self._conn.connects,
            "read_retries": self._conn.retries,
            "market_encryption_fallback": self._quote_via_min,
            # US-P0-3：持仓查询是否覆盖盘前/盘后/夜盘
            "position_sessions": {
                **self._position_session_flags(),
                "last_query": dict(self._last_position_flags),
                "warning": ("" if all(self._position_session_flags().values())
                            else "有盘前/盘后/夜盘持仓未被查询 —— 那些持仓将脱离风控"),
            },
            "market_fallback_reason": self._market_fallback_reason,
            # 明确标注：未修改上游 SDK
            "sdk_modified": False,
        }
