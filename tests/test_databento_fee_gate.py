"""Databento 费用探针的三档模式审计测试（完全离线）。

核心不是"日志里没写联网"，而是**结构上联不了网**：

* A 档（``--estimate-only``）不读凭据、不构造 ``HttpClient``、不发请求；
* 用 socket 层拦截（``getaddrinfo`` / ``socket`` / ``create_connection`` /
  ``ssl.wrap_socket``）证明 DNS 与 TCP 一次都没发生；
* B/C 档的联网能力用可控桩驱动，断言"该拦的拦住了"。

上一版有一个真实漏洞：``--max-cost-usd nan`` 会让 ``cost > cap`` 恒为 False，
直接绕过费用上限。这里用 ``test_nan_cap_cannot_bypass`` 钉住它。
"""
from __future__ import annotations

import importlib.util
import json
import socket
import ssl
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tools" / "databento_probe.py"

RANGE = ["--start", "2026-11-16T14:30:00Z", "--end", "2026-11-16T14:35:00Z"]
SECRET = "db-SUPERSECRET-abcdef123456"


def _load(path: Path, name: str):
    """按文件加载模块。

    必须先注册到 ``sys.modules``：``@dataclass`` 内部会用
    ``sys.modules[cls.__module__].__dict__`` 解析类型注解，
    未注册时模块是 ``None``，直接抛 AttributeError。
    """
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


probe = _load(PROBE, "databento_probe")


@pytest.fixture(autouse=True)
def _reset_gate():
    probe._NETWORK_ENABLED = False
    yield
    probe._NETWORK_ENABLED = False


# ---------------------------------------------------------------------- #
# 网络拦截器：证明"没有发生"，而不是"没有记录"
# ---------------------------------------------------------------------- #
class NetSpy:
    """把 DNS/TCP/TLS 的入口全部换成计数+抛错。"""

    def __init__(self, monkeypatch):
        self.calls = []
        self._mp = monkeypatch

    def install(self):
        def deny(name):
            def _fn(*a, **k):
                self.calls.append(name)
                raise AssertionError(f"禁止的网络调用发生了：{name}")
            return _fn

        self._mp.setattr(socket, "getaddrinfo", deny("getaddrinfo"))
        self._mp.setattr(socket, "socket", deny("socket"))
        self._mp.setattr(socket, "create_connection", deny("create_connection"))
        self._mp.setattr(socket, "socketpair", deny("socketpair"))
        self._mp.setattr(ssl.SSLContext, "wrap_socket", deny("ssl.wrap_socket"))
        return self


@pytest.fixture
def netspy(monkeypatch):
    return NetSpy(monkeypatch).install()


class ForbiddenHTTP:
    """任何 HTTP 调用都记一笔并炸掉。"""

    def __init__(self):
        self.urls = []

    def __call__(self, url, *a, **k):
        self.urls.append(url)
        raise AssertionError(f"禁止的 HTTP 请求：{url}")


@pytest.fixture
def no_http(monkeypatch):
    spy = ForbiddenHTTP()
    monkeypatch.setattr(probe, "_http_get", spy)
    monkeypatch.setattr(probe.urllib.request, "urlopen", spy)
    return spy


def price_book(tmp_path: Path, *, verified=True, price=12.5,
               dataset="XNAS.ITCH", unit="per_gb", source="synthetic fixture") -> Path:
    data = {"dataset": dataset, "unit": unit, "unit_price_usd": price,
            "verified": verified, "source": source,
            "verified_at": "2026-11-17"}
    p = tmp_path / "book.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------- #
# 1/2. 估价：无凭据 / 有凭据，都必须零网络、不读凭据
# ---------------------------------------------------------------------- #
def test_estimate_without_credential_is_local_only(tmp_path, monkeypatch,
                                                   netspy, no_http):
    monkeypatch.delenv(probe.KEY_ENV, raising=False)
    rc = probe.main(["--estimate-only", "--price-book",
                     str(price_book(tmp_path)), "--expected-gb", "0.05",
                     *RANGE, "--out", str(tmp_path)])
    assert rc == 0
    assert netspy.calls == [] and no_http.urls == []


def test_estimate_with_credential_still_does_not_read_it(tmp_path, monkeypatch,
                                                         netspy, no_http):
    """A 档即使环境里**有**凭据也不能读 —— 读了就等于给了联网的可能。"""
    monkeypatch.setenv(probe.KEY_ENV, SECRET)
    called = []
    monkeypatch.setattr(probe, "_key", lambda: called.append(1) or SECRET)
    rc = probe.main(["--estimate-only", "--price-book",
                     str(price_book(tmp_path)), "--expected-gb", "0.05",
                     *RANGE, "--out", str(tmp_path)])
    assert rc == 0
    assert called == [], "estimate-only 不得读取凭据"
    assert netspy.calls == [] and no_http.urls == []


def test_estimate_reports_zero_network_in_evidence(tmp_path, monkeypatch):
    monkeypatch.delenv(probe.KEY_ENV, raising=False)
    probe.main(["--estimate-only", "--price-book",
                str(price_book(tmp_path)), "--expected-gb", "0.5",
                *RANGE, "--out", str(tmp_path)])
    ev = json.loads((tmp_path / "probe-estimate.json").read_text(encoding="utf-8"))
    assert ev["network"] is False
    assert ev["network_calls"] == 0
    assert ev["credential"] == "not_read"
    assert ev["estimated_cost_usd"] == pytest.approx(6.25)


# ---------------------------------------------------------------------- #
# 3/4/5. 客户端构造、HTTP、DNS/TCP 全被禁止
# ---------------------------------------------------------------------- #
def test_http_client_cannot_be_constructed_when_gate_closed():
    probe._NETWORK_ENABLED = False
    with pytest.raises(probe.NetworkForbidden) as exc:
        probe.HttpClient("db-key-123456")
    assert exc.value.exit_code == 2


def test_estimate_mode_never_constructs_client(tmp_path, monkeypatch, netspy):
    built = []

    class Boom:
        def __init__(self, *a, **k):
            built.append(1)
            raise AssertionError("estimate-only 不得构造 HttpClient")

    monkeypatch.setattr(probe, "HttpClient", Boom)
    rc = probe.main(["--estimate-only", "--price-book",
                     str(price_book(tmp_path)), "--expected-gb", "0.01",
                     *RANGE, "--out", str(tmp_path)])
    assert rc == 0 and built == []


def test_estimate_mode_does_no_dns_or_tcp(tmp_path, monkeypatch, netspy):
    monkeypatch.delenv(probe.KEY_ENV, raising=False)
    probe.main(["--estimate-only", "--price-book",
                str(price_book(tmp_path)), "--expected-gb", "1",
                *RANGE, "--out", str(tmp_path)])
    assert netspy.calls == [], f"发生了网络调用：{netspy.calls}"


# ---------------------------------------------------------------------- #
# 6. 未知价格 → UNKNOWN_COST
# ---------------------------------------------------------------------- #
def test_no_price_book_is_unknown_cost(tmp_path, monkeypatch, netspy):
    monkeypatch.delenv(probe.KEY_ENV, raising=False)
    rc = probe.main(["--estimate-only", *RANGE, "--out", str(tmp_path)])
    assert rc == 7
    ev = json.loads((tmp_path / "probe-blocked.json").read_text(encoding="utf-8"))
    assert ev["blocked"] == "UNKNOWN_COST"
    assert netspy.calls == []


@pytest.mark.parametrize("kwargs", [
    {"verified": False},
    {"source": ""},
    {"price": 0.0},
    {"price": -1.0},
    {"unit": "per_furlong"},
    {"dataset": "XNYS.PILLAR"},
])
def test_unverifiable_price_book_is_unknown_cost(tmp_path, monkeypatch, kwargs):
    monkeypatch.delenv(probe.KEY_ENV, raising=False)
    rc = probe.main(["--estimate-only", "--price-book",
                     str(price_book(tmp_path, **kwargs)), "--expected-gb", "0.1",
                     *RANGE, "--out", str(tmp_path)])
    assert rc == 7, f"{kwargs} 应被判为无法核实"


def test_missing_expected_gb_is_unknown_cost(tmp_path, monkeypatch):
    monkeypatch.delenv(probe.KEY_ENV, raising=False)
    rc = probe.main(["--estimate-only", "--price-book", str(price_book(tmp_path)),
                     *RANGE, "--out", str(tmp_path)])
    assert rc == 7


# ---------------------------------------------------------------------- #
# 7/8. 费用上限
# ---------------------------------------------------------------------- #
class FakeClient:
    """可控 HttpClient：记录调用，按 URL 返回估价或数据。"""

    instances = []
    next_cost = 0.0

    def __init__(self, key, *, allowed=None, timeout=30.0):
        enabled = probe.network_enabled() if allowed is None else bool(allowed)
        if not enabled:
            raise probe.NetworkForbidden("闸门关闭")
        self.key = key
        self.requests = 0
        self.urls = []
        FakeClient.instances.append(self)

    def get(self, url):
        if "metadata.get_cost" in url:
            self.requests += 1
            self.urls.append(url)
            return json.dumps({"cost": FakeClient.next_cost}).encode()
        if "timeseries.get_range" in url:
            self.requests += 1
            self.urls.append(url)
            return b'{"rtype":18}\n'
        raise AssertionError(url)


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.instances = []
    FakeClient.next_cost = 0.0
    monkeypatch.setattr(probe, "HttpClient", FakeClient)
    monkeypatch.setenv(probe.KEY_ENV, SECRET)
    return FakeClient


def test_fetch_refused_when_cap_is_zero(tmp_path, fake_client):
    rc = probe.main(["--fetch", "--confirm", *RANGE, "--out", str(tmp_path)])
    assert rc == 5
    assert fake_client.instances == [], "未授权时不应构造客户端"


def test_fetch_refused_when_cost_exceeds_cap(tmp_path, fake_client):
    fake_client.next_cost = 0.5
    rc = probe.main(["--fetch", "--confirm", "--max-cost-usd", "0.1",
                     *RANGE, "--out", str(tmp_path)])
    assert rc == 5
    cli = fake_client.instances[0]
    assert not any("get_range" in u for u in cli.urls), "超预算仍然取数 = 违约"


def test_fetch_requires_confirm(tmp_path, fake_client):
    fake_client.next_cost = 0.0
    rc = probe.main(["--fetch", "--max-cost-usd", "1",
                     *RANGE, "--out", str(tmp_path)])
    assert rc == 6
    assert fake_client.instances == []


def test_fetch_succeeds_within_cap(tmp_path, fake_client):
    fake_client.next_cost = 0.0002
    rc = probe.main(["--fetch", "--confirm", "--max-cost-usd", "0.01",
                     *RANGE, "--out", str(tmp_path)])
    assert rc == 0
    cli = fake_client.instances[0]
    assert sum("get_range" in u for u in cli.urls) == 1
    assert list(tmp_path.glob("*.jsonl"))


def test_nan_cap_cannot_bypass(tmp_path, fake_client):
    """上一版的真实漏洞：``cost > nan`` 恒为 False，上限形同虚设。"""
    fake_client.next_cost = 99.0
    rc = probe.main(["--fetch", "--confirm", "--max-cost-usd", "nan",
                     *RANGE, "--out", str(tmp_path)])
    assert rc == 2
    assert fake_client.instances == []


@pytest.mark.parametrize("cap", ["inf", "-1", "-0.5"])
def test_invalid_cap_rejected(tmp_path, fake_client, cap):
    rc = probe.main(["--fetch", "--confirm", "--max-cost-usd", cap,
                     *RANGE, "--out", str(tmp_path)])
    assert rc == 2


def test_cap_above_hard_limit_rejected(tmp_path, fake_client):
    rc = probe.main(["--fetch", "--confirm", "--max-cost-usd",
                     str(probe.HARD_CAP_USD + 1), *RANGE, "--out", str(tmp_path)])
    assert rc == 2


def test_env_var_cannot_raise_cap(tmp_path, fake_client, monkeypatch):
    """环境变量一律忽略 —— 只有命令行能授权花费，且默认 0。"""
    fake_client.next_cost = 0.1
    monkeypatch.setenv("DATABENTO_MAX_COST_USD", "999")
    monkeypatch.setenv("MAX_COST_USD", "999")
    rc = probe.main(["--fetch", "--confirm", *RANGE, "--out", str(tmp_path)])
    assert rc == 5, "环境变量把上限抬起来了"
    assert fake_client.instances == []


# ---------------------------------------------------------------------- #
# 9. 估价模式不能切到下载
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("argv,mode", [
    (["--estimate-only", "--confirm"], "return"),
    (["--estimate-only", "--max-cost-usd", "5"], "return"),
    # 互斥由 argparse 保证：两个模式同时给会直接 exit 2
    (["--estimate-only", "--fetch"], "exit"),
    (["--estimate-only", "--estimate-online"], "exit"),
])
def test_estimate_mode_cannot_escalate(tmp_path, monkeypatch, argv, mode):
    monkeypatch.delenv(probe.KEY_ENV, raising=False)
    if mode == "exit":
        with pytest.raises(SystemExit) as exc:
            probe.main([*argv, *RANGE, "--out", str(tmp_path)])
        assert exc.value.code == 2
        return
    assert probe.main([*argv, *RANGE, "--out", str(tmp_path)]) == 2


# ---------------------------------------------------------------------- #
# B 档：只查价，不取数
# ---------------------------------------------------------------------- #
def test_online_estimate_requires_credential(tmp_path, monkeypatch, netspy):
    monkeypatch.delenv(probe.KEY_ENV, raising=False)
    rc = probe.main(["--estimate-online", *RANGE, "--out", str(tmp_path)])
    assert rc == 3
    assert netspy.calls == []


def test_online_estimate_does_not_fetch_data(tmp_path, fake_client):
    fake_client.next_cost = 0.3
    rc = probe.main(["--estimate-online", *RANGE, "--out", str(tmp_path)])
    assert rc == 0
    cli = fake_client.instances[0]
    assert not any("get_range" in u for u in cli.urls), "B 档不得请求历史数据"
    ev = json.loads((tmp_path / "probe-estimate-online.json").read_text(encoding="utf-8"))
    assert ev["within_cap"] is False
    assert ev.get("data_requested") is None


# ---------------------------------------------------------------------- #
# 10. 凭据不泄漏
# ---------------------------------------------------------------------- #
def test_secret_never_printed_or_written(tmp_path, fake_client, capsys):
    fake_client.next_cost = 0.5
    probe.main(["--fetch", "--confirm", "--max-cost-usd", "0.1",
                *RANGE, "--out", str(tmp_path)])
    cap = capsys.readouterr()
    assert SECRET not in cap.out and SECRET not in cap.err
    for f in Path(tmp_path).rglob("*"):
        if f.is_file():
            assert SECRET not in f.read_text(encoding="utf-8", errors="replace")


def test_api_key_argument_not_supported(tmp_path):
    with pytest.raises(SystemExit) as exc:
        probe.main(["--api-key", "db-x", *RANGE, "--out", str(tmp_path)])
    assert exc.value.code == 2


# ---------------------------------------------------------------------- #
# 其他
# ---------------------------------------------------------------------- #
def test_missing_range_is_usage_error(tmp_path, monkeypatch):
    monkeypatch.delenv(probe.KEY_ENV, raising=False)
    assert probe.main(["--estimate-only", "--out", str(tmp_path)]) == 2


@pytest.mark.parametrize("limit", ["0", "-5", "99999"])
def test_limit_bounds(tmp_path, monkeypatch, limit):
    monkeypatch.delenv(probe.KEY_ENV, raising=False)
    rc = probe.main(["--estimate-only", "--price-book",
                     str(price_book(tmp_path)), "--expected-gb", "0.1",
                     "--limit", limit, *RANGE, "--out", str(tmp_path)])
    assert rc == 2


def test_network_gate_is_closed_after_each_run(tmp_path, fake_client):
    fake_client.next_cost = 0.0
    probe.main(["--estimate-online", *RANGE, "--out", str(tmp_path)])
    assert probe.network_enabled() is False, "运行结束后闸门必须回到关闭"
