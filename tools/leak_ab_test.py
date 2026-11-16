"""A/B 对照：旧实现（函数内定义 Structure）vs 新实现（模块级缓存）。"""
import ctypes, gc, sys
from ctypes import wintypes


def old_rss():
    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetCurrentProcess.argtypes = []
    fn = k32.K32GetProcessMemoryInfo
    fn.restype = wintypes.BOOL
    fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    c = PMC(); c.cb = ctypes.sizeof(c)
    fn(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.WorkingSetSize / 1048576


def types_count():
    return sum(1 for o in gc.get_objects() if isinstance(o, type) and issubclass(o, ctypes.Structure))


sys.path.insert(0, "src")
from ai_trader.paper_runtime.supervisor import process_rss_mb as new_rss

gc.collect(); base = types_count()
for _ in range(500):
    old_rss()
gc.collect()
print("old_impl_type_delta", types_count() - base)

gc.collect(); base2 = types_count()
before_rss = new_rss()
for _ in range(3000):
    new_rss()
gc.collect()
print("new_impl_type_delta", types_count() - base2, "rss_delta", round(new_rss() - before_rss, 2))
