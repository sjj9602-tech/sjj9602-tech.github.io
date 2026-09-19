"""데이터 로더. 모두 소문자 열(open, high, low, close, volume), 시간순 DatetimeIndex 로 통일한다.

- yfinance: 국내주식(005930.KS, 247540.KQ …), 해외주식, 지수. 일봉 위주. 실시간 매매는 없음(백테스트/스캔용).
- upbit(ccxt): 코인 KRW 마켓. 200봉/호출이라 since 로 나눠 받는다. 실시간 매매 가능.
- csv: 사용자가 가진 파일.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import pandas as pd

COLS = ["open", "high", "low", "close", "volume"]


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = [c[0] for c in d.columns]
    d.columns = [str(c).strip().lower().replace(" ", "_") for c in d.columns]
    rename = {"adj_close": "adj_close", "vol": "volume"}
    d = d.rename(columns=rename)
    missing = [c for c in COLS if c not in d.columns]
    if missing:
        raise ValueError(f"열이 없습니다: {missing} (있는 열: {list(d.columns)})")
    d = d[COLS + [c for c in d.columns if c not in COLS]]
    if not isinstance(d.index, pd.DatetimeIndex):
        d.index = pd.to_datetime(d.index)
    if d.index.tz is not None:
        d.index = d.index.tz_convert(None)
    d = d[~d.index.duplicated(keep="last")].sort_index()
    d = d.dropna(subset=["open", "high", "low", "close"])
    d["volume"] = d["volume"].fillna(0.0)
    return d.astype({c: float for c in COLS})


# ---------------------------------------------------------------- yfinance
def load_yfinance(ticker: str, start: Optional[str] = None, end: Optional[str] = None, period: str = "5y", interval: str = "1d") -> pd.DataFrame:
    import yfinance as yf

    kw = dict(interval=interval, auto_adjust=False, progress=False, threads=False)
    if start:
        df = yf.download(ticker, start=start, end=end, **kw)
    else:
        df = yf.download(ticker, period=period, **kw)
    if df is None or len(df) == 0:
        raise ValueError(f"yfinance 에서 {ticker} 데이터를 못 받았습니다")
    return normalize(df)


# ---------------------------------------------------------------- upbit (ccxt)
def make_upbit(api_key: Optional[str] = None, secret: Optional[str] = None):
    """ccxt.upbit 인스턴스. 키는 인자 → 환경변수(UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY) 순.
    프록시 환경(회사망 등)에서는 ACCUMBOT_TRUST_ENV=1, REQUESTS_CA_BUNDLE=경로 를 주면 그대로 따른다.
    """
    import ccxt

    cfg = {"enableRateLimit": True}
    api_key = api_key or os.environ.get("UPBIT_ACCESS_KEY")
    secret = secret or os.environ.get("UPBIT_SECRET_KEY")
    if api_key and secret:
        cfg.update({"apiKey": api_key, "secret": secret})
    ex = ccxt.upbit(cfg)
    if os.environ.get("ACCUMBOT_TRUST_ENV") == "1":
        ex.session.trust_env = True
    ca = os.environ.get("REQUESTS_CA_BUNDLE")
    if ca:
        ex.verify = ca
    return ex


def load_upbit(symbol: str = "BTC/KRW", timeframe: str = "1d", days: int = 730, exchange=None, drop_partial: bool = True) -> pd.DataFrame:
    """since 를 뒤로 물리며 200봉씩 받아 이어 붙인다. drop_partial=True 면 아직 안 끝난 마지막 봉을 뺀다."""
    ex = exchange or make_upbit()
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    since = ex.milliseconds() - days * 24 * 3600 * 1000
    rows: list = []
    while True:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=200)
        if not batch:
            break
        rows.extend(batch)
        last = batch[-1][0]
        if len(batch) < 200 or last + tf_ms >= ex.milliseconds():
            break
        since = last + tf_ms
        time.sleep(ex.rateLimit / 1000.0)
    if not rows:
        raise ValueError(f"업비트에서 {symbol} {timeframe} 데이터를 못 받았습니다")
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert(None)
    df = df.set_index("ts")
    df = normalize(df)
    if drop_partial and len(df) > 1:
        # 마지막 봉의 시작 + 봉 길이 가 아직 안 지났으면 진행 중인 봉
        last_start = df.index[-1]
        if (pd.Timestamp.utcnow().tz_localize(None) - last_start) < pd.Timedelta(milliseconds=tf_ms):
            df = df.iloc[:-1]
    return df


# ---------------------------------------------------------------- csv
def load_csv(path: str, date_col: Optional[str] = None) -> pd.DataFrame:
    df = pd.read_csv(path)
    col = date_col or next((c for c in df.columns if str(c).lower() in ("date", "datetime", "time", "timestamp", "ts", "candle_date_time_kst")), df.columns[0])
    df[col] = pd.to_datetime(df[col])
    df = df.set_index(col)
    return normalize(df)


def load(source: str, symbol: str, **kw) -> pd.DataFrame:
    source = source.lower()
    if source in ("yfinance", "yf", "stock"):
        return load_yfinance(symbol, start=kw.get("start"), end=kw.get("end"), period=kw.get("period", "5y"), interval=kw.get("interval", "1d"))
    if source in ("upbit", "crypto"):
        return load_upbit(symbol, timeframe=kw.get("timeframe", "1d"), days=int(kw.get("days", 730)), exchange=kw.get("exchange"))
    if source == "csv":
        return load_csv(symbol)
    raise ValueError(f"알 수 없는 source: {source}")
