import numpy as np
import pandas as pd
import pytest

from accumbot.backtest import BacktestConfig, run_backtest, position_size
from accumbot.strategy import StrategyParams
from tests.test_strategy import synthetic_accumulation


def _cfg(**kw):
    base = dict(init_equity=10_000_000, risk_pct=0.01, max_position_pct=0.5, fee_bps=0, slippage_bps=0, lot_size=0, min_notional=1)
    base.update(kw)
    return BacktestConfig(**base)


def _append(df, closes, highs=None, lows=None, vol=1000):
    idx = pd.date_range(df.index[-1] + pd.Timedelta(days=1), periods=len(closes), freq="D")
    prev = df["close"].iloc[-1]
    opens = np.r_[prev, closes[:-1]]
    highs = np.asarray(highs if highs is not None else np.maximum(opens, closes) * 1.005, float)
    lows = np.asarray(lows if lows is not None else np.minimum(opens, closes) * 0.995, float)
    add = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": np.full(len(closes), vol, float)}, index=idx)
    return pd.concat([df, add])


def test_entry_next_open_and_target_hit():
    df = synthetic_accumulation()
    stop_ref = None
    # 돌파 다음 날 시가 10,650, 이후 상승해 익절 도달
    df2 = _append(df, closes=[10700, 11000, 11600, 13000], highs=[10800, 11100, 11700, 13500])
    df2.loc[df2.index[-4], "open"] = 10650
    p = StrategyParams(flow_indicator="none", tp_r_multiple=2.0, time_stop_bars=0, breakeven_r=0)
    res = run_backtest(df2, p, _cfg())
    assert len(res.trades) == 1
    t = res.trades.iloc[0]
    assert t["entry_time"] == df2.index[-4]
    assert t["entry_price"] == pytest.approx(10650)
    assert t["reason"] in ("target", "target_gap")
    assert t["exit_price"] >= t["target"] - 1e-6
    assert t["pnl"] > 0
    assert res.metrics["win_rate"] == 1.0


def test_stop_hit_intrabar_and_gap():
    df = synthetic_accumulation()
    df2 = _append(df, closes=[10600, 10200, 9400], lows=[10500, 9300, 9200], highs=[10700, 10650, 9500])
    p = StrategyParams(flow_indicator="none", time_stop_bars=0, breakeven_r=0)
    res = run_backtest(df2, p, _cfg())
    assert len(res.trades) == 1
    t = res.trades.iloc[0]
    assert t["reason"] == "stop"
    assert t["exit_price"] == pytest.approx(t["stop"])
    # 손실은 자본의 약 1% (risk_pct) 이내
    assert -res.metrics["max_drawdown"] <= 0.011 + 1e-9


def test_time_stop():
    df = synthetic_accumulation()
    flat = [10620] * 25
    df2 = _append(df, closes=flat, highs=[10640] * 25, lows=[10600] * 25)
    p = StrategyParams(flow_indicator="none", time_stop_bars=5, breakeven_r=0)
    res = run_backtest(df2, p, _cfg())
    assert len(res.trades) == 1
    assert res.trades.iloc[0]["reason"] == "time"
    assert res.trades.iloc[0]["bars_held"] == 5


def test_position_size_respects_risk_and_cap():
    cfg = _cfg(risk_pct=0.01, max_position_pct=0.2, lot_size=1)
    qty = position_size(10_000_000, 10_000, 9_500, cfg)
    # 위험 100,000원 / 500원 = 200주 → 명목 2,000,000 = 자본의 20% 상한과 동일
    assert qty == 200
    qty2 = position_size(10_000_000, 10_000, 9_900, cfg)
    # 위험 기준 1000주지만 상한 200주
    assert qty2 == 200
    assert position_size(10_000_000, 10_000, 10_000, cfg) == 0


def test_fees_reduce_pnl():
    df = synthetic_accumulation()
    df2 = _append(df, closes=[10700, 11000, 11600, 12200], highs=[10800, 11100, 11700, 12400])
    p = StrategyParams(flow_indicator="none", time_stop_bars=0, breakeven_r=0)
    a = run_backtest(df2, p, _cfg(fee_bps=0)).trades.iloc[0]["pnl"]
    b = run_backtest(df2, p, _cfg(fee_bps=25)).trades.iloc[0]["pnl"]
    assert b < a
