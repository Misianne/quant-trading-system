"""进程级故障注入。

为什么必须"真的杀进程"
----------------------
Python 异常只会回滚到最近的 ``except``，磁盘上可能已经写了一半。
真正的崩溃是 ``SIGKILL`` 级别的：没有栈展开、没有 finally、没有 flush。
只有用 :func:`os._exit` 模拟，才能验证"重启后账还对不对"。

用法::

    PAPER_CRASH_AT=after_fill_write python -m ai_trader.paper_runtime --paper ...

未设置环境变量时 :func:`check` 立即返回，零开销、零行为改变。
"""
from __future__ import annotations

import os
import sys

#: 允许的注入点（白名单，防止拼错导致"永不崩溃"的假通过）
POINTS = (
    "before_order",          # 下单之前
    "after_order_create",    # 订单创建并落盘之后
    "after_risk",            # 风控通过之后
    "before_fill_write",     # 成交写入之前
    "after_fill_write",      # 成交写入并落盘之后
    "after_position",        # 持仓更新之后
    "after_settlement",      # 结算更新之后
)

EXIT_CODE = 9


def armed() -> str:
    return str(os.getenv("PAPER_CRASH_AT", "")).strip()


def validate() -> str:
    """启动时校验注入点名称；返回错误描述（空串 = 正常）。"""
    want = armed()
    if not want:
        return ""
    if want not in POINTS:
        return f"未知注入点 {want!r}；可用：{', '.join(POINTS)}"
    return ""


def check(point: str) -> None:
    """到达注入点且匹配 → 立即终止进程（不执行任何清理）。"""
    want = armed()
    if not want or want != point:
        return
    if want not in POINTS:          # 配置错误也要能被发现
        sys.stderr.write(f"[fault-inject] 未知注入点 {want!r}\n")
        sys.stderr.flush()
        os._exit(EXIT_CODE)
    sys.stderr.write(f"[fault-inject] crash at {point} (pid={os.getpid()})\n")
    sys.stderr.flush()
    # 先尽量把已缓存的日志刷掉，再硬退出（模拟断电，不做业务清理）
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(EXIT_CODE)
