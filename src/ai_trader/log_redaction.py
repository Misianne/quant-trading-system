"""统一日志脱敏（US-P1 Batch 4.1 / Batch 4.1.1）。

为什么边界要多层
----------------
Python 的 ``Logger.handle()`` 只执行**该条记录创建者自己**的 filters；
记录向父 logger 传播时**不再**执行父 logger 的 filters。真正的输出边界是
``Handler.handle()`` —— 但它是一个**实例方法**，任何子类重写都会遮蔽它。
因此 Batch 4.1.1 把保护做成五层，每层解决一类绕过：

===== =============================== ==========================================
层级 落点                            解决什么
===== =============================== ==========================================
L0   ``register_secret``              运行时才知道的字面量（Session/API Key）
L1   ``_sanitize_record``（filter）    msg/args/extra/exc_text/stack_info
L2   ``logging.Logger.handle`` 钩子    自定义 Handler 重写 handle、绕过 filter
L3   ``exc_info`` 中和 +              Formatter 重新渲染异常、最终文本兜底
     ``logging.Handler.format`` 钩子
L4   安装失败 → 包装 / 禁用           filters 不可变等"装不上"的情况
===== =============================== ==========================================

边界与限制（必须如实理解）
--------------------------
* 只保护**经过 Python logging 且经标准分发链**的记录。以下路径**不在**范围内，
  不得宣称"日志绝对安全"：``print``、直接写文件（如 ``FaultRecorder``）、
  ``sys.stderr.write``、``warnings``（未 ``captureWarnings``）、子进程输出、
  SDK 示例脚本自己的 handler。
* 直接调用 ``handler.handle(record)`` 且该 handler 又重写了 ``handle`` 与
  ``format`` 时，无法在不替换实例的前提下拦截（见测试
  ``test_direct_handler_handle_call_is_documented_gap``）。
* 不做加密或还原，只把命中内容替换成 ``***``。
* 递归深度、容器项数、节点总数、字符串长度都有上限；超限部分被截断，
  **不会**为了"完整"而把剩余内容原样输出。超过 2 MiB 的输入整条丢弃
  （替换为占位符），不做"先截断再脱敏"——那会把凭证切成半截留在输出里。
* 本模块从不调用 logging、从不记录密钥原文/指纹/脱敏前内容。凭据登记表
  是**只写**的：除脱敏替换外没有任何读取接口，``repr`` 也不含值。

fail-closed
-----------
过滤器自身出任何异常时，记录会被替换成固定占位符并清空 ``args``/
``exc_info``/extra；若连替换都失败，``filter()`` 返回 ``False`` 丢弃该记录。
``install_on_handler`` 装不上 filter 时依次降级为"包装实例 handle"与
"禁用该 handler + stderr 告警"，绝不静默放行。

末级"禁用"**不是恒可用**的（Batch 4.1.2 / AUD2-F2-01）：``setLevel`` 可能
抛异常、被重写成空操作、或 level 被改回。因此降级到这一级时必须**回读
校验**；校验不过的 handler 记入 ``_UNSECURED_HANDLERS``，``redaction_health()``
的 ``healthy`` 为 False，且**不会**计入 ``blocked_handlers``。
"""
from __future__ import annotations

import logging
import re
import sys
import threading
from collections import OrderedDict
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

__all__ = [
    "RedactionFilter",
    "redact_text",
    "redact_obj",
    "register_secret",
    "register_secrets",
    "install_on_handler",
    "install_on_handlers",
    "install_global_hook",
    "install_logger_hook",
    "install_format_guard",
    "ensure_hooks",
    "uninstall_hooks",
    "redaction_health",
    "redact_exception",
]

# --------------------------------------------------------------------------- #
# 限额：日志处理本身不能成为新的故障源
# --------------------------------------------------------------------------- #
#: 单条字符串脱敏后的最大长度（超出截断）。凭证通常 < 4KB，256KB 足够容纳
#: 一条完整的异常上下文，同时避免恶意/异常日志把 CPU 拖死。
MAX_TEXT_CHARS = 262_144
#: 输入硬上限：超过 2 MiB 的文本**不求值**，整条替换为占位符。
MAX_RAW_TEXT_CHARS = 2 * 1024 * 1024
#: 容器递归深度上限
MAX_DEPTH = 6
#: 单个容器最多处理的项数
MAX_ITEMS = 100
#: 单条记录最多处理的节点总数（跨容器累计）
MAX_NODES = 1000
#: 短于该长度的字面量不登记，避免把普通词误伤成 ***
MIN_SECRET_LEN = 8
#: 进程内最多登记多少条字面量（超出按插入顺序淘汰）
MAX_SECRETS = 64
#: 键标识符的最大长度（防止正则超长回溯）
MAX_IDENT_CHARS = 64
#: 单个值最多匹配多少字符（必须有上界，否则超长文本上会退化）
MAX_VALUE_CHARS = 4096

_MASK = "***"
_TRUNCATED = "...[truncated]"
_PLACEHOLDER = "***[log redaction failed]***"
_OVERSIZED = "***[oversized log dropped]***"
_CIRCULAR = "[circular]"

# --------------------------------------------------------------------------- #
# 敏感标识符表（文本与结构化 extra 共用）
# --------------------------------------------------------------------------- #
_SENSITIVE_KEYS = frozenset({
    "apikey", "xapikey", "xsession", "session", "sessionid", "sessionkey",
    "sessiontoken", "token", "authtoken", "bearertoken", "accesstoken",
    "refreshtoken", "idtoken", "secret", "clientsecret", "secretkey",
    "apisecret", "password", "passwd", "passphrase", "authorization",
    "proxyauthorization", "cookie", "setcookie", "privatekey",
    "clientprivatekey", "serverpublickey", "signingkey", "encryptionkey",
    "credential", "credentials", "signature", "accesskeyid",
    "secretaccesskey",
})

#: 以这些后缀结尾的标识符**不是**凭证（避免 ``token_count=5`` 被误伤）
_TOKEN_SUFFIX_DENY = (
    "count", "len", "length", "size", "ttl", "timeout", "expire", "expiry",
    "expires", "name", "type", "mode", "enabled", "bytes", "path", "url",
)

_NON_KEY_CHARS = re.compile(r"[^a-z0-9]")


def _normalize_key(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    return _NON_KEY_CHARS.sub("", name.lower())


def _is_sensitive_token(name: Any) -> bool:
    """标识符是否表示"值必须打码"。

    规则：
    1. 归一化（小写、去掉非字母数字）；
    2. 命中后缀白名单（``*_count`` / ``*_timeout`` ...）→ 不是凭证；
    3. 等于敏感词，或以长度 >= 5 的敏感词结尾（``my_session_id`` /
       ``auth_token`` 都能命中）。

    误伤边界：``obsession:`` 会因以 ``session`` 结尾被打码——日志里几乎不出现，
    且"多打码"比"漏打码"安全，接受。
    """
    norm = _normalize_key(name)
    if not norm or len(norm) > MAX_IDENT_CHARS:
        return False
    if any(norm.endswith(suffix) for suffix in _TOKEN_SUFFIX_DENY):
        return False
    if norm in _SENSITIVE_KEYS:
        return True
    return any(len(tok) >= 5 and norm.endswith(tok) for tok in _SENSITIVE_KEYS)


#: 兼容旧名（结构化 extra 的判定入口）
_is_sensitive_key = _is_sensitive_token

# --------------------------------------------------------------------------- #
# 规则表
# --------------------------------------------------------------------------- #
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.S)
#: 认证头：值可能含空格（Bearer xxx / Cookie 多段），整行剩余部分抹掉
_AUTH_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*[:=][^\r\n]*")
#: 裸 Bearer 令牌
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}")

_IDENT = r"[A-Za-z_][A-Za-z0-9_.\-]{0,63}"
_VALUE = r"(?:\"[^\"\r\n]{1,4096}\"|'[^'\r\n]{1,4096}'|[^\s,;>]{1,4096})"
#: 通用 key=value / key: value —— 用回调判定标识符，避免写死有限的键名
_KV_RE = re.compile(
    rf"(?P<ident>{_IDENT})(?P<sep>\s*[:=]\s*)(?P<value>{_VALUE})")
#: JSON / Python dict 字面量（含反斜杠转义、单双引号）：
#: ``{"api_key": "..."}`` / ``{\"api_key\": \"...\"}`` / ``{'api_key': '...'}``
#: 层数实测：1 层 = 1 个反斜杠，2 层 = 3 个，3 层 = 7 个，4 层 = 15 个；
#: 因此上界取 15，覆盖到 4 层 ``json.dumps`` 嵌套。
_JSON_RE = re.compile(
    r"(?i)(?P<pre>\\{0,15}\"|')(?P<ident>[A-Za-z0-9_.\-]{1,64})(?P=pre)"
    r"(?P<sep>\s*:\s*)(?P<q>\\{0,15}\"|')"
    # 值里**不允许**吃掉转义引号：否则 `\"a\": \"b\", \"c\"` 会被并成一个值，
    # 连带把后面的业务字段一起抹掉（转义层数越多越明显）。
    r"(?P<value>(?:[^\"'\\\r\n]|\\[^\"'\r\n]){0,4096})(?P=q)")
#: URL 查询参数：?session_id=...&x_api_key=...
_URL_RE = re.compile(
    r"(?i)(?P<pre>[?&])(?P<ident>[A-Za-z0-9_.\-]{1,64})(?P<sep>=)"
    r"(?P<value>[^&#\s]{1,4096})")


def _kv_sub(match: "re.Match[str]") -> str:
    ident = match.group("ident")
    if not _is_sensitive_token(ident):
        return match.group(0)
    # 保留旧实现的输出形态（统一写成 "key: ***"），避免破坏既有日志解析与断言
    return f"{ident}: {_MASK}"


def _json_sub(match: "re.Match[str]") -> str:
    ident = match.group("ident")
    if not _is_sensitive_token(ident):
        return match.group(0)
    q = match.group("q")
    return (f"{match.group('pre')}{ident}{match.group('pre')}"
            f"{match.group('sep')}{q}{_MASK}{q}")


def _url_sub(match: "re.Match[str]") -> str:
    ident = match.group("ident")
    if not _is_sensitive_token(ident):
        return match.group(0)
    return f"{match.group('pre')}{ident}{match.group('sep')}{_MASK}"


def _auth_sub(match: "re.Match[str]") -> str:
    return f"{match.group(1)}: {_MASK}"


#: 顺序有意义：先整块（PEM）、再整行（认证头）、再结构化（JSON）、
#: 再裸令牌（Bearer）、最后键值对与 URL。
_RULES: Tuple[Tuple["re.Pattern[str]", Any], ...] = (
    (_PEM_RE, _MASK),
    (_AUTH_RE, _auth_sub),
    (_BEARER_RE, "Bearer ***"),
    (_JSON_RE, _json_sub),
    (_KV_RE, _kv_sub),
    (_URL_RE, _url_sub),
)

# --------------------------------------------------------------------------- #
# 凭据登记表（只写；仅供脱敏替换使用，永不对外读出）
# --------------------------------------------------------------------------- #
_SECRETS: "OrderedDict[str, None]" = OrderedDict()
_SECRETS_LOCK = threading.Lock()


def register_secret(value: Any) -> bool:
    """登记一个需要从日志里抹掉的字面量，返回是否**新增**。

    * 只接受 ``str`` / ``bytes``；``None``、空值、短于 :data:`MIN_SECRET_LEN`
      的值直接忽略（避免把普通词误伤成 ``***``）。
    * 登记表上限 :data:`MAX_SECRETS`，超出按插入顺序淘汰，长跑不无界增长。
    * 本函数不记录、不返回、不打印任何内容；表中的值除了用于替换之外
      没有读取接口。
    """
    if value is None:
        return False
    if isinstance(value, (bytes, bytearray)):
        try:
            text = bytes(value).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return False
    elif isinstance(value, str):
        text = value
    else:
        return False
    text = text.strip()
    if len(text) < MIN_SECRET_LEN or text == _MASK:
        return False
    with _SECRETS_LOCK:
        is_new = text not in _SECRETS
        _SECRETS[text] = None
        _SECRETS.move_to_end(text)
        while len(_SECRETS) > MAX_SECRETS:
            _SECRETS.popitem(last=False)
    return is_new


def register_secrets(values: Iterable[Any]) -> int:
    """批量登记，返回实际新增条数（只返回数量，不返回值）。"""
    return sum(1 for value in values if register_secret(value))


def _secret_snapshot() -> List[str]:
    """内部使用：返回按长度倒序的字面量副本（长的先替换，避免被短值切碎）。"""
    with _SECRETS_LOCK:
        items = list(_SECRETS)
    items.sort(key=len, reverse=True)
    return items


# --------------------------------------------------------------------------- #
# 文本脱敏
# --------------------------------------------------------------------------- #
def redact_text(text: Any, truncate: bool = True) -> str:
    """把认证头、令牌形态与已登记字面量从任意文本里抹掉。

    * 幂等：对已脱敏文本再跑一次结果不变。
    * 绝不抛异常：内部异常时返回 ``***``（fail-closed）。
    * 超长文本先脱敏再截断 —— 先截断可能把凭证切成"半截"仍留在输出里。
    * ``truncate=False`` 供"最终格式化文本"兜底使用：不追加截断标记，
      但超过 :data:`MAX_RAW_TEXT_CHARS` 仍整条丢弃（fail-closed）。
    """
    if text is None:
        return ""
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return _MASK
    elif not isinstance(text, str):
        try:
            text = str(text)
        except Exception:  # noqa: BLE001
            return _MASK
    if not text:
        return text
    if len(text) > MAX_RAW_TEXT_CHARS:
        # 直接丢弃：不截断、不部分处理，杜绝"半截凭证"与超大文本开销
        return _OVERSIZED

    try:
        out = text
        for pattern, replacement in _RULES:
            out = pattern.sub(replacement, out)
        for secret in _secret_snapshot():
            if secret in out:
                out = out.replace(secret, _MASK)
    except Exception:  # noqa: BLE001 - 脱敏失败绝不能回退成原文
        return _MASK

    if truncate and len(out) > MAX_TEXT_CHARS:
        out = out[:MAX_TEXT_CHARS] + _TRUNCATED
    return out


def redact_exception(exc_info: Any) -> str:
    """把 ``exc_info`` 三元组格式化成**已脱敏**的文本；失败返回 ``***``。

    独立成函数，便于测试与复用（不修改 SDK 源码，也不依赖具体 Formatter）。
    """
    try:
        text = logging.Formatter().formatException(exc_info)
    except Exception:  # noqa: BLE001
        return _MASK
    return redact_text(text)


# --------------------------------------------------------------------------- #
# 结构化对象脱敏
# --------------------------------------------------------------------------- #
class _Budget:
    """单条记录的节点预算，防止超大结构把日志线程拖死。"""

    __slots__ = ("left",)

    def __init__(self, left: int = MAX_NODES) -> None:
        self.left = left

    def spend(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def redact_obj(obj: Any, _depth: int = 0, _budget: Optional[_Budget] = None,
               _seen: Optional[Set[int]] = None) -> Any:
    """递归脱敏 dict / list / tuple / set 等结构，保持容器类型。"""
    budget = _budget if _budget is not None else _Budget()
    seen = _seen if _seen is not None else set()

    if _depth > MAX_DEPTH:
        return _TRUNCATED
    if not budget.spend():
        return _TRUNCATED

    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, (bytes, bytearray)):
        return redact_text(obj)
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, BaseException):
        try:
            return redact_text("%s: %s" % (type(obj).__name__, obj))
        except Exception:  # noqa: BLE001
            return _MASK

    if isinstance(obj, dict):
        return _redact_mapping(obj, _depth, budget, seen)
    if isinstance(obj, (list, tuple)):
        return _redact_sequence(obj, _depth, budget, seen)
    if isinstance(obj, (set, frozenset)):
        items = [redact_obj(v, _depth + 1, budget, seen) for v in list(obj)[:MAX_ITEMS]]
        try:
            return type(obj)(items)
        except Exception:  # noqa: BLE001 - 元素不可哈希时退化为列表
            return items

    # 不透明对象：只保留脱敏后的文本，绝不把对象本身交给 formatter
    try:
        return redact_text(str(obj))
    except Exception:  # noqa: BLE001
        return _MASK


def _redact_mapping(obj: Dict[Any, Any], depth: int, budget: _Budget,
                    seen: Set[int]) -> Any:
    oid = id(obj)
    if oid in seen:
        return _CIRCULAR
    seen.add(oid)
    out: Dict[Any, Any] = {}
    try:
        for index, (key, value) in enumerate(obj.items()):
            if index >= MAX_ITEMS:
                out[_TRUNCATED] = None
                break
            if isinstance(key, (str, bytes)):
                new_key = redact_text(key)   # bytes 键会转成 str
            else:
                new_key = key
            if _is_sensitive_key(new_key):
                out[new_key] = _MASK
            else:
                out[new_key] = redact_obj(value, depth + 1, budget, seen)
    finally:
        seen.discard(oid)
    return out


def _redact_sequence(obj: Any, depth: int, budget: _Budget,
                     seen: Set[int]) -> Any:
    oid = id(obj)
    if oid in seen:
        return [_CIRCULAR]
    seen.add(oid)
    items: List[Any] = []
    try:
        for index, value in enumerate(obj):
            if index >= MAX_ITEMS:
                items.append(_TRUNCATED)
                break
            items.append(redact_obj(value, depth + 1, budget, seen))
    finally:
        seen.discard(oid)
    return tuple(items) if isinstance(obj, tuple) else items


# --------------------------------------------------------------------------- #
# logging 过滤器
# --------------------------------------------------------------------------- #
#: LogRecord 的**真正**内置属性：这些由 logging 自己维护，不能当作 extra 处理。
#: 注意：``message`` **不在**这里——它是 Formatter 计算出来的缓存字段，
#: 可以被任何 handler 预先写入，必须参与脱敏（Batch 4.1.1 / SR-F2-07）。
_STANDARD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName",
})

#: 标记：该记录已经过脱敏，最终文本兜底可跳过
_REDACTED_FLAG = "_aitrader_redacted"


def _sanitize_record(record: logging.LogRecord) -> None:
    """就地脱敏一条记录（可能抛异常，由调用方 fail-closed）。"""
    # 1) 消息 + 参数：getMessage() 会把 args 合并进来（websockets 的对象参数
    #    只有在这一步才会暴露 __str__ 内容）。
    if record.args:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - 格式化失败时不能保留原始 msg/args
            record.msg = _PLACEHOLDER
            record.args = None
        else:
            record.msg = redact_text(text)
            record.args = None
    elif isinstance(record.msg, str):
        record.msg = redact_text(record.msg)
    else:
        record.msg = redact_obj(record.msg)

    # 2) 异常：先取文本、脱敏、写回 exc_text；随后**切断 exc_info**，
    #    防止任何 Formatter 用 formatException(exc_info) 重新渲染出原文。
    if record.exc_info:
        record.exc_text = redact_exception(record.exc_info)
        try:
            record.safe_exception = record.exc_text
        except Exception:  # noqa: BLE001 - 极端自定义 LogRecord
            pass
        record.exc_info = None
    elif record.exc_text:
        record.exc_text = redact_text(record.exc_text)

    # 3) 调用栈文本
    if record.stack_info:
        record.stack_info = redact_text(record.stack_info)

    # 4) 结构化 extra（含 formatter 缓存的 message）
    for key in list(record.__dict__):
        if key in _STANDARD_ATTRS:
            continue
        value = record.__dict__[key]
        if key == _REDACTED_FLAG:
            continue
        if _is_sensitive_key(key):
            record.__dict__[key] = _MASK
        else:
            record.__dict__[key] = redact_obj(value)

    try:
        record.__dict__[_REDACTED_FLAG] = True
    except Exception:  # noqa: BLE001
        pass


def _fail_closed(record: logging.LogRecord) -> bool:
    """把记录替换成占位符；成功返回 True，连替换都失败返回 False（丢弃记录）。"""
    try:
        record.msg = _PLACEHOLDER
        record.args = None
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        for key in list(record.__dict__):
            if key in _STANDARD_ATTRS:
                continue
            record.__dict__[key] = _MASK
        record.__dict__["message"] = _PLACEHOLDER
        return True
    except BaseException:  # noqa: BLE001 - 极端的自定义 LogRecord
        return False


class RedactionFilter(logging.Filter):
    """在记录进入 handler 之前完成脱敏的 logging.Filter。

    * ``filter()`` **永不把异常抛给 logging**（否则一次脱敏失败会打断业务
      日志调用）；内部异常走 :func:`_fail_closed`。
    * 对同一条记录重复执行是幂等的（已打码的内容不会再被改写）。
    * 不读取、不输出凭据登记表中的值。
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            _sanitize_record(record)
        except BaseException:  # noqa: BLE001 - 包括 RecursionError 等
            if not _fail_closed(record):
                return False
        return True

    def __repr__(self) -> str:  # pragma: no cover - 仅调试可见
        return "<RedactionFilter>"


# --------------------------------------------------------------------------- #
# 安装：现有 handler + 未来 handler + 分发边界 + 最终文本
# --------------------------------------------------------------------------- #
_INSTALL_LOCK = threading.Lock()
#: 所有 handler 共用同一个无状态过滤器实例
_GLOBAL_FILTER = RedactionFilter()

#: **已核实**无法再输出日志的 handler（level 门禁通过读回校验）
_BLOCKED_HANDLERS: List[str] = []
#: 其中"只能靠 level 门禁、连实例 handle 都改不动"的那部分（直驱仍可能输出）
_GATED_HANDLERS: List[str] = []
#: 连禁用都做不到的 handler：**未受保护**，健康状态必须是 unhealthy
_UNSECURED_HANDLERS: List[str] = []
#: 已包装（filter 不可用）的 handler 数量
_WRAPPED_HANDLERS: List[str] = []

#: 三个钩子的**可信原始实现**（模块导入时抓取；Batch 4.1.2 / AUD2-F2-02）。
#: 只接受 ``logging`` 自己的函数：第三方替换进来的函数一律不当作原实现。
_PRESTINE: Dict[str, Any] = {}
#: 检测到的"未知第三方替换"：钩子名 → 诊断信息（不含记录内容）
_FOREIGN_HOOKS: Dict[str, Dict[str, Any]] = {}

_HOOK_TARGETS = (
    ("Handler.handle", logging.Handler, "handle"),
    ("Logger.handle", logging.Logger, "handle"),
    ("Handler.format", logging.Handler, "format"),
)


def _is_stdlib_hook(fn: Any) -> bool:
    """``fn`` 是否是 Python ``logging`` 自己的实现（而不是第三方替换品）。"""
    mod = getattr(fn, "__module__", "") or ""
    return mod == "logging" or mod.startswith("logging.")


def _capture_pristine() -> None:
    """抓取"项目安装之前"的原始实现；抓不到可信实现就记 ``None``。"""
    for key, owner, name in _HOOK_TARGETS:
        try:
            fn = getattr(owner, name)
        except Exception:  # noqa: BLE001 - 极端的自定义 logging
            fn = None
        _PRESTINE[key] = fn if _is_stdlib_hook(fn) else None


_capture_pristine()


def _remember(bucket: List[str], handler: logging.Handler) -> None:
    """按 repr 去重登记，避免重复安装把计数刷大。"""
    label = repr(handler)
    if label not in bucket:
        bucket.append(label)


def _mark(obj: Any, attr: str, value: Any) -> bool:
    try:
        setattr(obj, attr, value)
        return True
    except Exception:  # noqa: BLE001 - __slots__ / 只读属性
        return False


def _level_gate_verified(handler: logging.Handler) -> bool:
    """``handler`` 的 level 门禁是否**真的**会把 CRITICAL 也挡在门外。

    （Batch 4.1.2 / AUD2-F2-01）``logging.Handler`` 没有 ``isEnabledFor``，
    真正的门禁在 ``Logger.callHandlers``：``record.levelno >= hdlr.level``。
    因此校验方式是**回读**：

    1. ``handler.level`` 必须是整数且 ``> logging.CRITICAL``（默认 50）；
       这同时覆盖"``setLevel`` 抛异常"与"``setLevel`` 是空操作"两种情况；
    2. 回读过程不能抛异常（自定义 ``level`` 属性抛错视为不可信）。
    """
    try:
        level = getattr(handler, "level", None)
        if not isinstance(level, int) or isinstance(level, bool):
            return False
        return level > logging.CRITICAL
    except Exception:  # noqa: BLE001 - 自定义 level 属性不可信
        return False


def _drop_record(record: logging.LogRecord) -> None:
    """替换实例 ``handle`` 的空实现：记录到此为止，什么都不做。"""
    return None


def _neutralize_handle(handler: logging.Handler) -> bool:
    """尝试把实例 ``handle`` 换成空实现；成功返回 True。"""
    try:
        handler.handle = _drop_record  # type: ignore[method-assign]
        return getattr(handler, "handle", None) is _drop_record
    except Exception:  # noqa: BLE001 - __slots__ / 只读属性 / 自定义 __setattr__
        return False


def install_on_handler(handler: Optional[logging.Handler],
                       prepend: bool = True) -> bool:
    """给单个 handler 安装脱敏；返回是否**本次新安装**。

    降级链（任何一步成功即视为"不会明文输出"）：
    1. 在 ``handler.filters`` 头部插入脱敏 filter；
    2. filter 不可用时，包装**实例**的 ``handle``；
    3. 再失败则禁用该 handler（level 调到 CRITICAL 之上）并向 stderr 告警。

    第 3 步**必须回读校验**（Batch 4.1.2 / AUD2-F2-01）：``setLevel`` 抛异常、
    被重写成空操作、或 level 被改回，都算"没有禁用"。此时该 handler 进入
    ``_UNSECURED_HANDLERS``，``redaction_health().healthy`` 为 False，绝不
    再计入 ``blocked_handlers``。

    重复调用返回 ``False``（幂等）。
    """
    if not isinstance(handler, logging.Handler):
        return False
    if getattr(handler, "_aitrader_redaction_mode", None):
        return False
    try:
        if any(isinstance(f, RedactionFilter) for f in handler.filters):
            _mark(handler, "_aitrader_redaction_mode", "filter")
            return False
        with _INSTALL_LOCK:
            if any(isinstance(f, RedactionFilter) for f in handler.filters):
                _mark(handler, "_aitrader_redaction_mode", "filter")
                return False
            if prepend:
                handler.filters.insert(0, _GLOBAL_FILTER)
            else:
                handler.addFilter(_GLOBAL_FILTER)
        _mark(handler, "_aitrader_redaction_mode", "filter")
        return True
    except Exception:  # noqa: BLE001 - 继续降级，不静默放行
        pass

    # 降级 1：包装实例 handle（filters 不可变时仍能脱敏）
    try:
        original = handler.handle

        def _guarded(record: logging.LogRecord, _orig: Any = original) -> Any:
            try:
                _GLOBAL_FILTER.filter(record)
            except BaseException:  # noqa: BLE001
                pass
            return _orig(record)

        _guarded._aitrader_redaction = True  # type: ignore[attr-defined]
        handler.handle = _guarded  # type: ignore[method-assign]
        if getattr(handler, "handle", None) is not _guarded:
            raise RuntimeError("handle 包装未生效")
        _mark(handler, "_aitrader_redaction_mode", "wrapper")
        _remember(_WRAPPED_HANDLERS, handler)
        return True
    except Exception:  # noqa: BLE001 - 进入最终降级
        pass

    # 降级 2：拒绝输出（fail-closed）+ 告警。
    # AUD2-F2-01：**不能**只看 setLevel 有没有抛异常——它可能被重写成空操作、
    # 也可能被拒绝而 level 没变。必须回读 level 并用 isEnabledFor 交叉验证；
    # 验证不过就是"没禁用"，绝不能计入 blocked_handlers。
    blocked = False
    try:
        handler.setLevel(logging.CRITICAL + 10)
    except Exception:  # noqa: BLE001 - 拒绝设置
        blocked = False
    else:
        blocked = _level_gate_verified(handler)

    if blocked:
        # 再尝试把实例 handle 换成空实现：连"绕过 logger 直驱"也堵住。
        # 改不动不算失败（logging 链路上已被 level 门禁拦住），但要如实记为 gated。
        gated_only = not _neutralize_handle(handler)
        _mark(handler, "_aitrader_redaction_mode", "blocked")
        _mark(handler, "_aitrader_output_blocked", "gated" if gated_only else "blocked")
        _remember(_BLOCKED_HANDLERS, handler)
        if gated_only:
            _remember(_GATED_HANDLERS, handler)
        print(f"[redaction] handler disabled (cannot install redaction): {handler!r}",
              file=sys.stderr)
        return True

    # 连"禁用"都做不到 → 该 handler 未受保护。健康状态必须如实暴露，
    # 不能把"函数没抛异常"当成"已拒绝输出"。
    _mark(handler, "_aitrader_redaction_mode", "unsecured")
    _mark(handler, "_aitrader_output_blocked", "")
    _remember(_UNSECURED_HANDLERS, handler)
    print(f"[redaction] handler NOT secured (cannot disable it): {handler!r} "
          f"- 该 handler 未被脱敏保护，redaction_health().healthy=false",
          file=sys.stderr)
    return False


def install_on_handlers(root: Optional[logging.Logger] = None) -> int:
    """给 root、所有已知 logger 的 handler 以及 ``lastResort`` 安装脱敏。

    返回**本次新增**的 handler 数量（重复调用为 0）。只处理 handler，不改变
    logger 级别；子 logger 的记录经传播到达 root handler 时同样会被脱敏。
    """
    root_logger = root if root is not None else logging.root
    targets: List[logging.Handler] = list(root_logger.handlers)
    try:
        for obj in logging.root.manager.loggerDict.values():
            if isinstance(obj, logging.Logger):
                targets.extend(obj.handlers)
    except Exception:  # noqa: BLE001 - 内省失败时至少保住 root
        pass
    if isinstance(logging.lastResort, logging.Handler):
        targets.append(logging.lastResort)

    installed = 0
    seen: Set[int] = set()
    for handler in targets:
        if id(handler) in seen:
            continue
        seen.add(id(handler))
        if install_on_handler(handler):
            installed += 1
    return installed


def _wrap_method(owner: Any, name: str, wrapper: Callable[..., Any],
                 key: str) -> bool:
    """把 ``owner.name`` 换成 ``wrapper``，并把原实现挂在 wrapper 上。

    * 幂等：当前已是**有效**包装（原实现可信）时返回 False；
    * 防叠加：若当前包装是旧实例，取其 ``_aitrader_original`` 作为原实现；
    * 打标 ``_aitrader_redaction``，供 ``redaction_health`` 与重装判断；
    * **只把 ``logging`` 自己的实现当作可信原实现**（Batch 4.1.2 / AUD2-F2-02）：
      第三方替换进来的函数（例如 ``lambda self, record: None``）绝不能被当作
      original 链进去——那会让日志永久静默。检测到未知替换时，链接回可信
      原始实现，并在 ``_FOREIGN_HOOKS`` 里如实记录（不静默）。
    """
    with _INSTALL_LOCK:
        current = getattr(owner, name)
        ours = bool(getattr(current, "_aitrader_redaction", False))
        current_original = getattr(current, "_aitrader_original", None)

        if ours and _is_stdlib_hook(current_original):
            # 已是有效包装：幂等返回，不叠加
            return False

        trusted = _PRESTINE.get(key)
        if _is_stdlib_hook(current):
            # 当前就是 logging 自己的实现：可信
            trusted = current
            _PRESTINE[key] = current
        elif _is_stdlib_hook(current_original):
            # 当前是我们的（或旧版）包装，原实现可信
            trusted = current_original
        else:
            # 未知第三方替换：不信任它
            _FOREIGN_HOOKS[key] = {
                "detected": True,
                "replaced_by_module": getattr(current, "__module__", "") or "",
                "replaced_by_name": getattr(current, "__name__", "") or "",
                "chain_to": "trusted" if trusted is not None else "none",
                "installed": trusted is not None,
            }
            if trusted is None:
                # 连可信原实现都没有 → 装不上，交给健康位报 unhealthy
                return False

        bound = wrapper(trusted)
        bound._aitrader_redaction = True          # type: ignore[attr-defined]
        bound._aitrader_original = trusted        # type: ignore[attr-defined]
        bound._aitrader_trusted = True            # type: ignore[attr-defined]
        bound._aitrader_foreign_bypassed = not _is_stdlib_hook(current) and not ours
        setattr(owner, name, bound)
        if not bound._aitrader_foreign_bypassed:
            _FOREIGN_HOOKS.pop(key, None)
        return True


def install_global_hook(force: bool = False) -> bool:
    """让**之后创建**的 handler 也自动获得脱敏（幂等、可自愈）。

    做法是包一层 ``logging.Handler.handle``：每次输出前检查该 handler 是否
    已有 :class:`RedactionFilter`，没有就补上（内部走 ``install_on_handler``
    的降级链），再调用原始实现。它不修改 level/formatter，也不改变输出格式。

    幂等判据是**当前属性是否仍是我们的有效包装**（原实现必须可信），而不是
    "曾经装过"——这样第三方重新赋值后再次调用可以恢复（Batch 4.1.1 /
    SR-F2-05）；第三方换成 no-op 时也不会把 no-op 当成原实现（4.1.2 /
    AUD2-F2-02）。
    """

    def _factory(original: Callable[..., Any]) -> Callable[..., Any]:
        def _handle(self: logging.Handler, record: logging.LogRecord) -> Any:
            try:
                if not isinstance(getattr(self, "_aitrader_redaction_mode", None), str):
                    install_on_handler(self)
            except Exception:  # noqa: BLE001 - 宁可继续（已包装）也不阻断
                pass
            return original(self, record)
        return _handle

    return _wrap_method(logging.Handler, "handle", _factory, "Handler.handle")


def install_logger_hook() -> bool:
    """在 ``Logger.handle``（分发入口）脱敏。

    这是覆盖"自定义 Handler 重写 handle"的关键一层：记录在交给任何 handler
    之前就已经是安全形态。对同一条记录重复执行是幂等的。
    """

    def _factory(original: Callable[..., Any]) -> Callable[..., Any]:
        def _handle(self: logging.Logger, record: logging.LogRecord) -> Any:
            try:
                _GLOBAL_FILTER.filter(record)
            except BaseException:  # noqa: BLE001
                pass
            return original(self, record)
        return _handle

    return _wrap_method(logging.Logger, "handle", _factory, "Logger.handle")


def install_format_guard() -> bool:
    """在 ``Handler.format`` 的**最终文本**上再脱敏一次（最后一道网）。

    覆盖"Formatter 自己重新生成内容"的场景（例如用 ``exc_info`` 重渲染）。
    已由 L1/L2 标记脱敏的记录直接跳过，避免每条日志两次全量正则。
    """

    def _factory(original: Callable[..., Any]) -> Callable[..., Any]:
        def _format(self: logging.Handler, record: logging.LogRecord) -> Any:
            text = original(self, record)
            if getattr(record, _REDACTED_FLAG, False):
                return text
            if not isinstance(text, str):
                return text
            return redact_text(text, truncate=False)
        return _format

    return _wrap_method(logging.Handler, "format", _factory, "Handler.format")


def _hook_state(key: str) -> Dict[str, Any]:
    """某个钩子当前是否仍是**可信**的项目包装。"""
    owner, name = {
        "Handler.handle": (logging.Handler, "handle"),
        "Logger.handle": (logging.Logger, "handle"),
        "Handler.format": (logging.Handler, "format"),
    }[key]
    current = getattr(owner, name, None)
    installed = bool(getattr(current, "_aitrader_redaction", False))
    original = getattr(current, "_aitrader_original", None)
    trusted = installed and _is_stdlib_hook(original)
    return {"installed": installed, "trusted_original": trusted,
            "original_module": getattr(original, "__module__", "") or ""}


def redaction_health() -> Dict[str, Any]:
    """只读健康信息：钩子是否**有效**、handler 是否能真正拒绝输出。

    与 4.1.1 的差别（Batch 4.1.2）：``logger_hook`` 只表示"我们的包装在"，
    新增 ``*_hook_trusted`` 表示"原实现可信"；``blocked_handlers`` 只统计
    **回读校验通过**的 handler，另有 ``gated_handlers`` / ``unsecured_handlers``
    与总的 ``healthy``。
    """
    hook_keys = ("Handler.handle", "Logger.handle", "Handler.format")
    states = {k: _hook_state(k) for k in hook_keys}
    handlers_unsecured = len(_UNSECURED_HANDLERS)
    hooks_ok = all(s["installed"] and s["trusted_original"] for s in states.values())
    foreign_broken = [k for k, v in _FOREIGN_HOOKS.items() if not v.get("installed", True)]
    return {
        "handler_hook": states["Handler.handle"]["installed"],
        "logger_hook": states["Logger.handle"]["installed"],
        "format_hook": states["Handler.format"]["installed"],
        "handler_hook_trusted": states["Handler.handle"]["trusted_original"],
        "logger_hook_trusted": states["Logger.handle"]["trusted_original"],
        "format_hook_trusted": states["Handler.format"]["trusted_original"],
        "foreign_hooks": {k: dict(v) for k, v in _FOREIGN_HOOKS.items()},
        "blocked_handlers": len(_BLOCKED_HANDLERS),
        "gated_handlers": len(_GATED_HANDLERS),
        "unsecured_handlers": handlers_unsecured,
        "wrapped_handlers": len(_WRAPPED_HANDLERS),
        "registered_secrets": len(_SECRETS),
        # degraded：保护仍然有效，但有需要人工确认的弱化：
        #   * 为了不让日志静默而**绕过**了某个第三方钩子（详见 foreign_hooks）；
        #   * 某个 handler 只能靠 level 门禁拦住（Logger.callHandlers 之外被
        #     直接驱动时仍可能输出，见 gated_handlers）。
        "degraded": bool(_FOREIGN_HOOKS or _GATED_HANDLERS),
        "healthy": bool(hooks_ok and handlers_unsecured == 0 and not foreign_broken),
    }


def ensure_hooks() -> Dict[str, Any]:
    """检测并修复被第三方覆盖的钩子；返回修复结果 + 健康状态。

    Batch 4.1.2 / AUD2-F2-02：
    * 只在"当前钩子仍是**有效**包装（原实现可信）"时才算已安装；
    * 被换成 no-op / 任意第三方函数时，**不把它当原实现**，而是链回
      ``_PRESTINE`` 里的可信实现，并在 ``foreign_hooks`` 里如实记录；
    * 连可信实现都拿不到（导入时抓不到）→ 不安装、``healthy=False``、
      向 stderr 告警，绝不虚报 ``logger_hook=True``。
    """
    repaired = {
        "handler": install_global_hook(),
        "logger": install_logger_hook(),
        "format": install_format_guard(),
    }
    health = redaction_health()
    health["repaired"] = repaired
    if not health["healthy"]:
        reasons: List[str] = []
        if not health["handler_hook_trusted"]:
            reasons.append("Handler.handle")
        if not health["logger_hook_trusted"]:
            reasons.append("Logger.handle")
        if not health["format_hook_trusted"]:
            reasons.append("Handler.format")
        if health["unsecured_handlers"]:
            reasons.append(f"unsecured_handlers={health['unsecured_handlers']}")
        print("[redaction] health check FAILED: 未受保护的组件 "
              f"{', '.join(reasons) or 'unknown'}（详见 redaction_health）",
              file=sys.stderr)
    return health


def uninstall_hooks() -> Dict[str, Any]:
    """把三个钩子**精确**恢复为可信原始实现（正常卸载路径）。

    只恢复"当前仍是我们的包装"的钩子；第三方后来改过的一律跳过并记录，
    不覆盖别人的改动。返回 ``{"restored": [...], "skipped": [...]}``。
    """
    restored: List[str] = []
    skipped: List[str] = []
    with _INSTALL_LOCK:
        for key, owner, name in _HOOK_TARGETS:
            current = getattr(owner, name, None)
            if not getattr(current, "_aitrader_redaction", False):
                skipped.append(key)
                continue
            target = _PRESTINE.get(key)
            if target is None:
                skipped.append(key)
                continue
            try:
                setattr(owner, name, target)
                restored.append(key)
            except Exception:  # noqa: BLE001
                skipped.append(key)
        _FOREIGN_HOOKS.clear()
    return {"restored": restored, "skipped": skipped}
