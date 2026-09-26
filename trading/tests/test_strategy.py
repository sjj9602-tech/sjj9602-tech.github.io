import numpy as np
import pandas as pd
import pytest

from accumbot.strategy import StrategyParams, compute_signals, target_price
from accumbot import indicators as ind


def make_df(closes, volumes, spread=0.01, start="2024-01-01"):
    closes = np.asarray(closes, dtype=float)
    volumes = np.asarray(volumes, dtype=float)
    idx = pd.date_range(start, periods=len(closes), freq="D")
    opens = np.r_[closes[0], closes[:-1]]
    highs = np.maximum(opens, closes) * (1 + spread)
    lows = np.minimum(opens, closes) * (1 - spread)
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}, index=idx)


def synthetic_accumulation(n_pre=80, n_zone=15, breakout_mult=1.06, zone_vol_mult=1.5, breakout_vol_mult=2.5, seed=1):
    """앞 n_pre 봉: 완만한 하락 + 보통 거래량 → n_zone 봉: 10,000 근처 평평 + 거래량 증가(매집) → 돌파 봉."""
    rng = np.random.default_rng(seed)
    pre = np.linspace(11000, 10000, n_pre) + rng.normal(0, 40, n_pre)
    zone = 10000 + rng.normal(0, 30, n_zone)
    closes = np.r_[pre, zone, [10000 * breakout_mult]]
    base_vol = 1000
    vols = np.r_[np.full(n_pre, base_vol) + rng.normal(0, 30, n_pre), np.full(n_zone, base_vol * zone_vol_mult), [base_vol * breakout_vol_mult]]
    # OBV 상승: 구간에서 종가가 소폭이라도 오르는 날에 거래량을 더 준다 → up-days 에 volume 가중
    df = make_df(closes, vols)
    return df


def test_signal_fires_on_breakout_only():
    df = synthetic_accumulation()
    p = StrategyParams(flow_indicator="none")  # 흐름 지표는 별도 테스트
    sig = compute_signals(df, p)
    assert bool(sig["signal"].iloc[-1]), "돌파 봉에서 신호가 나야 한다"
    assert not sig["signal"].iloc[:-1].any(), "구간 안에서는 신호가 없어야 한다"
    # 손절은 구간 저점 아래, 익절은 R 배수
    row = sig.iloc[-1]
    assert row["stop"] < row["zone_low"] <= df["low"].iloc[-16:-1].min() * 1.0001
    entry = df["close"].iloc[-1]
    assert target_price(entry, row["stop"], 2.0) == pytest.approx(entry + 2 * (entry - row["stop"]))


def test_no_signal_without_volume():
    df = synthetic_accumulation(zone_vol_mult=0.6, breakout_vol_mult=0.9)
    sig = compute_signals(df, StrategyParams(flow_indicator="none"))
    assert not sig["signal"].any()


def test_no_signal_when_zone_too_wide():
    df = synthetic_accumulation()
    # 구간 안 한 봉을 크게 흔들어 폭을 8% 넘게 만든다
    df.loc[df.index[-8], ["high"]] = df["high"].iloc[-8] * 1.15
    sig = compute_signals(df, StrategyParams(flow_indicator="none"))
    assert not bool(sig["signal"].iloc[-1])


def test_flow_filter_blocks_distribution():
    """구간에서 오르는 날은 거래량이 작고 내리는 날이 큼(분산) → OBV 하락 → 신호 없음."""
    df = synthetic_accumulation()
    zone = slice(len(df) - 16, len(df) - 1)
    diff = np.sign(df["close"].diff()).iloc[zone].to_numpy()
    vols = df["volume"].iloc[zone].to_numpy().copy()
    vols[diff > 0] *= 0.3
    vols[diff < 0] *= 2.0
    df.iloc[zone, df.columns.get_loc("volume")] = vols
    sig = compute_signals(df, StrategyParams(flow_indicator="obv"))
    assert not bool(sig["signal"].iloc[-1])


def test_indicators_have_no_lookahead():
    df = synthetic_accumulation()
    sig_full = compute_signals(df, StrategyParams(flow_indicator="none"))
    cut = len(df) - 5
    sig_cut = compute_signals(df.iloc[:cut], StrategyParams(flow_indicator="none"))
    # 앞부분 결과는 뒤 데이터를 붙여도 변하지 않아야 한다
    a = sig_full.iloc[:cut][["signal", "stop", "zone_high"]].fillna(-1)
    b = sig_cut[["signal", "stop", "zone_high"]].fillna(-1)
    pd.testing.assert_frame_equal(a, b)


def test_atr_obv_basic():
    df = synthetic_accumulation()
    a = ind.atr(df, 14)
    assert a.iloc[-1] > 0 and a.iloc[:13].isna().all()
    o = ind.obv(df)
    assert len(o) == len(df)
