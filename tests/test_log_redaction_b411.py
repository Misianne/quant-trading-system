"""US-P1 Batch 4.1.1 / F-2 定点修复测试（T01–T14）。

与 ``test_log_redaction_b41.py`` 的分工
--------------------------------------
* b41 验证"三条泄漏路径在真实 handler 输出层被堵住"；
* 本文件验证 **4.1.1 的定点修复**：字段名归一化、转义 JSON、``exc_info``
  中和、自定义 ``handle``、hook 被覆盖后的自愈、handler 安装失败时
  拒绝输出（fail-closed）。

判定原则
--------
一律以 **handler 真实输出的字符串** 为准，不看函数返回值。``KNOWN_BOUNDARY``
标记的用例故意记录"仍然拦不住"的路径，断言的是"确实泄露"，而不是跳过。

所有密钥均为伪造串，不联网、不触碰真实凭证与配置文件。
"""
from __future__ import annotations

import io
import json
import logging

import pytest

from ai_trader import log_redaction as lr

# 全部伪造；长度 >= MIN_SECRET_LEN，避免被"太短不登记"规则跳过
AK = "B411-FAKE-API-KEY-3c9f1a7e"
SK = "B411-FAKE-SESSION-8d2b4c6f"
TK = "B411-FAKE-TOKEN-5e7a9c1b"


@pytest.fixture(autouse=True)
def _isolate_logging_state():
    """还原全局钩子、handler 计数与凭据登记表，避免测试互相污染。"""
    saved = (logging.Handler.handle, logging.Logger.handle, logging.Handler.format)
    secrets_before = dict(lr._SECRETS)
    blocked_before = list(lr._BLOCKED_HANDLERS)
    wrapped_before = list(lr._WRAPPED_HANDLERS)
    try:
        yield
    finally:
        logging.Handler.handle = saved[0]
        logging.Logger.handle = saved[1]
        logging.Handler.format = saved[2]
        with lr._SECRETS_LOCK:
            lr._SECRETS.clear()
            lr._SECRETS.update(secrets_before)
        lr._BLOCKED_HANDLERS[:] = blocked_before
        lr._WRAPPED_HANDLERS[:] = wrapped_before


def _install_all() -> None:
    """按生产路径安装全部防护层（root 上没有 handler 时也没问题）。"""
    lr.install_global_hook()
    lr.install_logger_hook()
    lr.install_format_guard()


def _logger_with(name: str, handler: logging.Handler,
                 fmt: str | None = "%(levelname)s %(message)s") -> logging.Logger:
    if fmt is not None:
        handler.setFormatter(logging.Formatter(fmt))
    log = logging.getLogger(name)
    log.handlers[:] = [handler]
    log.setLevel(logging.DEBUG)
    log.propagate = False
    return log


class _BufHandler(logging.StreamHandler):
    """真实 StreamHandler + StringIO，输出即"最终日志文本"。"""

    def __init__(self) -> None:
        self.buf = io.StringIO()
        super().__init__(self.buf)


# --------------------------------------------------------------------------- #
# T01 / T02：字段名变体与误伤边界
# --------------------------------------------------------------------------- #
def test_t01_sensitive_key_variants_are_masked():
    variants = [
        f"session_id={SK}",
        f"x_api_key={AK}",
        f"X-Api-Key: {AK}",
        f"'sessionToken': '{SK}'",
        f"apiKey={AK}",
        f"?x_api_key={AK}&session_id={SK}",
        f"token={TK}",
    ]
    for text in variants:
        out = lr.redact_text(text)
        for secret in (AK, SK, TK):
            assert secret not in out, (text, out)
        assert lr._MASK in out, (text, out)


def test_t02_non_sensitive_suffixes_are_not_masked():
    """``token_count`` / ``session_timeout`` 这类"带后缀的普通字段"不能误伤。"""
    for text in ("token_count=5", "session_timeout=30", "api_key_count=2",
                 "session_len=12", "token_ttl=600", "api_key_path=/tmp/x"):
        assert lr.redact_text(text) == text, text


# --------------------------------------------------------------------------- #
# T03：转义 JSON
# --------------------------------------------------------------------------- #
def test_t03_escaped_json_is_masked_and_stays_valid_json():
    payload = {"api_key": AK, "session_id": SK, "symbol": "AAPL"}
    text = json.dumps(payload, ensure_ascii=False)

    for layer in range(5):          # 0 层（原始）到 4 层 json.dumps 嵌套
        out = lr.redact_text(text)
        assert AK not in out and SK not in out, (layer, out)
        # 逐层解开后必须仍是合法 JSON，且业务字段未被破坏
        decoded = json.loads(out)
        guard = 0
        while isinstance(decoded, str) and guard < 8:
            decoded = json.loads(decoded)
            guard += 1
        assert isinstance(decoded, dict), (layer, decoded)
        assert decoded["symbol"] == "AAPL", (layer, decoded)
        assert decoded["api_key"] == lr._MASK, (layer, decoded)
        text = json.dumps(text, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# T04 / T05：exc_info 中和
# --------------------------------------------------------------------------- #
def test_t04_exc_info_is_neutralized_for_sdk_json_formatter():
    """SDK ``JsonFormatter`` 会用 ``exc_info`` 重渲染 —— 必须切断来源。"""
    from fsopenapi.logging_utils import JsonFormatter

    # 生产路径：凭证在解析时就被登记为"需要抹掉的字面量"（L0 层）。
    # 这里显式登记，模拟 _register_secret_values() 已完成。
    assert lr.register_secret(AK) is True
    _install_all()
    h = _BufHandler()
    h.setFormatter(JsonFormatter())
    log = _logger_with("b411.t04", h, fmt=None)

    try:
        raise RuntimeError(f"connect failed api_key={AK}")
    except RuntimeError:
        log.exception("handshake rejected session_id=%s", SK)

    out = h.buf.getvalue()
    assert AK not in out, out
    assert SK not in out, out
    assert "RuntimeError" in out, out          # 仍能看到异常类型，便于排查
    payload = json.loads(out)
    assert payload["msg"] == "handshake rejected session_id: ***", payload
    # exc_info 被中和后，JsonFormatter 不再有可重渲染的来源
    assert "exception" not in payload, payload


def test_t05_standard_formatter_keeps_traceback():
    # 裸令牌出现在 traceback 的行尾（不是 key=value 形态），只能靠 L0 字面量登记
    assert lr.register_secret(AK) is True
    _install_all()
    h = _BufHandler()
    log = _logger_with("b411.t05", h, "%(levelname)s %(message)s")

    try:
        raise ValueError(f"bad key {AK}")
    except ValueError:
        log.exception("boom")

    out = h.buf.getvalue()
    assert AK not in out, out
    assert "Traceback (most recent call last)" in out, out
    assert "ValueError" in out, out


# --------------------------------------------------------------------------- #
# T06 / T07：自定义 handle 的 handler
# --------------------------------------------------------------------------- #
class _CustomHandleHandler(logging.Handler):
    """重写 ``handle`` 的第三方 handler：基线实现（不过滤、不调 format）。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def handle(self, record: logging.LogRecord) -> bool:  # noqa: D401
        self.lines.append(record.getMessage())
        return True

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        self.lines.append(self.format(record))


def test_t06_custom_handle_handler_output_is_masked():
    """``Handler.handle`` 被重写时，靠 ``Logger.handle`` 这一层兜住。"""
    _install_all()
    h = _CustomHandleHandler()
    log = _logger_with("b411.t06", h)

    log.warning("subscribe failed x_api_key=%s", AK)

    assert h.lines, "handler 没收到记录，用例无效"
    assert AK not in "".join(h.lines), h.lines
    assert lr._MASK in h.lines[0]


class _FullyCustomHandler(logging.Handler):
    """KNOWN_BOUNDARY：既重写 ``handle``、又重写 ``format``，并被直接驱动。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def handle(self, record: logging.LogRecord) -> bool:
        self.lines.append(self.format(record))
        return True

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        text = f"{record.getMessage()} | {record.exc_text or ''}"
        if record.exc_info:
            text += " | " + logging.Formatter().formatException(record.exc_info)
        return text

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        self.lines.append(self.format(record))


def test_t07_known_boundary_direct_handle_with_custom_format():
    """记录**不经过 logger**、且 handler 两个方法都自定义 → 仍然泄露。

    这是"不改 SDK / 不接管 logging 全局"前提下的真实边界：
    ``Logger.handle``（L2）与 ``Handler.format``（L3）都被子类重写绕过，
    而记录根本没有经过任何 logger。审计要求把它写成显式用例，而不是
    假装已被覆盖 —— 项目内的 handler 都由 ``setup_logging`` 创建，
    第三方代码若这样直连，属于"自己拼装 logging"的极端场景。
    """
    _install_all()
    h = _FullyCustomHandler()
    log = logging.getLogger("b411.t07")
    record = log.makeRecord(
        log.name, logging.ERROR, __file__, 1, "connect api_key=%s", (AK,), None)

    h.handle(record)

    assert AK in h.lines[0], "该路径本应收敛为 KNOWN_BOUNDARY，若被覆盖请更新本用例"
    # 同一份 record 走正常 logger 分发时是被打码的（说明防线本身有效）
    h.lines.clear()
    log.handlers[:] = [h]
    log.setLevel(logging.DEBUG)
    log.propagate = False
    log.error("connect api_key=%s", AK)
    assert AK not in "".join(h.lines), h.lines


# --------------------------------------------------------------------------- #
# T08 / T09：hook 自愈与幂等
# --------------------------------------------------------------------------- #
def test_t08_ensure_hooks_heals_overwritten_handler_handle():
    _install_all()
    hijacked = lambda self, record: None  # noqa: E731 - 模拟第三方覆盖
    logging.Handler.handle = hijacked

    health = lr.ensure_hooks()

    assert health["repaired"]["handler"] is True, health
    assert health["handler_hook"] is True, health
    assert logging.Handler.handle is not hijacked

    h = _BufHandler()
    log = _logger_with("b411.t08", h)
    log.warning("token=%s", TK)
    assert TK not in h.buf.getvalue(), h.buf.getvalue()


def test_t09_repeat_install_does_not_stack():
    _install_all()
    base = logging.Handler.handle
    assert lr.install_global_hook() is False
    assert logging.Handler.handle is base, "重复安装不得再包一层"
    # 原实现只挂在 _aitrader_original 上，链深度为 1
    assert getattr(base, "_aitrader_original", None) is not None
    assert not getattr(getattr(base, "_aitrader_original"), "_aitrader_redaction", False)


# --------------------------------------------------------------------------- #
# T10：record.message 死分支
# --------------------------------------------------------------------------- #
def test_t10_preset_record_message_is_masked():
    log = logging.getLogger("b411.t10")
    record = log.makeRecord(log.name, logging.INFO, __file__, 1, "hello", (), None)
    # 模拟某个 Formatter / handler 预先算好的 message 缓存
    record.message = f"payload x_api_key={AK}"

    assert lr._GLOBAL_FILTER.filter(record) is True

    assert AK not in record.message, record.message
    assert lr._MASK in record.message


# --------------------------------------------------------------------------- #
# T11 / T12：安装降级链
# --------------------------------------------------------------------------- #
class _TupleFiltersHandler(logging.StreamHandler):
    """``filters`` 只读为元组 → 无法 insert → 必须降级包装 ``handle``。"""

    def __init__(self) -> None:
        self.buf = io.StringIO()
        super().__init__(self.buf)

    @property
    def filters(self):
        return tuple(getattr(self, "_frozen", ()))

    @filters.setter
    def filters(self, value):
        object.__setattr__(self, "_frozen", tuple(value))


def test_t11_tuple_filters_falls_back_to_handle_wrapper():
    h = _TupleFiltersHandler()
    assert lr.install_on_handler(h) is True
    assert getattr(h, "_aitrader_redaction_mode", "") == "wrapper", vars(h)

    log = logging.getLogger("b411.t11")
    record = log.makeRecord(log.name, logging.WARNING, __file__, 1,
                            "handshake x_api_key=%s", (AK,), None)
    # 绕过 logger 直接驱动 handler：证明"仅靠 handler 级包装"也够
    h.handle(record)

    out = h.buf.getvalue()
    assert AK not in out, out
    assert lr._MASK in out


class _HostileHandler(logging.Handler):
    """filters 不可读、handle 不可写 → 只能走"拒绝输出"。"""

    def __init__(self) -> None:
        self.buf = io.StringIO()
        super().__init__()

    @property
    def filters(self):
        raise RuntimeError("filters unavailable")

    @filters.setter
    def filters(self, value):
        pass

    def __setattr__(self, name, value):
        if name == "handle":
            raise RuntimeError("handle readonly")
        object.__setattr__(self, name, value)

    def emit(self, record: logging.LogRecord) -> None:
        self.buf.write(self.format(record) + "\n")


def test_t12_hostile_handler_is_blocked_and_produces_no_output():
    h = _HostileHandler()
    assert lr.install_on_handler(h) is True
    assert getattr(h, "_aitrader_redaction_mode", "") == "blocked"
    assert h.level == logging.CRITICAL + 10
    assert lr.redaction_health()["blocked_handlers"] >= 1

    log = logging.getLogger("b411.t12")
    log.handlers[:] = [h]
    log.setLevel(logging.DEBUG)
    log.propagate = False
    for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR):
        log.log(level, "x_api_key=%s", AK)

    assert h.buf.getvalue() == "", "被禁用的 handler 不得产生任何输出"


# --------------------------------------------------------------------------- #
# T13：回归 —— msg/args/extra/lastResort/filter 顺序
# --------------------------------------------------------------------------- #
def test_t13_message_args_extra_and_filter_order_are_preserved():
    _install_all()
    h = _BufHandler()
    dropped: list[logging.LogRecord] = []

    def _drop(record: logging.LogRecord) -> bool:
        dropped.append(record)
        return False

    h.addFilter(_drop)          # 第三方 filter：拒收
    log = _logger_with("b411.t13", h)

    # 1) %s 参数合并后仍然可用（args 被消化掉，不会破坏格式化）
    record = log.makeRecord(log.name, logging.INFO, __file__, 1,
                            "order %s qty=%s", ("AAPL", 100), None)
    record.symbol = "AAPL"
    record.qty = 100
    record.api_key = AK
    lr._GLOBAL_FILTER.filter(record)
    assert record.getMessage().startswith("order AAPL qty=100"), record.getMessage()
    assert record.args is None
    assert record.symbol == "AAPL" and record.qty == 100, "普通业务字段被误改"
    assert record.api_key == lr._MASK
    assert record.levelno == logging.INFO and record.lineno == 1

    # 2) 我们的 filter 在最前面，但第三方 filter 仍可拒收（顺序未被破坏）
    assert lr.install_on_handler(h) is True
    assert isinstance(h.filters[0], lr.RedactionFilter)
    log.warning("x_api_key=%s", AK)
    assert h.buf.getvalue() == ""
    assert len(dropped) == 1


def test_t13b_last_resort_handler_is_redacted():
    _install_all()
    buf = io.StringIO()
    previous = logging.lastResort
    logging.lastResort = logging.StreamHandler(buf)
    logging.lastResort.setFormatter(logging.Formatter("%(message)s"))
    try:
        assert lr.install_on_handler(logging.lastResort) is True
        log = logging.getLogger("b411.t13b")
        log.handlers[:] = []
        log.propagate = False
        log.setLevel(logging.DEBUG)
        log.warning("token=%s", TK)
        out = buf.getvalue()
    finally:
        logging.lastResort = previous
    assert TK not in out, out
    assert lr._MASK in out


# --------------------------------------------------------------------------- #
# T14：限额
# --------------------------------------------------------------------------- #
def test_t14_oversize_truncate_depth_and_node_limits():
    big = "a" * (lr.MAX_RAW_TEXT_CHARS + 1)
    assert lr.redact_text(big) == lr._OVERSIZED

    long_text = "b" * (lr.MAX_TEXT_CHARS + 1)
    out = lr.redact_text(long_text)
    assert len(out) == lr.MAX_TEXT_CHARS + len(lr._TRUNCATED)
    assert out.endswith(lr._TRUNCATED)

    node = "leaf"
    for _ in range(lr.MAX_DEPTH + 3):
        node = {"n": node}
    assert lr._TRUNCATED in json.dumps(lr.redact_obj(node), ensure_ascii=False)

    wide = list(range(lr.MAX_ITEMS + 50))
    assert lr._TRUNCATED in lr.redact_obj(wide)

    circular: list = []
    circular.append(circular)
    assert lr._CIRCULAR in json.dumps(lr.redact_obj(circular), ensure_ascii=False)
