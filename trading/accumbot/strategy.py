"""매집 구간 추종 전략 (Accumulation-zone breakout).

영상에서 말한 방식: 기관은 큰 주문을 한 번에 못 넣어서 "같은 가격대에서 며칠에 걸쳐 나눠 매수"한다.
그 흔적(가격은 평평한데 거래량과 매집 지표는 올라감)이 보이고, 그 구간 위로 가격이 뚫고 올라가면 따라 산다.

규칙 (모두 봉 t 까지의 정보만 사용):
  1. 매집 구간: 직전 zone_lookback 봉(t-N .. t-1)의 (고점-저점)/저점 <= zone_max_width
  2. 같은 가격에서 매집: 그 구간 종가 중 cluster_min_ratio 이상이 구간 VWAP ±cluster_band 안
  3. 매집 증거: 구간 평균 거래량 >= vol_ratio_min × 그 이전 vol_lookback 봉 평균 거래량,
                그리고 (선택) OBV 또는 ADL 이 구간 시작보다 끝에서 더 높다(가격은 평평한데 자금은 유입)
  4. 진입 신호: 봉 t 종가 > 구간 고점 × (1+breakout_buffer) 이고 거래량 >= breakout_vol_ratio × 20봉 평균 (기본 1.0 = 평균 이상)
  5. 손절가: 구간 저점 - stop_atr_mult × ATR.   손절 거리가 max_stop_pct 를 넘으면 진입하지 않음
  6. 익절가: 진입가 + tp_r_multiple × (진입가 - 손절가)   (체결가 기준으로 확정)
  7. 보조 청산: breakeven_r 도달 시 손절을 진입가로 올림, trail_atr_mult>0 이면 ATR 트레일링, time_stop_bars 지나면 청산
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Optional

import numpy as np
import pandas as pd

from . import indicators as ind


@dataclass
class StrategyParams:
    zone_lookback: int = 15
    zone_max_width: float = 0.08
    cluster_band: float = 0.03
    cluster_min_ratio: float = 0.7
    vol_lookback: int = 60
    vol_ratio_min: float = 1.0
    flow_indicator: str = "obv"        # 'obv' | 'adl' | 'none'
    flow_min_slope: float = 0.0        # 구간 동안 지표 증가량이 이 값보다 커야 함 (0 = 상승만 요구)
    breakout_buffer: float = 0.005
    breakout_vol_lookback: int = 20
    breakout_vol_ratio: float = 1.0      # 돌파 봉 거래량 ≥ 20봉 평균 × 1.0 (민감도 표에서 1.5 이상은 성과를 깎았음)
    atr_period: int = 14
    stop_atr_mult: float = 0.5
    max_stop_pct: float = 0.12
    tp_r_multiple: float = 2.0
    breakeven_r: float = 1.0           # 0 이면 끔
    trail_atr_mult: float = 0.0        # 0 이면 끔
    time_stop_bars: int = 20           # 0 이면 끔

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "StrategyParams":
        d = dict(d or {})
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


def _check(df: pd.DataFrame) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"OHLCV 열이 없습니다: {missing}")
    if not df.index.is_monotonic_increasing:
        raise ValueError("시간순으로 정렬된 데이터가 필요합니다")


def compute_signals(df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    """각 봉에 대해 신호와 근거를 계산한다. 반환 열:
    zone_high, zone_low, zone_width, zone_vwap, cluster_ratio, vol_ratio, flow_delta,
    breakout, zone_ok, cluster_ok, vol_ok, flow_ok, atr, stop, stop_pct, signal, entry_ref
    signal 이 True 인 봉의 '다음 봉 시가'가 실제 진입 기준이다 (백테스트).  실시간에서는 그 봉 마감 직후 시장가.
    """
    _check(df)
    n = p.zone_lookback
    out = pd.DataFrame(index=df.index)

    # ① 매집 구간 통계: 직전 N 봉 (t-N .. t-1) → shift(1)
    zs = ind.zone_stats(df, n).shift(1)
    out["zone_high"] = zs["zone_high"]
    out["zone_low"] = zs["zone_low"]
    out["zone_width"] = zs["zone_width"]
    out["zone_vwap"] = zs["zone_vwap"]

    # ② 같은 가격 군집 비율 (구간 VWAP 기준, 직전 N 봉)
    out["cluster_ratio"] = ind.cluster_ratio(df["close"], zs["zone_vwap"].ffill(), p.cluster_band, n).shift(1)

    # ③ 거래량: 구간 평균 / 그 이전 vol_lookback 봉 평균
    zone_vol = df["volume"].rolling(n, min_periods=n).mean().shift(1)
    base_vol = df["volume"].rolling(p.vol_lookback, min_periods=max(5, p.vol_lookback // 2)).mean().shift(n + 1)
    out["vol_ratio"] = zone_vol / base_vol.replace(0, np.nan)

    # ③' 자금 흐름 지표: 구간 끝(t-1) - 구간 시작(t-N)
    if p.flow_indicator == "obv":
        flow = ind.obv(df)
    elif p.flow_indicator == "adl":
        flow = ind.adl(df)
    else:
        flow = None
    if flow is not None:
        out["flow_delta"] = flow.shift(1) - flow.shift(n)
        out["flow_ok"] = out["flow_delta"] > p.flow_min_slope
    else:
        out["flow_delta"] = np.nan
        out["flow_ok"] = True

    # ④ 돌파
    avg_vol = df["volume"].rolling(p.breakout_vol_lookback, min_periods=p.breakout_vol_lookback).mean().shift(1)
    out["breakout"] = (df["close"] > out["zone_high"] * (1.0 + p.breakout_buffer)) & (
        df["volume"] >= p.breakout_vol_ratio * avg_vol
    )

    out["zone_ok"] = out["zone_width"] <= p.zone_max_width
    out["cluster_ok"] = out["cluster_ratio"] >= p.cluster_min_ratio
    out["vol_ok"] = out["vol_ratio"] >= p.vol_ratio_min

    # ⑤ 손절
    out["atr"] = ind.atr(df, p.atr_period)
    out["stop"] = out["zone_low"] - p.stop_atr_mult * out["atr"]
    out["entry_ref"] = df["close"]
    out["stop_pct"] = (out["entry_ref"] - out["stop"]) / out["entry_ref"]
    stop_ok = (out["stop_pct"] > 0) & (out["stop_pct"] <= p.max_stop_pct)

    sig = out["breakout"] & out["zone_ok"] & out["cluster_ok"] & out["vol_ok"] & out["flow_ok"] & stop_ok
    out["signal"] = sig.fillna(False).astype(bool)
    return out


def target_price(entry: float, stop: float, r_multiple: float) -> float:
    return entry + r_multiple * (entry - stop)


def explain_row(row: pd.Series) -> str:
    """한 봉의 판정 근거를 한 줄로."""
    def f(x, fmt="{:.4g}"):
        try:
            return fmt.format(float(x))
        except Exception:
            return "-"
    parts = [
        f"구간폭 {f(row.get('zone_width'), '{:.1%}')}({'OK' if row.get('zone_ok') else 'X'})",
        f"군집 {f(row.get('cluster_ratio'), '{:.0%}')}({'OK' if row.get('cluster_ok') else 'X'})",
        f"거래량비 {f(row.get('vol_ratio'), '{:.2f}')}({'OK' if row.get('vol_ok') else 'X'})",
        f"자금흐름 {'OK' if row.get('flow_ok') else 'X'}",
        f"돌파 {'OK' if row.get('breakout') else 'X'}",
        f"손절거리 {f(row.get('stop_pct'), '{:.1%}')}",
    ]
    return " · ".join(parts)
