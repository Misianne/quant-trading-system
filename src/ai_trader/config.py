"""配置加载与安全门禁。

设计原则
--------
1. 一切危险开关默认取"关闭"值，配置文件中漏写 = 关闭。
2. 只读环境变量覆盖，绝不写回环境变量、绝不修改系统全局环境。
3. :class:`SafetyGate` 是所有真实交易路径的唯一入口，
   任何模块想触碰真实账户都必须先过它。
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .safety.halt import (
    SOURCE_CONFIG,
    HaltStore,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class SafetyState:
    """三重开关的运行时快照。"""

    live_trading: bool = False
    order_execution_enabled: bool = False
    paper_trading: bool = True
    kill_switch: bool = False
    live_ack_phrase: str = ""
    required_live_ack: str = "I UNDERSTAND REAL MONEY IS AT RISK"
    #: 急停来源：sentinel / config / storage_error / none
    halt_source: str = "none"
    halt_reason: str = ""
    halt_at: str = ""
    #: 急停哨兵读写失败（sticky）。为真时任何交易都必须被拒绝。
    #: US-P1 N-3 起保持为"读或写任一失败"的聚合标志，向后兼容。
    halt_store_failed: bool = False
    #: US-P1 N-3：读写语义拆分
    halt_read_failed: bool = False      # 不知道是否被人为停止 → 一切阻断
    halt_write_failed: bool = False     # 知道自己没停但停不下来 → 默认也阻断
    halt_error_kind: str = ""           # corrupt / permission / no_space / atomic_failed / ...
    halt_error_phase: str = ""          # read / write

    # ---------------- 派生判定 ----------------

    @property
    def live_ack_satisfied(self) -> bool:
        return bool(self.live_ack_phrase) and self.live_ack_phrase == self.required_live_ack

    @property
    def live_authorized(self) -> bool:
        """是否具备发起真实券商请求的资格（仅代表"允许建连"，不代表可以下单）。"""
        return (
            self.live_trading
            and not self.paper_trading
            and not self.kill_switch
            and not self.halt_store_failed
            and self.live_ack_satisfied
        )

    @property
    def real_order_authorized(self) -> bool:
        """是否具备真实下单/撤单资格 —— 最严格的一档。"""
        return self.live_authorized and self.order_execution_enabled

    def mark_halt_storage_failure(self, kind: str = "", phase: str = "") -> None:
        """把哨兵故障写到门禁状态上（读写分别打标，并聚合到 halt_store_failed）。"""
        self.halt_store_failed = True
        self.halt_error_kind = kind or self.halt_error_kind
        self.halt_error_phase = phase or self.halt_error_phase
        if phase == "read":
            self.halt_read_failed = True
        elif phase == "write":
            self.halt_write_failed = True
        else:
            # 未区分阶段 → 按最严的一方处理
            self.halt_read_failed = True
            self.halt_write_failed = True

    def clear_halt_storage_failure(self) -> None:
        self.halt_store_failed = False
        self.halt_read_failed = False
        self.halt_write_failed = False
        self.halt_error_kind = ""
        self.halt_error_phase = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "live_trading": self.live_trading,
            "order_execution_enabled": self.order_execution_enabled,
            "paper_trading": self.paper_trading,
            "kill_switch": self.kill_switch,
            "halt_source": self.halt_source,
            "halt_reason": self.halt_reason,
            "halt_at": self.halt_at,
            "halt_store_failed": self.halt_store_failed,
            "halt_read_failed": self.halt_read_failed,
            "halt_write_failed": self.halt_write_failed,
            "halt_error_kind": self.halt_error_kind,
            "halt_error_phase": self.halt_error_phase,
            "live_ack_satisfied": self.live_ack_satisfied,
            "live_authorized": self.live_authorized,
            "real_order_authorized": self.real_order_authorized,
        }


@dataclass
class SafetyGate:
    """把所有危险动作收敛到一处判定，并留下拒绝理由。

    风控模块（risk.controller）依赖本对象判断"能不能下手"；
    券商适配器（broker.fosun_adapter）依赖本对象判断"能不能建连/发送指令"。
    """

    state: SafetyState = field(default_factory=SafetyState)

    def can_connect_broker(self) -> tuple[bool, str]:
        if self.state.halt_store_failed:
            return False, "急停哨兵读写异常，拒绝一切券商操作（fail-closed）"
        if self.state.kill_switch:
            return False, "KILL_SWITCH 已触发，禁止一切券商连接"
        if self.state.paper_trading:
            return False, "PAPER_TRADING=true，真实券商连接被禁用"
        if not self.state.live_trading:
            return False, "LIVE_TRADING=false，真实券商连接被禁用"
        if not self.state.live_ack_satisfied:
            return False, "缺少 LIVE_ACK_PHRASE 显式确认，拒绝真实券商连接"
        return True, "ok"

    def can_send_real_order(self) -> tuple[bool, str]:
        ok, reason = self.can_connect_broker()
        if not ok:
            return ok, reason
        if not self.state.order_execution_enabled:
            return False, "ORDER_EXECUTION_ENABLED=false，禁止真实下单/撤单"
        return True, "ok"

    def can_cancel_real_order(self) -> tuple[bool, str]:
        # 撤单在风控上属于"降风险"动作，但仍要求真实下单权限，
        # 以免在无授权状态下触碰真实账户。
        return self.can_send_real_order()

    def snapshot(self) -> Dict[str, Any]:
        return self.state.describe()


@dataclass
class AppConfig:
    raw: Dict[str, Any]
    path: Path
    gate: SafetyGate
    dotenv_loaded: bool = False
    halt_store: Optional[HaltStore] = None

    # ---- 便捷取值 ----
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def mode(self) -> str:
        return str(self.get("system.mode", "paper"))

    @property
    def risk(self) -> Dict[str, Any]:
        return self.get("risk", {}) or {}

    @property
    def ai(self) -> Dict[str, Any]:
        return self.get("ai", {}) or {}

    @property
    def fosun(self) -> Dict[str, Any]:
        return self.get("fosun", {}) or {}

    @property
    def paper(self) -> Dict[str, Any]:
        return self.get("paper", {}) or {}

    @property
    def universe(self) -> List[Dict[str, Any]]:
        items = self.get("universe", []) or []
        return [dict(x) for x in items if isinstance(x, dict) and x.get("code")]

    @property
    def data_dir(self) -> Path:
        p = PROJECT_ROOT / str(self.get("system.data_dir", "data"))
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def log_dir(self) -> Path:
        p = PROJECT_ROOT / str(self.get("system.log_dir", "logs"))
        p.mkdir(parents=True, exist_ok=True)
        return p

    def typed_risk(self, key: str, default: float) -> float:
        return _as_float(self.risk.get(key), default)

    def typed_risk_int(self, key: str, default: int) -> int:
        return _as_int(self.risk.get(key), default)


#: 由本进程 ``load_dotenv`` 写入 os.environ 的键名（不含值）。
#: Batch 4.1.1 / SR-B1-04：用于把"运维显式 export"与"应用自己读 .env"
#: 区分开——二者在 os.environ 里长得完全一样，只靠值无法判断来源。
_DOTENV_KEYS: set = set()

#: 键名 → 该次写入值的 SHA-256 指纹（**不保存明文**）。
#: 只有"当前环境值仍等于当时从 .env 读到的值"才算 dotenv 来源；
#: 之后被 ``export``/``setenv`` 改写成别的值，就必须按显式环境变量处理。
#: 仅凭键名判断会把"后来显式覆盖"误判成 dotenv（B-1 来源模型的前置条件）。
_DOTENV_FP: Dict[str, str] = {}


class _DotenvValue(str):
    """``load_dotenv`` 写进 ``os.environ`` 的值对象 —— **加载时来源标记**。

    为什么需要（Batch 4.1.2 / AUD2-B1-01）
    --------------------------------------
    值指纹只能证明"值相同"，不能证明"来源"：运维把与 ``.env`` 完全相同的
    值显式 export 之后，指纹判定仍会说它来自 dotenv，于是 Vault 越级覆盖了
    显式环境变量。

    这里让 ``load_dotenv`` 写入的值带上一个 ``str`` 子类标记。之后：
    * 没有任何人改写 → ``os.environ`` 内部仍是这个对象 → 判 dotenv；
    * 被运维/第三方用普通 ``str`` 重新赋值（哪怕内容一模一样）→ 标记消失
      → 判 ``explicit_env``。

    标记只改变类型，不改变内容：``repr``/``len``/``==``/``encode`` 与普通
    ``str`` 完全一致，也不会把任何额外信息带进日志。
    """

    __slots__ = ()


#: 键名 → 当时写入的标记对象（强引用，保证对象不被回收）
_DOTENV_MARKED: Dict[str, "_DotenvValue"] = {}

#: ``os.environ`` 内部结构的哨兵：区分"没有这个键"和"读不到内部结构"
_MISSING = object()

# ---------------------------------------------------------------------- #
# 加载时来源元数据（Batch 4.1.2 / AUD2-B1-01）
# ---------------------------------------------------------------------- #
#: 载入之后被**外部**重写过的键（哪怕写回的值与 `.env` 完全相同）。
#:
#: 为什么必须有它：值指纹只能证明"值相同"，永远无法证明"没人重写过"。
#: 运维 ``export`` 一个与 `.env` 一字不差的值时，指纹判定会说"还是 dotenv"，
#: 于是 Vault 越级覆盖了运维显式指定的值（AUD2-B1-01 的根因）。
#: 这里记录"载入之后是否发生过外部赋值"，把"值"升级为"值的来历"。
_DOTENV_TOUCHED: set = set()

#: 受控写入的嵌套深度：本模块与凭据注册表自己写 ``os.environ`` 时 >0，
#: 这类写入**不算**外部改写（否则自己注入一次就把 dotenv 身份抹掉了）。
_INTERNAL_WRITE_DEPTH = 0

#: ``os._Environ.__setitem__`` 写入追踪器的安装状态（诊断用，不含任何值）
_ENVIRON_TRACKER: Dict[str, Any] = {
    "installed": False,
    "reason": "",
    "original": None,
}


def _norm_key(name: Any) -> str:
    """把键名归一化到 ``os.environ`` 实际使用的形态。

    Windows 的环境变量名不区分大小写（``os.environ`` 内部统一大写），
    POSIX 区分。归一化保证"写入日志"与"来源查询"用同一把尺子。
    """
    text = name if isinstance(name, str) else str(name)
    return text.upper() if os.name == "nt" else text


class _InternalEnvWrite:
    """``with internal_env_write():`` —— 声明"这段写入是我们自己人干的"。

    只有**受控写入**（``load_dotenv`` 载入、凭据注册表注入/恢复）才使用它；
    其余任何 ``os.environ[...] = ...`` 都会被记为外部改写。
    """

    __slots__ = ()

    def __enter__(self) -> "_InternalEnvWrite":
        global _INTERNAL_WRITE_DEPTH
        _INTERNAL_WRITE_DEPTH += 1
        return self

    def __exit__(self, *exc: Any) -> bool:
        global _INTERNAL_WRITE_DEPTH
        _INTERNAL_WRITE_DEPTH = max(0, _INTERNAL_WRITE_DEPTH - 1)
        return False


_INTERNAL_ENV_WRITE = _InternalEnvWrite()


def internal_env_write() -> "_InternalEnvWrite":
    """返回受控写入标记（可重入）。供凭据注册表包裹自己的 ``os.environ`` 写。"""
    return _INTERNAL_ENV_WRITE


def _note_env_write(key: Any) -> None:
    """记录一次**外部**环境写入（由 ``_Environ.__setitem__`` 包装器调用）。"""
    if _INTERNAL_WRITE_DEPTH > 0:
        return
    try:
        name = _norm_key(key)
        if name in _DOTENV_KEYS:
            _DOTENV_TOUCHED.add(name)
    except Exception:  # noqa: BLE001 - 记账失败绝不能影响环境写入本身
        return


def _install_environ_tracker() -> bool:
    """给 ``type(os.environ).__setitem__`` 套一层**记账**包装（幂等）。

    为什么需要动 stdlib 的类
    ------------------------
    同值改写没有任何可观测的"值差异"，唯一可靠的观测点是**写入动作**本身。
    ``os.environ`` 的写入在 CPython 里只有一个入口（``_Environ.__setitem__``），
    包装它既能保持行为完全不变（先调原实现，再记账），又能跨平台生效
    ——不受"POSIX 内部存 bytes、看不到对象标记"的限制。

    安全性：包装器只做记账，异常全部吞掉，绝不改变返回值/抛错时机；
    重复安装会被识别（``_aitrader_env_tracker`` 标记），不会叠加。
    """
    if _ENVIRON_TRACKER.get("installed"):
        return True
    cls = type(os.environ)
    original = getattr(cls, "__setitem__", None)
    if original is None:  # pragma: no cover - 极端自定义 environ
        _ENVIRON_TRACKER["reason"] = "no __setitem__"
        return False
    if getattr(original, "_aitrader_env_tracker", False):
        _ENVIRON_TRACKER.update(installed=True, reason="",
                                original=getattr(original, "_aitrader_original",
                                                 original))
        return True

    def __setitem__(self, key, value):  # type: ignore[no-untyped-def]
        original(self, key, value)
        if self is os.environ:
            _note_env_write(key)

    __setitem__._aitrader_env_tracker = True          # type: ignore[attr-defined]
    __setitem__._aitrader_original = original         # type: ignore[attr-defined]
    try:
        cls.__setitem__ = __setitem__                 # type: ignore[method-assign]
    except Exception as exc:  # noqa: BLE001 - 装不上就退回对象标记判据
        _ENVIRON_TRACKER["reason"] = type(exc).__name__
        return False
    _ENVIRON_TRACKER.update(installed=True, reason="", original=original)
    return True


def _environ_storage_kind() -> str:
    """``os.environ`` 内部存的是 ``str``（Windows）还是 ``bytes``（POSIX）。"""
    data = getattr(os.environ, "_data", None)
    if not isinstance(data, dict):
        return "unknown"
    for key in data:
        return "bytes" if isinstance(key, bytes) else "str"
    return "unknown"


def dotenv_source_diagnostics() -> Dict[str, Any]:
    """来源元数据的**可审计状态**（只含计数与开关，不含任何键名/值）。"""
    return {
        "write_tracker_installed": bool(_ENVIRON_TRACKER.get("installed")),
        "write_tracker_error": str(_ENVIRON_TRACKER.get("reason") or ""),
        "environ_storage": _environ_storage_kind(),
        "marked_keys": len(_DOTENV_MARKED),
        "touched_keys": len(_DOTENV_TOUCHED),
        "internal_write_depth": _INTERNAL_WRITE_DEPTH,
    }


def _dotenv_fingerprint(value: str) -> str:
    import hashlib

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _raw_environ_value(name: str) -> Any:
    """取 ``os.environ`` **内部存储的原始对象**（不经过 decode）。

    CPython 的 ``os.environ`` 读取时会把值 ``str()`` 成普通 str，标记会因此
    丢失；只有内部 ``_data`` 保留原对象。读不到内部结构时返回 ``_MISSING``，
    调用方据此退回保守判定。
    """
    data = getattr(os.environ, "_data", None)
    if not isinstance(data, dict):
        return _MISSING
    try:
        if name in data:
            return data[name]
        encoded = os.fsencode(name)
        if encoded in data:
            return data[encoded]
    except Exception:  # noqa: BLE001 - 极端的自定义 environ
        return _MISSING
    return _MISSING


def dotenv_keys() -> frozenset:
    """返回由 ``load_dotenv`` 写入进程环境的键名（只读，不含值）。"""
    return frozenset(_DOTENV_KEYS)


def dotenv_origin(name: str, value: Optional[str]) -> bool:
    """``value`` 是否**确实**来自本进程从 ``.env`` 的载入。

    四个条件缺一不可（Batch 4.1.2 / AUD2-B1-01）：

    1. 键名由本进程 ``load_dotenv`` 写入；
    2. 载入之后**没有被外部重写过**（写入日志里没有该键）；
    3. 当前值的指纹与载入时一致（被改成别的值 → 不再算 dotenv）；
    4. 没有写入日志可用时，``os.environ`` 内部必须仍是载入时写入的**同一个
       标记对象**——被任何普通 str 重新赋值（即使内容完全相同）→ 判
       ``explicit_env``。

    第 2 条是本次修复的核心：值指纹只能证明"值相同"，证明不了"没人写过"。
    第 4 条是第 2 条不可用时的兜底（Windows 能看见对象身份；POSIX 下
    ``os.environ`` 内部按字节存储，看不到身份，此时直接 fail-closed）。

    任何一项无法确认时一律按**显式环境变量**处理（保守方向：不让 Vault
    覆盖进程环境里已经存在的值）。**绝不会**仅凭"值相同"就断言来自 ``.env``。
    """
    if not value:
        return False
    key = _norm_key(name)
    if key not in _DOTENV_KEYS:
        return False
    if key in _DOTENV_TOUCHED:
        # 载入之后被外部以普通赋值重写过（**哪怕值完全相同**）→ 显式环境变量
        return False
    fp = _DOTENV_FP.get(key)
    if not fp:
        # 兼容：早期记录只有键名，没有指纹 → 不能据此断言来源
        return False
    import hmac

    if not hmac.compare_digest(fp, _dotenv_fingerprint(value)):
        return False
    marked = _DOTENV_MARKED.get(key)
    if marked is None:
        return False
    raw = _raw_environ_value(key)
    if raw is _MISSING:
        # 读不到内部存储（非 CPython / environ 被整体替换）→ 无法确认，保守处理
        return False
    if _ENVIRON_TRACKER.get("installed"):
        # 写入日志已经证明"载入之后没有任何**外部**写入"。受控写入（凭据注入 /
        # 释放恢复）会换掉 os.environ 里的对象，但来源没有变，所以这里不再要求
        # 对象身份相同——否则我们自己注入一次就会把 dotenv 身份抹掉（T19b）。
        return True
    # 没有写入日志（包装失败）→ 退回对象身份判据；POSIX 内部存字节，看不到
    # 身份，同样按显式环境变量处理（保守方向）。
    if isinstance(raw, bytes):
        return False
    return raw is marked


def _reset_dotenv_keys() -> None:
    """仅供测试夹具：清空来源记录（不动 os.environ）。"""
    _DOTENV_KEYS.clear()
    _DOTENV_FP.clear()
    _DOTENV_MARKED.clear()
    _DOTENV_TOUCHED.clear()


def load_dotenv(path: Optional[Path] = None) -> bool:
    """轻量 .env 加载：只 setdefault，不覆盖已有进程环境，不写系统环境。

    载入时同时登记**来源元数据**（键名 / 值指纹 / 标记对象），并开启写入
    追踪：之后任何人以普通赋值改写这些键（即使写回一模一样的值），该键
    就不再算 dotenv，而按 ``explicit_env`` 处理（Batch 4.1.2 / AUD2-B1-01）。
    """
    env_path = path or (PROJECT_ROOT / ".env")
    if not env_path.exists():
        return False
    _install_environ_tracker()
    loaded = False
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if key not in os.environ:
            marked = _DotenvValue(value)
            with internal_env_write():
                os.environ[key] = marked
            name = _norm_key(key)
            _DOTENV_KEYS.add(name)
            _DOTENV_FP[name] = _dotenv_fingerprint(value)
            _DOTENV_MARKED[name] = marked
            _DOTENV_TOUCHED.discard(name)
            loaded = True
    return loaded


def _apply_env_overrides(raw: Dict[str, Any]) -> Dict[str, Any]:
    """环境变量优先于配置文件中的安全开关与关键路径。"""
    out = copy.deepcopy(raw)
    out.setdefault("safety", {})
    s = out["safety"]

    if os.getenv("LIVE_TRADING") is not None:
        s["live_trading"] = _as_bool(os.getenv("LIVE_TRADING"), False)
    if os.getenv("ORDER_EXECUTION_ENABLED") is not None:
        s["order_execution_enabled"] = _as_bool(os.getenv("ORDER_EXECUTION_ENABLED"), False)
    if os.getenv("PAPER_TRADING") is not None:
        s["paper_trading"] = _as_bool(os.getenv("PAPER_TRADING"), True)
    if os.getenv("KILL_SWITCH") is not None:
        s["kill_switch"] = _as_bool(os.getenv("KILL_SWITCH"), False)
    if os.getenv("LIVE_ACK_PHRASE"):
        s["live_ack_phrase"] = os.getenv("LIVE_ACK_PHRASE", "")

    out.setdefault("fosun", {})
    if os.getenv("FSOPENAPI_BASE_URL"):
        out["fosun"]["base_url"] = os.getenv("FSOPENAPI_BASE_URL", "")
    if os.getenv("FOSUN_SUB_ACCOUNT_ID"):
        out["fosun"]["sub_account_id"] = os.getenv("FOSUN_SUB_ACCOUNT_ID", "")
    if os.getenv("FOSUN_APPLY_ACCOUNT_ID"):
        out["fosun"]["apply_account_id"] = os.getenv("FOSUN_APPLY_ACCOUNT_ID", "")

    out.setdefault("web", {})
    if os.getenv("WEB_HOST"):
        out["web"]["host"] = os.getenv("WEB_HOST")
    if os.getenv("WEB_PORT"):
        out["web"]["port"] = _as_int(os.getenv("WEB_PORT"), 8770)
    return out


def build_gate(raw: Dict[str, Any], data_dir: Optional[Path] = None) -> SafetyGate:
    """构建安全门禁。

    ``data_dir`` 非空时会读取急停哨兵（HALT.json）：

    * 哨兵存在 → 强制 ``kill_switch=True``，与配置里的 false 无关
    * 哨兵损坏/不可读 → ``halt_store_failed=True`` + ``kill_switch=True``（fail-closed）

    这样"重启后默认保持停止"是**结构性的**，不依赖任何调用方记得去查。
    """
    s = raw.get("safety", {}) or {}
    state = SafetyState(
        live_trading=_as_bool(s.get("live_trading"), False),
        order_execution_enabled=_as_bool(s.get("order_execution_enabled"), False),
        paper_trading=_as_bool(s.get("paper_trading"), True),
        kill_switch=_as_bool(s.get("kill_switch"), False),
        live_ack_phrase=str(s.get("live_ack_phrase") or ""),
        required_live_ack=str(s.get("required_live_ack") or "I UNDERSTAND REAL MONEY IS AT RISK"),
    )
    if state.kill_switch:
        state.halt_source = SOURCE_CONFIG
        state.halt_reason = "配置或环境变量要求停止（KILL_SWITCH=true）"

    # 急停哨兵优先于配置：哨兵在，就一定是停止状态
    if data_dir is not None:
        store = HaltStore(data_dir)
        hs = store.safe_read()
        if hs.halted:
            state.kill_switch = True
            state.halt_source = hs.source
            state.halt_reason = hs.reason or "急停哨兵存在"
            state.halt_at = hs.at
        if store.failed:
            # US-P1 N-3：把读失败的具体类型带上门禁，便于风控分级与排查
            state.mark_halt_storage_failure(kind=store.last_kind,
                                            phase=store.last_phase or "read")
            state.kill_switch = True

    # 互斥纠正：paper_trading 与 live_trading 不得同时为真。
    # 冲突时以"更安全"的一方为准 —— 强制回到模拟盘。
    if state.paper_trading and state.live_trading:
        state.live_trading = False
        state.order_execution_enabled = False
    return SafetyGate(state=state)


def load_config(path: Optional[Path] = None, use_dotenv: bool = True) -> AppConfig:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    dotenv_loaded = load_dotenv() if use_dotenv else False
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    else:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件格式非法（应为 YAML 映射）: {cfg_path}")
    raw = _apply_env_overrides(raw)
    data_dir = PROJECT_ROOT / str(raw.get("system", {}).get("data_dir", "data"))
    gate = build_gate(raw, data_dir=data_dir)
    cfg = AppConfig(raw=raw, path=cfg_path, gate=gate, dotenv_loaded=dotenv_loaded)
    cfg.halt_store = HaltStore(cfg.data_dir)
    return cfg


def config_as_dict(cfg: AppConfig) -> Dict[str, Any]:
    """脱敏后的配置快照（用于 Web 展示/审计），不含任何密钥明文。"""
    safe = copy.deepcopy(cfg.raw)
    safe.pop("safety", None)
    safe.setdefault("_security", {})
    safe["_security"] = cfg.gate.snapshot()
    return asdict(cfg) if False else safe  # 保留结构，避免误序列化 Path
