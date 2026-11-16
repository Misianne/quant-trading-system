"""测试：命令行入口的参数解析与门禁输出。"""
from __future__ import annotations

import json

import pytest

from ai_trader import cli


def test_all_subcommands_parse():
    p = cli.build_parser()
    cases = [
        ["status"], ["check-env"], ["scan"], ["scan", "--symbols", "hk00700,usAAPL"],
        ["monitor"], ["kill", "--reason", "x"],
        ["resume", "--confirm", "RESUME-TRADING"],
        ["cancel", "--order-id", "X1"],
        ["sdk-audit"],
        ["demo"], ["demo", "--keep"],
        ["web"], ["web", "--port", "9000", "--host", "0.0.0.0"],
        ["vault", "list"], ["vault", "set", "--name", "K", "--value", "v"],
        ["vault", "get", "K"], ["vault", "delete", "K"],
        ["vault", "export", "--passphrase", "0123456789ab"],
        ["vault", "import", "--infile", "x.enc", "--passphrase", "0123456789ab"],
    ]
    for argv in cases:
        ns = p.parse_args(argv)
        assert hasattr(ns, "func"), argv


def test_web_accepts_log_level_after_subcommand():
    """回归：`web --log-level` 曾因只定义在全局而报 unrecognized arguments。"""
    ns = cli.build_parser().parse_args(["web", "--log-level", "warning", "--port", "9123"])
    assert ns.log_level == "warning"
    assert ns.port == 9123


def test_global_log_level_still_works_before_subcommand():
    ns = cli.build_parser().parse_args(["--log-level", "debug", "status"])
    assert ns.log_level == "debug"


def test_no_subcommand_raises():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_status_command_emits_safe_json(tmp_path, monkeypatch, capsys):
    """status 输出必须是可解析 JSON，且绝不含真实下单资格。"""
    from ai_trader.config import AppConfig, build_gate
    from ai_trader.runtime import build_runtime
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(cli, "_rt", lambda args: build_runtime(cfg))

    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["safety"]["real_order_authorized"] is False
    assert data["broker"]["kind"] == "paper"
    assert data["audit"]["chain"]["ok"] is True


def test_check_env_reports_not_configured_without_credentials(capsys):
    rc = cli.main(["check-env"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["fosun"]["configured"] is False
    assert data["sdk_importable"]["ok"] is True
