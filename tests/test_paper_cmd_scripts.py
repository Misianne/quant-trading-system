"""Windows 启停脚本的真实调用测试（不是语法解析）。"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows 专属")


def _run(cmd, cwd, timeout=120):
    tmp = Path(tempfile.mkdtemp(prefix="cmdt-"))
    out, err = tmp / "o", tmp / "e"
    with open(out, "wb") as fo, open(err, "wb") as fe:
        cp = subprocess.run(cmd, cwd=str(cwd), stdout=fo, stderr=fe, timeout=timeout)
    def dec(b):
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return ""
    return cp.returncode, dec(out.read_bytes()), dec(err.read_bytes())


def test_cmd_wrappers_are_ascii_only():
    """cmd.exe 按 ANSI 读文件；非 ASCII 注释会被拆成命令导致语法错误。"""
    for name in ("paper_start.cmd", "paper_stop.cmd", "paper_status.cmd"):
        data = (ROOT / name).read_bytes()
        assert all(b < 128 for b in data), f"{name} 含非 ASCII 字节（cmd 会误读）"


def test_ps1_files_have_utf8_bom():
    """PowerShell 5.1 只有看到 BOM 才按 UTF-8 解析，否则中文会破坏语法。"""
    for name in ("paper_start.ps1", "paper_stop.ps1", "paper_status.ps1"):
        data = (ROOT / "scripts" / name).read_bytes()
        assert data.startswith(b"\xef\xbb\xbf"), f"{name} 缺少 UTF-8 BOM"


def test_status_when_idle(tmp_path):
    rc, out, err = _run(["cmd", "/c", str(ROOT / "paper_status.cmd")], tmp_path)
    assert rc in (0, 6), f"rc={rc} {out[:200]}"
    assert ("DEAD" in out) or ("NO_RUNS" in out) or ("UNKNOWN" in out)


def test_stop_when_idle(tmp_path):
    rc, out, err = _run(["cmd", "/c", str(ROOT / "paper_stop.cmd")], tmp_path)
    assert rc == 0
    assert "已停止" in out or "没有 PID" in out


def test_start_with_invalid_scenario_fails(tmp_path):
    rc, out, err = _run(["cmd", "/c", str(ROOT / "paper_start.cmd"),
                         "-Replay", "definitely_not_a_scenario"], tmp_path)
    assert rc != 0, f"启动失败却返回 0：{out[:300]}"
    assert ("未知场景" in out) or ("未知场景" in err) or rc == 2


def test_start_status_stop_roundtrip(tmp_path):
    rc, out, _ = _run(["cmd", "/c", str(ROOT / "paper_start.cmd"), "-Minutes", "5"], tmp_path)
    assert rc == 0, out[:300]
    pid_file = ROOT / "data" / "paper_runs" / "supervisor.pid"
    for _ in range(150):
        if pid_file.exists():
            break
        time.sleep(0.2)
    assert pid_file.exists(), "启动后未写 PID 文件"

    rc2, out2, _ = _run(["cmd", "/c", str(ROOT / "paper_status.cmd")], tmp_path)
    assert rc2 == 0 and "RUNNING" in out2

    rc3, out3, _ = _run(["cmd", "/c", str(ROOT / "paper_stop.cmd")], tmp_path)
    assert rc3 == 0, out3[:300]
