"""US-P1 Batch 4.1 / F-2：统一日志脱敏的独立测试。

重点不是"函数返回值对不对"，而是**从 handler 的真实输出**确认三条泄漏路径
都被堵住：

1. 子 logger —— 父 logger 的 filter 不会处理传播上来的记录；
2. ``websockets`` DEBUG 握手 —— 记录是 ``msg + 对象 args``，格式化发生在
   handler 里，只改 ``msg`` 抹不掉；
3. 异常 traceback / 结构化 extra / **之后才创建**的 handler。

所有用例都不联网、不启动行情连接、不触碰真实凭证。
"""
from __future__ import annotations

import io
import logging

import pytest

from ai_trader import log_redaction as lr
from ai_trader.runtime import setup_logging

# 全部为伪造值；长度足够触发字面量登记（>= MIN_SECRET_LEN）
AK = "B41-FAKE-API-KEY-9f3a7c1d"
SK = "B41-FAKE-SESSION-4e8b2a6c"
PRIV = "B41-FAKE-PRIVATE-KEY-7d1c5a9e"
BARE = "B41BARE-5c2e8a1f"


@pytest.fixture
def root_capture():
    """在 root 上挂一个真实 handler，并按生产路径安装脱敏。"""
    root = logging.getLogger()
    old_level = root.level
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s :: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    setup_logging("DEBUG")
    try:
        yield buf, handler
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


# --------------------------------------------------------------------------- #
# 1. 子 logger：父 logger 的 filter 管不到
# --------------------------------------------------------------------------- #
def test_child_logger_is_redacted_at_handler(root_capture):
    buf, _ = root_capture
    child = logging.getLogger("ai_trader.market.feed.child")
    # 前提：子 logger 自己没有 filter（否则这条用例没有鉴别力）
    assert not any(isinstance(f, lr.RedactionFilter) for f in child.filters)

    child.warning("handshake X-Api-Key: %s X-Session: %s", AK, SK)

    out = buf.getvalue()
    assert AK not in out, out
    assert SK not in out, out
    assert "X-Api-Key: ***" in out, out


def test_strategy_engine_logger_is_redacted(root_capture):
    buf, _ = root_capture
    logging.getLogger("ai_trader.strategy.engine").error(
        "order rejected token=%s", SK)
    out = buf.getvalue()
    assert SK not in out, out
    assert "token: ***" in out, out


# --------------------------------------------------------------------------- #
# 2. websockets DEBUG 握手：msg + 对象 args
# --------------------------------------------------------------------------- #
def test_websockets_server_debug_handshake_is_redacted(root_capture):
    buf, _ = root_capture

    class _Request:
        def __str__(self) -> str:
            return (f"<Request headers X-Api-Key: {AK} X-Session: {SK} "
                    f"Authorization: Bearer {PRIV}>")

    ws_logger = logging.getLogger("websockets.server")
    old_level = ws_logger.level
    ws_logger.setLevel(logging.DEBUG)
    try:
        ws_logger.debug("%s > %s", "server", _Request())
    finally:
        ws_logger.setLevel(old_level)

    out = buf.getvalue()
    assert out, "DEBUG 下应有输出，否则用例没有鉴别力"
    assert AK not in out, out
    assert SK not in out, out
    assert PRIV not in out, out
    assert "X-Api-Key: ***" in out, out


# --------------------------------------------------------------------------- #
# 3. 异常 traceback / exc_text
# --------------------------------------------------------------------------- #
def test_traceback_is_redacted(root_capture):
    buf, _ = root_capture
    try:
        raise RuntimeError(f"auth failed api_key={AK} session={SK}")
    except RuntimeError:
        logging.getLogger("ai_trader.market.feed.child").exception("connect failed")

    out = buf.getvalue()
    assert "RuntimeError" in out, "traceback 仍应保留可诊断信息"
    assert AK not in out, out
    assert SK not in out, out


def test_preexisting_exc_text_is_redacted_too():
    record = logging.LogRecord("ai_trader.market.feed.child", logging.ERROR,
                               __file__, 1, "boom", None, None)
    record.exc_text = f"Traceback (most recent call last): api_key={AK}"
    assert lr.RedactionFilter().filter(record) is True
    assert AK not in (record.exc_text or "")
    assert "Traceback" in (record.exc_text or "")


# --------------------------------------------------------------------------- #
# 4. 结构化 extra（嵌套）与"之后创建的 handler"
# --------------------------------------------------------------------------- #
def test_nested_extra_and_future_handler_are_redacted(root_capture):
    for value in (AK, SK, PRIV):
        lr.register_secret(value)
    payload = {
        "nested": [
            {"token": SK, "note": f"raw {PRIV}"},
            (f"api_key={AK}", 7),
        ],
    }
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(
        logging.Formatter("%(message)s | p=%(payload)s | k=%(api_key)s"))
    logging.getLogger().addHandler(handler)
    try:
        # 新 handler 在 setup_logging() 之后创建，自身还没有 filter
        assert not any(isinstance(f, lr.RedactionFilter) for f in handler.filters)
        logging.getLogger("ai_trader.market.feed.child").warning(
            "cfg loaded", extra={"payload": payload, "api_key": AK})
    finally:
        logging.getLogger().removeHandler(handler)

    out = buf.getvalue()
    assert out, "extra 应被 formatter 输出，否则用例没有鉴别力"
    assert AK not in out, out
    assert SK not in out, out
    assert PRIV not in out, out
    assert "api_key: ***" in out, out


def test_registered_secret_masked_even_without_field_name(root_capture):
    buf, _ = root_capture
    lr.register_secret(BARE)
    logging.getLogger("ai_trader.strategy.engine").info("payload=%s", BARE)
    out = buf.getvalue()
    assert BARE not in out, out
    assert "***" in out, out


# --------------------------------------------------------------------------- #
# 5. fail-closed：过滤器自身异常不得泄露原文
# --------------------------------------------------------------------------- #
def _boom_record() -> logging.LogRecord:
    class _Boom:
        def __str__(self) -> str:
            raise RuntimeError("cannot stringify")

    return logging.LogRecord(
        "ai_trader.market.feed.child", logging.ERROR, __file__, 1,
        f"secret context {AK} %s", (_Boom(),), None)


def test_filter_exception_fails_closed_without_leaking(root_capture):
    buf, handler = root_capture
    record = _boom_record()
    handler.handle(record)  # Handler.handle 本身返回 None，这里只验证输出
    out = buf.getvalue()
    assert AK not in out, out
    assert lr._PLACEHOLDER in out, out
    # 记录本身也已被替换，后续 handler 不可能拿到原文
    assert AK not in record.getMessage(), record.getMessage()
    assert record.args is None


def test_filter_never_raises_on_weird_record():
    record = _boom_record()
    # 直接调用也不允许把异常抛给 logging 调用方
    assert lr.RedactionFilter().filter(record) is True


# --------------------------------------------------------------------------- #
# 6. 安装幂等：重复安装不重复处理
# --------------------------------------------------------------------------- #
def test_install_on_handler_is_idempotent():
    handler = logging.StreamHandler(io.StringIO())
    assert lr.install_on_handler(handler) is True
    assert lr.install_on_handler(handler) is False
    assert sum(isinstance(f, lr.RedactionFilter) for f in handler.filters) == 1


def test_setup_logging_installs_on_existing_root_handlers():
    handler = logging.StreamHandler(io.StringIO())
    logging.getLogger().addHandler(handler)
    try:
        setup_logging()
        assert any(isinstance(f, lr.RedactionFilter) for f in handler.filters)
        setup_logging()
        assert sum(isinstance(f, lr.RedactionFilter) for f in handler.filters) == 1
    finally:
        logging.getLogger().removeHandler(handler)


def test_repeated_filtering_is_idempotent(root_capture):
    buf, handler = root_capture
    record = logging.LogRecord("ai_trader.market.feed.child", logging.WARNING,
                               __file__, 1, "X-Api-Key: %s", (AK,), None)
    flt = lr.RedactionFilter()
    flt.filter(record)
    first = record.getMessage()
    flt.filter(record)
    assert record.getMessage() == first
    handler.handle(record)
    out = buf.getvalue()
    assert out.count("***") == 1, out


# --------------------------------------------------------------------------- #
# 7. 纯函数：类型、限额、循环引用、PEM
# --------------------------------------------------------------------------- #
def test_redact_text_is_idempotent_and_keeps_normal_text():
    once = lr.redact_text(f"X-Api-Key: {AK}")
    twice = lr.redact_text(once)
    assert once == twice == "X-Api-Key: ***"
    assert lr.redact_text("") == ""
    assert lr.redact_text("正常中文日志") == "正常中文日志"


def test_redact_text_masks_pem_blocks():
    pem = ("-----BEGIN PRIVATE KEY-----\n"
           "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
           "-----END PRIVATE KEY-----")
    out = lr.redact_text(f"key loaded:\n{pem}\ndone")
    assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" not in out
    assert out.startswith("key loaded:")
    assert out.endswith("done")


def test_redact_obj_preserves_container_types_and_sensitive_keys():
    stripped = lr.redact_obj({"token": AK, "token_count": 2, "nested": (AK, [1])})
    assert stripped["token"] == "***"
    assert stripped["token_count"] == 2
    assert isinstance(stripped["nested"], tuple)
    assert stripped["nested"][1] == [1]


def test_redact_obj_bounds_depth_items_and_length():
    deep = "x"
    for _ in range(50):
        deep = [deep]
    out = repr(lr.redact_obj(deep))
    assert lr._TRUNCATED in out
    assert len(out) < 5000, "深度必须被截断，不能展开整棵树"

    circular = []
    circular.append(circular)
    assert "[circular]" in repr(lr.redact_obj(circular))

    long_text = "a" * (lr.MAX_TEXT_CHARS + 5000)
    redacted = lr.redact_text(long_text)
    assert len(redacted) <= lr.MAX_TEXT_CHARS + len(lr._TRUNCATED)
    assert redacted.endswith(lr._TRUNCATED)

    oversized = "a" * (lr.MAX_RAW_TEXT_CHARS + 1)
    assert lr.redact_text(oversized) == lr._OVERSIZED

    big = list(range(lr.MAX_ITEMS + 50))
    out_list = lr.redact_obj(big)
    assert len(out_list) == lr.MAX_ITEMS + 1
    assert out_list[-1] == lr._TRUNCATED


def test_redact_obj_handles_bytes_and_exceptions():
    lr.register_secret(AK)
    assert lr.redact_obj(AK.encode()) == "***"
    try:
        raise ValueError(f"bad token={SK}")
    except ValueError as exc:
        text = lr.redact_obj(exc)
    assert SK not in str(text)


def test_short_values_are_not_registered():
    before = lr.redact_text("ok")
    assert lr.register_secret("short") is False
    assert lr.redact_text("ok") == before
    assert lr.register_secret(None) is False
