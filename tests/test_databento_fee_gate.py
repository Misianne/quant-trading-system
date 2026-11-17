"""Databento 费用门禁测试（完全离线）。

不能真调 Databento（没有凭据，也不允许花钱），但门禁逻辑必须被验证：
"估价超上限就不取数"这件事如果只写在文档里，等于没写。

做法：把 HTTP 层替换成可控桩，直接驱动 ``tools/databento_probe.py`` 的
``main()``，检查退出码与是否真的发出了 ``timeseries.get_range``。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "databento_probe", ROOT / "tools" / "databento_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


probe = _load_probe()

RANGE = ["--start", "2026-11-16T14:30:00Z", "--end", "2026-11-16T14:35:00Z"]


class FakeHTTP:
    """记录调用序列的 HTTP 桩。"""

    def __init__(self, cost_payload, range_payload=b'{"rtype":18}\n'):
        self.cost_payload = cost_payload
        self.range_payload = range_payload
        self.calls = []

    def __call__(self, url, key, timeout=30.0):
        self.calls.append(url)
        if "metadata.get_cost" in url:
            if isinstance(self.cost_payload, bytes):
                return self.cost_payload
            return json.dumps(self.cost_payload).encode()
        if "timeseries.get_range" in url:
            return self.range_payload
        raise AssertionError(f"未预期的 URL：{url}")

    def count(self, needle):
        return sum(1 for c in self.calls if needle in c)


@pytest.fixture
def monkey(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABENTO_API_KEY", "db-test-key-not-real-000000")
    return monkeypatch, tmp_path


# ---------------------------------------------------------------------- #
# 凭据
# ---------------------------------------------------------------------- #
def test_missing_credential_blocks_before_any_network(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(probe, "_http_get", lambda *a, **k: called.append(a) or b"{}")
    assert probe.main([*RANGE, "--out", str(tmp_path)]) == 3
    assert called == [], "无凭据时不得发起任何网络请求"
    d = json.loads((tmp_path / "probe-blocked.json").read_text(encoding="utf-8"))
    assert d["blocked"] == "NO_CREDENTIAL"


def test_missing_range_blocks(monkey, tmp_path):
    monkeypatch, tmp = monkey
    monkeypatch.setattr(probe, "_http_get", FakeHTTP({"cost": 0.0}))
    assert probe.main(["--out", str(tmp)]) == 2


# ---------------------------------------------------------------------- #
# 成本门禁
# ---------------------------------------------------------------------- #
def test_cost_above_cap_blocks_without_fetching(monkey, tmp_path):
    monkeypatch, tmp = monkey
    http = FakeHTTP({"cost": 0.42})
    monkeypatch.setattr(probe, "_http_get", http)
    assert probe.main([*RANGE, "--out", str(tmp), "--confirm"]) == 5
    assert http.count("timeseries.get_range") == 0, "超预算仍然取数 = 违约"
    assert http.count("metadata.get_cost") == 1


def test_default_cap_is_zero(monkey, tmp_path):
    """默认上限 0 美元：任何正成本都必须被拦。"""
    monkeypatch, tmp = monkey
    monkeypatch.setattr(probe, "_http_get", FakeHTTP({"cost": 0.0000001}))
    assert probe.main([*RANGE, "--out", str(tmp), "--confirm"]) == 5


def test_zero_cost_allowed_with_confirm(monkey, tmp_path):
    monkeypatch, tmp = monkey
    http = FakeHTTP({"cost": 0.0})
    monkeypatch.setattr(probe, "_http_get", http)
    assert probe.main([*RANGE, "--out", str(tmp), "--confirm"]) == 0
    assert http.count("timeseries.get_range") == 1


def test_requires_confirm_even_when_free(monkey, tmp_path):
    monkeypatch, tmp = monkey
    http = FakeHTTP({"cost": 0.0})
    monkeypatch.setattr(probe, "_http_get", http)
    assert probe.main([*RANGE, "--out", str(tmp)]) == 6
    assert http.count("timeseries.get_range") == 0


def test_cost_unparseable_blocks(monkey, tmp_path):
    monkeypatch, tmp = monkey
    monkeypatch.setattr(probe, "_http_get", FakeHTTP(b"<html>oops</html>"))
    assert probe.main([*RANGE, "--out", str(tmp), "--confirm"]) == 4


def test_cost_missing_field_blocks(monkey, tmp_path):
    monkeypatch, tmp = monkey
    monkeypatch.setattr(probe, "_http_get", FakeHTTP({"result": "ok"}))
    assert probe.main([*RANGE, "--out", str(tmp), "--confirm"]) == 4


def test_cost_non_numeric_blocks(monkey, tmp_path):
    monkeypatch, tmp = monkey
    monkeypatch.setattr(probe, "_http_get", FakeHTTP({"cost": "cheap"}))
    assert probe.main([*RANGE, "--out", str(tmp), "--confirm"]) == 4


# ---------------------------------------------------------------------- #
# 取数后的证据
# ---------------------------------------------------------------------- #
def test_fetch_writes_raw_and_digest(monkey, tmp_path):
    monkeypatch, tmp = monkey
    payload = b'{"rtype":18,"action":8}\n{"rtype":0,"price":1}\n'
    monkeypatch.setattr(probe, "_http_get", FakeHTTP({"cost": 0.0}, payload))
    assert probe.main([*RANGE, "--out", str(tmp), "--confirm"]) == 0
    files = list(Path(tmp).glob("*.jsonl"))
    assert len(files) == 1 and files[0].read_bytes() == payload
    meta = json.loads(next(Path(tmp).glob("*.meta.json")).read_text(encoding="utf-8"))
    assert meta["sha256"] == __import__("hashlib").sha256(payload).hexdigest()
    assert meta["rows"] == 2
    assert meta["field_presence"]["rtype"] == 2


# ---------------------------------------------------------------------- #
# 密钥泄漏
# ---------------------------------------------------------------------- #
def test_key_never_in_output_or_files(monkey, tmp_path, capsys):
    monkeypatch, tmp = monkey
    secret = "db-SUPERSECRET-abcdef123456"
    monkeypatch.setenv("DATABENTO_API_KEY", secret)
    monkeypatch.setattr(probe, "_http_get", FakeHTTP({"cost": 99.0}))
    probe.main([*RANGE, "--out", str(tmp), "--confirm"])
    captured = capsys.readouterr()
    assert secret not in captured.out and secret not in captured.err
    for f in Path(tmp).rglob("*"):
        if f.is_file():
            assert secret not in f.read_text(encoding="utf-8", errors="replace")


def test_key_not_accepted_from_argv(monkey, tmp_path):
    """命令行传 key 不被支持 —— 会出现在进程列表和 shell 历史里。

    工具根本没有 ``--api-key`` 这个参数，argparse 直接拒绝（exit 2）。
    """
    monkeypatch, tmp = monkey
    with pytest.raises(SystemExit) as exc:
        probe.main([*RANGE, "--out", str(tmp), "--api-key", "db-x"])
    assert exc.value.code == 2
