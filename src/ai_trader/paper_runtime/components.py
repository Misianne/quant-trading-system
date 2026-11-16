"""Paper 运行组件：依赖拓扑、健康检查、逆序释放。

四个必需组件（US-P2 的四项能力）
--------------------------------
======================  ==================  ==============================
组件                    对应的 P2 能力      缺失后果
======================  ==================  ==============================
``market_status``       市场状态 / LULD     停牌、Limit State、竞价全失效
``short_guard``         卖空 / 借券 / SSR   可能裸卖空
``settlement``          T+1 结算 / 购买力   未结算资金被当成可用
``caps``                订单类型 / TIF      不支持的订单被送出去
======================  ==================  ==============================

设计要点
--------
* **拓扑顺序**初始化：依赖没就绪就不允许初始化下游（不是"尽力而为"）。
* **逆序释放**：与初始化严格相反，先关依赖者再关被依赖者。
* **不允许半启动**：任一必需组件 ``start`` 失败 → 已启动的全部逆序回滚 →
  整体 FAILED。不存在"先跑起来再说"。
* **关闭失败必须记录**：``stop`` 抛错会被收进 ``errors``，不被吞掉，
  也不会让注册表谎报"已干净停止"。
* **幂等**：重复 ``start_all`` / ``stop_all`` 不重复调用组件的 start/stop。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("ai_trader.paper.components")


class ComponentError(RuntimeError):
    pass


class DependencyError(ComponentError):
    """缺依赖 / 循环依赖 / 重名注册。"""


@dataclass
class ComponentHealth:
    name: str
    healthy: bool
    detail: str = ""
    critical: bool = True
    checked_at: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "healthy": self.healthy, "detail": self.detail,
                "critical": self.critical, "checked_at": self.checked_at,
                "metrics": dict(self.metrics)}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Component:
    """组件基类。子类只需实现 :meth:`_start` / :meth:`_stop` / :meth:`_health`。"""

    #: 组件名（唯一）
    name: str = "component"
    #: 依赖的组件名
    depends_on: Tuple[str, ...] = ()
    #: 关键组件：失效 → HALTED；非关键：失效 → DEGRADED
    critical: bool = True

    def __init__(self, **kwargs: Any):
        self.started = False
        self.start_count = 0
        self.stop_count = 0
        self.last_error = ""
        self._lock = threading.RLock()

    # -- 生命周期 ------------------------------------------------------- #
    def start(self, ctx: Any = None) -> None:
        with self._lock:
            if self.started:
                return                     # 幂等：重复 start 不重复订阅
            self._start(ctx)
            self.started = True
            self.start_count += 1

    def stop(self, ctx: Any = None) -> None:
        with self._lock:
            if not self.started:
                return                     # 幂等：没启动就没有可释放的
            try:
                self._stop(ctx)
            finally:
                # 即使 _stop 抛错也要标记为已停止，避免下次"以为还在跑"
                self.started = False
                self.stop_count += 1

    def health(self) -> ComponentHealth:
        try:
            h = self._health()
        except Exception as exc:  # noqa: BLE001 - 健康检查本身失败 = 不健康
            return ComponentHealth(self.name, False,
                                   f"健康检查异常：{type(exc).__name__}: {exc}",
                                   self.critical, _now_iso())
        if isinstance(h, ComponentHealth):
            h.name = self.name
            h.critical = self.critical
            h.checked_at = h.checked_at or _now_iso()
            return h
        ok, detail = (h if isinstance(h, tuple) else (bool(h), ""))
        return ComponentHealth(self.name, bool(ok), str(detail or ""),
                               self.critical, _now_iso())

    # -- 子类实现 ------------------------------------------------------- #
    def _start(self, ctx: Any) -> None:      # pragma: no cover - 默认无操作
        return None

    def _stop(self, ctx: Any) -> None:       # pragma: no cover
        return None

    def _health(self):                       # pragma: no cover
        return True, ""

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "started": self.started,
                "critical": self.critical, "depends_on": list(self.depends_on),
                "start_count": self.start_count, "stop_count": self.stop_count,
                "last_error": self.last_error}


@dataclass
class RegistryResult:
    ok: bool
    started: List[str] = field(default_factory=list)
    failed: str = ""
    rolled_back: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "started": list(self.started), "failed": self.failed,
                "rolled_back": list(self.rolled_back), "errors": list(self.errors)}


class ComponentRegistry:
    """组件注册表 + 依赖拓扑 + 健康聚合。"""

    def __init__(self) -> None:
        self._comps: Dict[str, Component] = {}
        self._order: List[str] = []
        self._started_order: List[str] = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    def register(self, comp: Component) -> Component:
        with self._lock:
            if comp.name in self._comps:
                raise DependencyError(f"组件重名：{comp.name}")
            self._comps[comp.name] = comp
            self._order = []          # 失效缓存，下次重算
            return comp

    def get(self, name: str) -> Optional[Component]:
        return self._comps.get(name)

    def names(self) -> List[str]:
        return list(self._comps)

    # ------------------------------------------------------------------ #
    def order(self) -> List[str]:
        """拓扑序（依赖在前）。缺依赖 / 循环依赖直接抛错。"""
        with self._lock:
            if self._order:
                return list(self._order)
            missing = [(n, d) for n, c in self._comps.items()
                       for d in c.depends_on if d not in self._comps]
            if missing:
                raise DependencyError("缺少依赖：" + "; ".join(
                    f"{n} 需要 {d}" for n, d in missing))

            perm: Dict[str, int] = {}     # 0=未访问 1=访问中 2=已排序
            out: List[str] = []

            def visit(n: str, stack: List[str]) -> None:
                st = perm.get(n, 0)
                if st == 2:
                    return
                if st == 1:
                    raise DependencyError("循环依赖：" + " → ".join(stack + [n]))
                perm[n] = 1
                for d in self._comps[n].depends_on:
                    visit(d, stack + [n])
                perm[n] = 2
                out.append(n)

            for n in self._comps:
                visit(n, [])
            self._order = out
            return list(out)

    def check_required(self, required: Iterable[str]) -> List[str]:
        """返回缺失的必需组件名（空列表 = 齐全）。"""
        return [n for n in required if n not in self._comps]

    # ------------------------------------------------------------------ #
    def start_all(self, ctx: Any = None) -> RegistryResult:
        """按拓扑序启动。任一失败 → 逆序回滚已启动的，整体失败。"""
        res = RegistryResult(ok=False)
        try:
            order = self.order()
        except DependencyError as exc:
            res.failed = str(exc)
            return res

        for name in order:
            comp = self._comps[name]
            try:
                comp.start(ctx)
                res.started.append(name)
                with self._lock:
                    if name not in self._started_order:
                        self._started_order.append(name)
            except Exception as exc:  # noqa: BLE001
                comp.last_error = f"{type(exc).__name__}: {exc}"
                res.failed = f"{name}: {comp.last_error}"
                logger.error("组件启动失败 %s：%s", name, comp.last_error)
                # 逆序回滚
                for done in reversed(res.started):
                    try:
                        self._comps[done].stop(ctx)
                        res.rolled_back.append(done)
                    except Exception as sexc:  # noqa: BLE001
                        msg = f"回滚 {done} 失败：{type(sexc).__name__}: {sexc}"
                        res.errors.append(msg)
                        logger.error(msg)
                return res
        res.ok = True
        return res

    def stop_all(self, ctx: Any = None) -> RegistryResult:
        """**逆序**释放。关闭失败逐条记录，不假装成功。"""
        res = RegistryResult(ok=True)
        with self._lock:
            started = list(self._started_order)
        for name in reversed(started):
            comp = self._comps[name]
            try:
                comp.stop(ctx)
                with self._lock:
                    if name in self._started_order:
                        self._started_order.remove(name)
            except Exception as exc:  # noqa: BLE001
                comp.last_error = f"{type(exc).__name__}: {exc}"
                res.ok = False
                msg = f"关闭 {name} 失败：{comp.last_error}"
                res.errors.append(msg)
                logger.error(msg)
        return res

    # ------------------------------------------------------------------ #
    def health_all(self) -> Dict[str, ComponentHealth]:
        return {n: c.health() for n, c in self._comps.items()}

    def aggregate(self) -> Tuple[str, List[str]]:
        """聚合健康：返回 ``("OK"|"DEGRADED"|"HALTED", 原因列表)``。"""
        reasons: List[str] = []
        worst = "OK"
        for name, h in self.health_all().items():
            if h.healthy:
                continue
            reasons.append(f"{name}: {h.detail or 'unhealthy'}")
            if h.critical:
                worst = "HALTED"
            elif worst == "OK":
                worst = "DEGRADED"
        # 未启动的组件也算不健康（不能"没启动但报告健康"）
        for name, comp in self._comps.items():
            if not comp.started:
                reasons.append(f"{name}: 未启动")
                if comp.critical:
                    worst = "HALTED"
                elif worst == "OK":
                    worst = "DEGRADED"
        return worst, reasons

    def as_dict(self) -> Dict[str, Any]:
        return {"order": self.order(), "components": {n: c.as_dict()
                                                      for n, c in self._comps.items()},
                "health": {n: h.as_dict() for n, h in self.health_all().items()}}
