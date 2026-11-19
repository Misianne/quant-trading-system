#!/usr/bin/env python3
"""逐一实测情报源的可达性，结果写入 ``reports/market_research/source_probe.json``。

只做 **GET**，不登录、不绕过任何访问控制。对返回 403/410/429 的源，
如实记录状态码与原文片段，不做重试穿透（GDELT 按其要求间隔 5 秒重试一次）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

OUT = ROOT / "reports" / "market_research" / "source_probe.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9"}

TARGETS = [
    ("fed_press_rss", "https://www.federalreserve.gov/feeds/press_all.xml",
     "official", "美联储全部新闻稿 RSS（含日期）"),
    ("fed_fomc_calendar", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
     "official", "FOMC 会议日历与声明链接（决议日可从链接日期提取）"),
    ("treasury_press", "https://home.treasury.gov/news/press-releases",
     "official", "财政部新闻稿（制裁、债务、财政政策）"),
    ("bea_schedule", "https://www.bea.gov/news/schedule",
     "official", "BEA 发布日程（GDP/PCE 的排期）"),
    ("bls_schedule", "https://www.bls.gov/schedule/news_release/2026_sched.htm",
     "official", "BLS 发布日程（CPI/非农的具体发布时间）"),
    ("sec_edgar_fts", "https://efts.sec.gov/LATEST/search-index?q=%22test%22",
     "official", "SEC EDGAR 全文检索（监管与公司重大事件）"),
    ("sec_submissions", "https://data.sec.gov/submissions/CIK0000320193.json",
     "official", "SEC submissions API（结构化 filings）"),
    ("eia_v2", "https://api.eia.gov/v2/electricity/rto/region-data/data/",
     "official", "EIA API v2（能源冲击）"),
    ("congress_v3", "https://api.congress.gov/v3/bill?limit=1",
     "official", "Congress.gov API（立法进展）"),
    ("cme_fedwatch", "https://www.cmegroup.com/CmeWS/mvc/MeetingDates/All/F",
     "market", "CME FedWatch（联邦基金期货隐含概率）"),
    ("tradingeconomics", "https://api.tradingeconomics.com/calendar?c=guest:guest&f=json",
     "vendor", "Trading Economics 日历（含一致预期）"),
    ("gdelt_doc", "https://api.gdeltproject.org/api/v2/doc/doc?query=%22federal+reserve%22"
                  "&mode=artlist&maxrecords=3&format=json&timespan=1d",
     "aggregator", "GDELT 全球事件/新闻检索（免费，限 1 次/5 秒）"),
    ("wsj_markets_rss", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
     "media", "WSJ 市场 RSS（版权内容，仅可读不可再分发）"),
    ("reuters_rss", "https://feeds.reuters.com/reuters/businessNews",
     "media", "路透 RSS（近年已停止公开）"),
    ("phillyfed_spf", "https://www.philadelphiafed.org/surveys-and-data/"
                      "real-time-data-research/survey-of-professional-forecasters",
     "official", "费城联储 SPF（专业预测者调查，季度中位数）"),
    ("clevelandfed_nowcast", "https://www.clevelandfed.org/indicators-and-data/"
                             "inflation-nowcasting",
     "official", "克利夫兰联储通胀即时预测（模型预测，非市场一致预期）"),
]


def probe(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=UA)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(4000)
            return {"http": r.status, "bytes_read": len(body),
                    "content_type": r.headers.get("Content-Type", ""),
                    "ms": int((time.monotonic() - t0) * 1000),
                    "preview": body[:200].decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        body = e.read(400)
        return {"http": e.code, "bytes_read": len(body),
                "ms": int((time.monotonic() - t0) * 1000),
                "preview": body[:200].decode("utf-8", "replace")}
    except Exception as exc:  # noqa: BLE001
        return {"http": 0, "error": f"{type(exc).__name__}: {exc}",
                "ms": int((time.monotonic() - t0) * 1000)}


def main() -> int:
    out = {"probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "note": "只做只读 GET；403/410/429 如实记录，不做绕过。", "sources": {}}
    for key, url, kind, desc in TARGETS:
        r = probe(url)
        out["sources"][key] = {"url": url, "kind": kind, "desc": desc, **r}
        print(f"  {key:22s} http={r['http']:>4} {r.get('ms', 0):>6}ms  {desc}")
        if key == "gdelt_doc" and r["http"] == 429:
            time.sleep(6)      # 按其提示"每 5 秒一次"重试一次
            r2 = probe(url)
            out["sources"][key]["retry_after_5s"] = r2
            print(f"  {'':22s} 5 秒后重试 http={r2['http']}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ok = sum(1 for v in out["sources"].values() if v["http"] == 200)
    print(f"\n{ok}/{len(out['sources'])} 个源返回 200 → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
