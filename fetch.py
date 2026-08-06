"""日米市場データの自動取得スクリプト（GitHub Actions用）

設計方針:
- 取得元は 多段フォールバック: stooq(生CSV) → Yahoo Finance(yfinance) → FRED(終値のみ)
- S&P500 は FRED公式値のみ使用（stooqの^USLCはレプリカ指数のため使わない: PROJECT.md参照）
- 全系列にバリデーション（鮮度・曜日・OHLC整合・行数）を実施
- 1系列でも失敗すれば最後に exit 1 で Actions を失敗させ通知する。
  ただし成功した系列のCSVは先に保存されるため、部分的な失敗が全体を巻き込まない
"""
import csv
import datetime
import gzip
import io
import os
import sys
import urllib.parse
import urllib.request

# name: (stooq_symbol, yahoo_symbol, fred_series)  Noneはその取得元を使わない
SERIES = {
    # 指数・FX・VIX
    "nkx":    ("^nkx",    "^N225", None),         # 日経平均 OHLC
    "spx":    (None,      None,    "SP500"),      # S&P500 公式終値のみ
    "ndq":    ("^ndq",    "^IXIC", "NASDAQCOM"),  # NASDAQ総合
    "usdjpy": ("usdjpy",  "USDJPY=X", "DEXJPUS"), # ドル円
    "vix":    (None,      "^VIX",  "VIXCLS"),     # VIX
    "soxx":   ("soxx.us", "SOXX",  None),         # 半導体ETF (SOX代替)
    # Phase 3: 個別銘柄（米国連動群）
    "8035": ("8035.jp", "8035.T", None),  # 東京エレクトロン
    "6857": ("6857.jp", "6857.T", None),  # アドバンテスト
    "6146": ("6146.jp", "6146.T", None),  # ディスコ
    "6920": ("6920.jp", "6920.T", None),  # レーザーテック
    "6758": ("6758.jp", "6758.T", None),  # ソニーグループ
    "7974": ("7974.jp", "7974.T", None),  # 任天堂
    "7203": ("7203.jp", "7203.T", None),  # トヨタ自動車
    "6501": ("6501.jp", "6501.T", None),  # 日立製作所
    "8306": ("8306.jp", "8306.T", None),  # 三菱UFJ
    # Phase 3: 対照群（内需・低連動想定）
    "9020": ("9020.jp", "9020.T", None),  # JR東日本
    "2802": ("2802.jp", "2802.T", None),  # 味の素
}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
TODAY = datetime.date.today()


def http_get(url, referer=None):
    headers = {"User-Agent": UA, "Accept-Encoding": "identity"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    raw = urllib.request.urlopen(req, timeout=60).read()
    if raw[:2] == b"\x1f\x8b":  # gzip圧縮されていたら解凍 (FRED対策)
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def fetch_stooq(sym):
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(sym)}&i=d"
    text = http_get(url, referer=f"https://stooq.com/q/d/?s={urllib.parse.quote(sym)}")
    if not text.lstrip().startswith("Date,"):
        raise ValueError("stooq: CSVでない応答 (bot検証の可能性)")
    rows = list(csv.DictReader(io.StringIO(text)))
    return [
        {"Date":
