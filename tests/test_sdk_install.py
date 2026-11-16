"""测试 1：SDK 安装与接口可发现性。"""
from __future__ import annotations

import importlib
import inspect

import pytest


def test_sdk_importable():
    fsopenapi = pytest.importorskip("fsopenapi")
    assert fsopenapi.__file__
    assert hasattr(fsopenapi, "SDKClient")


def test_sdk_exports_expected_api_classes():
    fsopenapi = importlib.import_module("fsopenapi")
    for name in ("SDKClient", "AccountAPI", "PortfolioAPI", "TradeAPI", "MarketAPI",
                 "SessionAPI", "APIError", "AuthenticationError"):
        assert hasattr(fsopenapi, name), f"SDK 缺少导出 {name}"


def test_trade_api_has_required_methods():
    fsopenapi = importlib.import_module("fsopenapi")
    required = ["create_order", "cancel_order", "order_modify", "list_orders",
                "get_order_detail", "get_cash_flows", "get_bid_ask_info"]
    for m in required:
        assert callable(getattr(fsopenapi.TradeAPI, m, None)), f"TradeAPI 缺少 {m}"


def test_market_api_has_required_methods():
    fsopenapi = importlib.import_module("fsopenapi")
    for m in ("quote", "kline", "min", "orderbook", "tick", "broker_list"):
        assert callable(getattr(fsopenapi.MarketAPI, m, None)), f"MarketAPI 缺少 {m}"


def test_sdk_client_requires_pem_env(monkeypatch):
    """缺 PEM 环境变量时必须抛错 —— 证明它不会"假装连上"。"""
    fsopenapi = importlib.import_module("fsopenapi")
    monkeypatch.delenv("FSOPENAPI_SERVER_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("FSOPENAPI_CLIENT_PRIVATE_KEY", raising=False)
    with pytest.raises(ValueError) as ei:
        fsopenapi.SDKClient("https://example.invalid", "k")
    assert "FSOPENAPI_SERVER_PUBLIC_KEY" in str(ei.value)


def test_sdk_client_requires_base_url_and_key(monkeypatch):
    fsopenapi = importlib.import_module("fsopenapi")
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", "x")
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", "y")
    with pytest.raises(ValueError):
        fsopenapi.SDKClient("", "k")


def test_create_order_signature_documents_market_and_currency():
    """下单接口必须能表达市场与币种（港股/美股/A股通）。"""
    fsopenapi = importlib.import_module("fsopenapi")
    sig = inspect.signature(fsopenapi.TradeAPI.create_order)
    params = sig.parameters
    for p in ("sub_account_id", "stock_code", "direction", "order_type",
              "quantity", "price", "market_code", "currency",
              "stop_loss_trig_price", "profit_trig_price"):
        assert p in params, f"create_order 缺少参数 {p}"
    assert params["market_code"].default == "hk"
    assert params["currency"].default == "HKD"
