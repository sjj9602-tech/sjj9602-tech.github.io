"""보조지표 계산. 모두 pandas Series/DataFrame 입력, 같은 길이의 Series 반환.

DataFrame 열 이름은 소문자 open, high, low, close, volume 로 통일한다 (data.py 가 맞춰 준다).
모든 지표는 해당 봉까지의 정보만 쓴다 (미래 참조 없음).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder 방식과 같은 지수평활). 첫 period 봉은 NaN."""
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume: 종가가 오르면 +거래량, 내리면 -거래량 누적."""
    direction = np.sign(df["close"].diff()).fillna(0.0)
    return (direction * df["volume"]).cumsum()


def adl(df: pd.DataFrame) -> pd.Series:
    """Accumulation/Distribution Line.
    CLV = ((close-low) - (high-close)) / (high-low), 누적(CLV*volume). high==low 면 0.
    """
    hl = (df["high"] - df["low"]).replace(0, np.nan)
    clv = (((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl).fillna(0.0)
    return (clv * df["volume"]).cumsum()


def rolling_slope(s: pd.Series, window: int) -> pd.Series:
    """창 안에서 (마지막값 - 처음값) / window. 단순 기울기 (NaN 안전)."""
    return (s - s.shift(window - 1)) / float(window)


def zone_stats(df: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """최근 lookback 봉의 박스(매집 구간) 통계.

    반환 열: zone_high, zone_low, zone_mid, zone_width (구간폭/저점), zone_vwap,
             cluster_ratio (종가가 zone_vwap ±band 안에 든 봉의 비율은 strategy 에서 band 를 알아야 하므로 여기서는 계산 안 함)
    """
    high = df["high"].rolling(lookback, min_periods=lookback).max()
    low = df["low"].rolling(lookback, min_periods=lookback).min()
    pv = (df["close"] * df["volume"]).rolling(lookback, min_periods=lookback).sum()
    v = df["volume"].rolling(lookback, min_periods=lookback).sum()
    vwap = pv / v.replace(0, np.nan)
    out = pd.DataFrame(index=df.index)
    out["zone_high"] = high
    out["zone_low"] = low
    out["zone_mid"] = (high + low) / 2.0
    out["zone_width"] = (high - low) / low.replace(0, np.nan)
    out["zone_vwap"] = vwap
    return out


def cluster_ratio(close: pd.Series, center: pd.Series, band: float, lookback: int) -> pd.Series:
    """최근 lookback 봉 중 종가가 center*(1±band) 안에 든 비율. center 는 각 시점의 기준가(예: zone_vwap).

    주의: 기준가는 '그 시점'의 값 하나를 쓰고, 과거 lookback 봉의 종가를 그 기준가와 비교한다.
    """
    vals = np.full(len(close), np.nan)
    c = close.to_numpy(dtype=float)
    ctr = center.to_numpy(dtype=float)
    for i in range(lookback - 1, len(c)):
        k = ctr[i]
        if not np.isfinite(k) or k <= 0:
            continue
        window = c[i - lookback + 1 : i + 1]
        vals[i] = np.mean(np.abs(window / k - 1.0) <= band)
    return pd.Series(vals, index=close.index)
