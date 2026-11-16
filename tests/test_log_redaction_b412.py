"""US-P1 Batch 4.1.2 / F-2 定点修复测试（AUD2-F2-01、AUD2-F2-02）。

覆盖两个"健康状态说安全、实际不安全"的缺陷：

* **AUD2-F2-01**：末级降级 ``setLevel(CRITICAL+10)`` 失败时，handler 仍然
  可能输出，却被计入 ``blocked_handlers``。
* **AUD2-F2-02**：第三方把 ``Logger.handle`` 换成 no-op 后，``ensure_hooks()``
  把 no-op 当成原实现链入 → 日志永久静默，而 ``logger_hook`` 仍是 True。

判定原则
--------
一律以 **handler 真实输出的字符串** 为准，不看函数返回值；每个用例结束都
把三个钩子还原到 ``log_redaction._PRESTINE``（可信原始实现），避免互相污染。
所有密钥均为伪造串，不联网、不写文件。
"""
from __future__ import annotations

import io
import logging

import pytest

from ai_trader import log_redaction as lr

AK = "B412-FAKE-API-KEY-7d1e4a9c"
TK = "B412-FAKE-TOKEN-2f8b6c3d"


def _pristine() -> tuple:
    return (lr._PRESTINE["Handler.handle"],
            lr._PRESTINE["Logger.handle"],
            lr._PRESTINE["Handler.format"])


@pytest.fixture(autouse=True)
def _isolate():
    saved = (logging.Handler.handle, logging.Logger.handle, logging.Handler.format)
    pristine = _pristine()
    blocked = list(lr._BLOCKED_HANDLERS)
    gated = list(lr._GATED_HANDLERS)
    unsecured = list(lr._UNSECURED_HANDLERS)
    wrapped = list(lr._WRAPPED_HANDLERS)
    foreign = {k: dict(v) for k, v in lr._FOREIGN_HOOKS.items()}
    pre = dict(lr._PRESTINE)
    logging.Handler.handle, logging.Logger.handle, logging.Handler.format = pristine
    lr._BLOCKED_HANDLERS.clear()
    lr._GATED_HANDLERS.clear()
    lr._UNSECURED_HANDLERS.clear()
    lr._WRAPPED_HANDLERS.clear()
    lr._FOREIGN_HOOKS.clear()
    try:
        yield
    finally:
        logging.Handler.handle = saved[0]
        logging.Logger.handle = saved[1]
        logging.Handler.format = saved[2]
        lr._BLOCKED_HANDLERS[:] = blocked
        lr._GATED_HANDLERS[:] = gated
        lr._UNSECURED_HANDLERS[:] = unsecured
        lr._WRAPPED_HANDLERS[:] = wrapped
        lr._FOREIGN_HOOKS.clear()
        lr._FOREIGN_HOOKS.update(foreign)
        lr._PRESTINE.clear()
        lr._PRESTINE.update(pre)


class _BufHandler(logging.StreamHandler):
    def __init__(self) -> None:
        self.buf = io.StringIO()
        super().__init__(self.buf)


def _logger_with(name: str, handler: logging.Handler) -> logging.Logger:
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger(name)
    log.handlers[:] = [handler]
    log.setLevel(logging.DEBUG)
    log.propagate = False
    return log


# --------------------------------------------------------------------------- #
# AUD2-F2-01：禁用必须"回读校验"，不能只看有没有抛异常
# --------------------------------------------------------------------------- #
class _BlockableHandler(logging.Handler):
    """filters 只读（元组）、handle 不可写 → 必然落到末级"禁用"。"""

    def __init__(self) -> None:
        self.stream = io.StringIO()
        super().__init__()
        object.__setattr__(self, "_sealed", True)

    @property
    def filters(self):
        return ()

    @filters.setter
    def filters(self, value):
        pass

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False) and name == "handle":
            raise RuntimeError("handle readonly")
        object.__setattr__(self, name, value)

    def emit(self, record: logging.LogRecord) -> None:
        self.stream.write(self.format(record) + "\n")


class _SetLevelRaises(_BlockableHandler):
    """setLevel 直接抛异常；同时重写 format（绕开 L3 兜底）。"""

    def setLevel(self, level):
        raise RuntimeError("setLevel blocked")

    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


class _SetLevelSilent(_BlockableHandler):
    """setLevel 不抛异常，但什么也不做（"设置后未生效"）。"""

    def setLevel(self, level):
        return None


def test_f201_setlevel_success_is_blocked_and_silent():
    _install_all()
    h = _BlockableHandler()
    assert lr.install_on_handler(h) is True
    assert getattr(h, "_aitrader_redaction_mode", "") == "blocked"
    assert h.level > logging.CRITICAL, h.level

    health = lr.redaction_health()
    assert health["blocked_handlers"] >= 1, health
    assert health["unsecured_handlers"] == 0, health
    assert health["healthy"] is True, health

    log = _logger_with("b412.f201a", h)
    for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR,
                  logging.CRITICAL):
        log.log(level, "x_api_key=%s", AK)
    assert h.stream.getvalue() == "", "已确认禁用的 handler 不得产生任何输出"


def test_f201_setlevel_raises_is_unsecured_not_blocked():
    h = _SetLevelRaises()
    assert lr.install_on_handler(h) is False
    assert getattr(h, "_aitrader_redaction_mode", "") == "unsecured"

    health = lr.redaction_health()
    assert health["blocked_handlers"] == 0, health
    assert health["unsecured_handlers"] == 1, health
    assert health["healthy"] is False, health


def test_f201_setlevel_silent_noop_is_unsecured():
    h = _SetLevelSilent()
    assert lr.install_on_handler(h) is False
    assert getattr(h, "_aitrader_redaction_mode", "") == "unsecured"
    assert lr.redaction_health()["unsecured_handlers"] == 1
    assert lr.redaction_health()["healthy"] is False


def test_f201_unsecured_handler_still_redacted_through_logger_but_leaks_directly():
    """未受保护的 handler：经 logger 仍被 L2 打码；直连才泄露。

    这条用例固定"为什么必须报 unhealthy"：健康位一旦虚报，直连路径就会
    被误认为安全。
    """
    lr.install_logger_hook()
    lr.install_format_guard()
    h = _SetLevelRaises()
    lr.install_on_handler(h)

    log = _logger_with("b412.f201b", h)
    log.error("x_api_key=%s", AK)
    via_logger = h.stream.getvalue()
    assert AK not in via_logger, "经 logger 时 L2 已把记录脱敏"
    h.stream.truncate(0)
    h.stream.seek(0)

    record = logging.LogRecord("b412.direct", logging.ERROR, __file__, 1,
                               "connect api_key=%s", (AK,), None)
    h.handle(record)          # 第三方直接驱动未受保护的 handler
    direct = h.stream.getvalue()
    assert AK in direct, "直连未受保护的 handler 会泄露 —— 这正是必须报 unhealthy 的原因"
    assert lr.redaction_health()["healthy"] is False


def test_f201_blocked_count_matches_real_output_with_mixed_handlers():
    """两个 handler：一个能禁用、一个不能 → 只报 1 个 blocked，且整体 unhealthy。"""
    good = _BlockableHandler()
    bad = _SetLevelRaises()
    assert lr.install_on_handler(good) is True
    assert lr.install_on_handler(bad) is False

    health = lr.redaction_health()
    assert health["blocked_handlers"] == 1, health
    assert health["unsecured_handlers"] == 1, health
    assert health["healthy"] is False, "单个失败不得报告全部安全"

    log = _logger_with("b412.f201c", good)
    log.error("token=%s", TK)
    assert good.stream.getvalue() == ""
    assert lr.redaction_health()["blocked_handlers"] == 1


def test_f201_verified_blocked_is_idempotent_and_not_double_counted():
    _install_all()
    h = _BlockableHandler()
    assert lr.install_on_handler(h) is True
    assert lr.install_on_handler(h) is False          # 幂等
    health = lr.redaction_health()
    assert health["blocked_handlers"] == 1, health
    assert health["healthy"] is True, health


# --------------------------------------------------------------------------- #
# AUD2-F2-02：no-op 覆盖不得被当成原实现
# --------------------------------------------------------------------------- #
def _install_all() -> None:
    lr.install_global_hook()
    lr.install_logger_hook()
    lr.install_format_guard()


def test_f202_normal_install_keeps_logging_alive():
    _install_all()
    health = lr.redaction_health()
    assert health["logger_hook"] is True
    assert health["logger_hook_trusted"] is True
    assert health["healthy"] is True

    h = _BufHandler()
    log = _logger_with("b412.f202a", h)
    log.error("token=%s", TK)
    out = h.buf.getvalue()
    assert out, "正常安装后日志必须仍有输出"
    assert TK not in out


def test_f202_logger_handle_replaced_by_noop_is_healed_not_accepted():
    _install_all()

    def _noop(self, record):
        return None

    logging.Logger.handle = _noop
    health = lr.ensure_hooks()

    assert health["repaired"]["logger"] is True, health
    assert health["logger_hook"] is True
    assert health["logger_hook_trusted"] is True, health
    assert "Logger.handle" in (health["foreign_hooks"] or {}), health

    h = _BufHandler()
    log = _logger_with("b412.f202b", h)
    log.error("token=%s", TK)
    out = h.buf.getvalue()
    assert out, "自愈后不得让日志永久静默"
    assert TK not in out


def test_f202_handler_handle_replaced_by_noop_keeps_platform_logging():
    _install_all()

    def _noop(self, record):
        return None

    logging.Handler.handle = _noop
    health = lr.ensure_hooks()
    assert health["handler_hook"] is True
    assert health["handler_hook_trusted"] is True

    h = _BufHandler()
    log = _logger_with("b412.f202c", h)
    log.error("x_api_key=%s", AK)
    out = h.buf.getvalue()
    assert out
    assert AK not in out


def test_f202_plain_third_party_function_is_detected():
    _install_all()
    calls = []

    def third_party(self, record):        # 不转发、也不打标
        calls.append(record)

    logging.Logger.handle = third_party
    health = lr.ensure_hooks()

    info = (health["foreign_hooks"] or {}).get("Logger.handle") or {}
    assert info.get("detected") is True, health
    assert info.get("installed") is True, health
    assert health["degraded"] is True, health
    assert health["logger_hook_trusted"] is True

    h = _BufHandler()
    log = _logger_with("b412.f202d", h)
    log.error("token=%s", TK)
    assert h.buf.getvalue(), "第三方替换后日志链路必须仍然可用"
    assert calls == [], "未知替换函数不应被当作链路上的原实现调用"


def test_f202_repeat_install_does_not_stack():
    _install_all()
    base = logging.Logger.handle
    assert lr.install_logger_hook() is False
    assert logging.Logger.handle is base, "重复安装不得再包一层"
    assert getattr(base, "_aitrader_original", None) is lr._PRESTINE["Logger.handle"]


def test_f202_restore_failure_is_unhealthy_not_fake_success(monkeypatch):
    """连可信原实现都拿不到时：不安装、报 unhealthy、不得虚报 logger_hook=True。"""
    _install_all()

    def _noop(self, record):
        return None

    logging.Logger.handle = _noop
    monkeypatch.setitem(lr._PRESTINE, "Logger.handle", None)
    health = lr.ensure_hooks()

    assert health["repaired"]["logger"] is False, health
    assert health["logger_hook_trusted"] is False, health
    assert health["healthy"] is False, health
    info = (health["foreign_hooks"] or {}).get("Logger.handle") or {}
    assert info.get("installed") is False, health


def test_f202_health_matches_actual_behaviour_after_heal():
    _install_all()

    def _noop(self, record):
        return None

    logging.Logger.handle = _noop
    health = lr.ensure_hooks()
    h = _BufHandler()
    log = _logger_with("b412.f202e", h)
    log.error("hello")
    assert bool(h.buf.getvalue()) is bool(health["logger_hook"])


def test_f202_uninstall_restores_trusted_original():
    _install_all()
    assert logging.Logger.handle is not lr._PRESTINE["Logger.handle"]
    result = lr.uninstall_hooks()
    assert "Logger.handle" in result["restored"], result
    assert logging.Logger.handle is lr._PRESTINE["Logger.handle"]
    assert logging.Handler.handle is lr._PRESTINE["Handler.handle"]
    assert logging.Handler.format is lr._PRESTINE["Handler.format"]


def test_f202_heal_keeps_redaction_after_noop(monkeypatch):
    """自愈后仍必须打码，而不是"恢复了输出但丢了脱敏"。"""
    _install_all()

    def _noop(self, record):
        return None

    logging.Logger.handle = _noop
    lr.ensure_hooks()
    h = _BufHandler()
    log = _logger_with("b412.f202f", h)
    log.error("payload token=%s", TK)
    out = h.buf.getvalue()
    assert out
    assert TK not in out
    assert lr._MASK in out
