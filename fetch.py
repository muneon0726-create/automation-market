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
    "usdjpy": ("usdjpy",  "JPY=X", "DEXJPUS"),    # ドル円
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
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
 
 
def fetch_stooq(sym):
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(sym)}&i=d"
    text = http_get(url, referer=f"https://stooq.com/q/d/?s={urllib.parse.quote(sym)}")
    if not text.lstrip().startswith("Date,"):
        raise ValueError("stooq: CSVでない応答 (bot検証の可能性)")
    rows = list(csv.DictReader(io.StringIO(text)))
    return [
        {"Date": r["Date"], "Open": r["Open"], "High": r["High"],
         "Low": r["Low"], "Close": r["Close"]}
        for r in rows if r.get("Close") not in (None, "", "0")
    ]
 
 
def fetch_yahoo(sym):
    import yfinance as yf
    df = yf.download(sym, period="max", interval="1d",
                     auto_adjust=True, progress=False)
    if df is None or len(df) == 0:
        raise ValueError("yahoo: 空の応答")
    if hasattr(df.columns, "get_level_values") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    out = []
    for idx, r in df.iterrows():
        out.append({"Date": idx.strftime("%Y-%m-%d"),
                    "Open": f"{float(r['Open']):.4f}", "High": f"{float(r['High']):.4f}",
                    "Low": f"{float(r['Low']):.4f}", "Close": f"{float(r['Close']):.4f}"})
    return out
 
 
def fetch_fred(series):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd=2016-01-01"
    text = http_get(url)
    rows = list(csv.DictReader(io.StringIO(text)))
    out = []
    for r in rows:
        vals = list(r.values())
        if len(vals) >= 2 and vals[1] not in ("", ".", None):
            out.append({"Date": vals[0], "Open": "", "High": "",
                        "Low": "", "Close": vals[1]})
    if not out:
        raise ValueError("fred: データなし")
    return out
 
 
def validate(rows, name, max_stale_days=7):
    if len(rows) < 50:
        raise ValueError(f"行数不足: {len(rows)}")
    last = rows[-1]
    d = datetime.date.fromisoformat(last["Date"])
    if (TODAY - d).days > max_stale_days:
        raise ValueError(f"鮮度不良: 最終日 {d}")
    for r in rows[-250:]:
        dd = datetime.date.fromisoformat(r["Date"])
        if dd.weekday() >= 5:
            raise ValueError(f"週末の日付が混入: {dd}")
        if r["Open"]:  # OHLCあり系列のみ
            o, h, l, c = (float(r[k]) for k in ("Open", "High", "Low", "Close"))
            if not (l <= o <= h and l <= c <= h and l > 0):
                raise ValueError(f"OHLC不整合: {r['Date']}")
    return True
 
 
def main():
    os.makedirs("data", exist_ok=True)
    errors = []
    for name, (stooq_sym, yahoo_sym, fred_series) in SERIES.items():
        sources = []
        if stooq_sym:
            sources.append(("stooq", lambda s=stooq_sym: fetch_stooq(s)))
        if yahoo_sym:
            sources.append(("yahoo", lambda s=yahoo_sym: fetch_yahoo(s)))
        if fred_series:
            sources.append(("fred", lambda s=fred_series: fetch_fred(s)))
        ok = False
        errs = []
        for src_name, fn in sources:
            try:
                rows = fn()
                validate(rows, name)
                with open(f"data/{name}.csv", "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=["Date", "Open", "High", "Low", "Close"])
                    w.writeheader()
                    w.writerows(rows)
                print(f"OK  {name:8s} <- {src_name:6s} rows={len(rows)} last={rows[-1]['Date']}")
                ok = True
                break
            except Exception as e:
                errs.append(f"{src_name}: {e}")
        if not ok:
            errors.append(f"{name}: " + " | ".join(errs))
            print(f"NG  {name}: " + " | ".join(errs), file=sys.stderr)
    if errors:
        sys.exit("FETCH ERRORS:\n" + "\n".join(errors))
 
 
if __name__ == "__main__":
    main()
