"""이벤트 기반 백테스트 (봉 단위, 미래 참조 없음).

체결 규칙
  - 신호가 뜬 봉의 '다음 봉 시가'에 매수 (슬리피지 가산). 실시간 봇은 봉 마감 직후 시장가 → 거의 같은 가격.
  - 손절/익절은 봉 안에서 판정. 같은 봉에서 둘 다 닿으면 손절을 먼저 인정(보수적).
  - 시가가 손절가 아래로 갭 하락하면 시가에 체결(더 나쁜 가격), 시가가 익절가 위로 갭 상승하면 시가에 체결(더 좋은 가격).
  - 수수료는 매수·매도 양쪽에 bps 로 차감. 슬리피지는 매수는 위로, 매도는 아래로.
  - 포지션 크기: 자본 × risk_pct 를 (진입가-손절가)로 나눈 수량. 명목가치는 자본 × max_position_pct 를 넘지 않음.
  - lot_size > 0 이면 수량을 그 단위로 내림 (주식 1주, 코인은 0 → 소수 허용).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import math
import numpy as np
import pandas as pd

from .strategy import StrategyParams, compute_signals, target_price


@dataclass
class BacktestConfig:
    init_equity: float = 10_000_000.0
    risk_pct: float = 0.01
    max_position_pct: float = 0.30
    fee_bps: float = 5.0          # 0.05% (업비트 0.05%, 국내주식은 세금 포함 더 큼 → 25~30 권장)
    slippage_bps: float = 5.0
    lot_size: float = 0.0         # 0 = 소수 수량 허용
    min_notional: float = 5000.0  # 이보다 작은 주문은 건너뜀 (업비트 최소 5,000원)


@dataclass
class Trade:
    symbol: str
    entry_time: pd.Timestamp
    entry_price: float
    qty: float
    stop: float
    target: float
    initial_stop: float = 0.0
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    reason: Optional[str] = None
    fees: float = 0.0
    bars_held: int = 0

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.qty - self.fees

    @property
    def r_multiple(self) -> float:
        risk = (self.entry_price - (self.initial_stop or self.stop)) * self.qty
        return self.pnl / risk if risk > 0 else float("nan")


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series
    metrics: dict
    signals: pd.DataFrame
    params: dict = field(default_factory=dict)


def _round_lot(qty: float, lot: float) -> float:
    if lot and lot > 0:
        return math.floor(qty / lot) * lot
    return qty


def position_size(equity: float, entry: float, stop: float, cfg: BacktestConfig) -> float:
    risk_amt = equity * cfg.risk_pct
    per_unit = entry - stop
    if per_unit <= 0:
        return 0.0
    qty = risk_amt / per_unit
    max_qty = (equity * cfg.max_position_pct) / entry
    qty = min(qty, max_qty)
    qty = _round_lot(qty, cfg.lot_size)
    if qty * entry < cfg.min_notional:
        return 0.0
    return qty


def run_backtest(df: pd.DataFrame, params: StrategyParams, cfg: BacktestConfig, symbol: str = "") -> BacktestResult:
    sig = compute_signals(df, params)
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    idx = df.index
    n = len(df)
    fee = cfg.fee_bps / 10_000.0
    slip = cfg.slippage_bps / 10_000.0

    equity = cfg.init_equity
    cash = equity
    eq_curve = np.empty(n)
    trades: list[Trade] = []
    pos: Optional[Trade] = None
    pending_stop: Optional[float] = None   # 신호 봉의 손절가 (다음 봉 시가에 진입)
    highest_since_entry = 0.0

    for i in range(n):
        # ---- 1) 대기 중인 진입을 이번 봉 시가에 체결
        if pos is None and pending_stop is not None:
            fill = o[i] * (1.0 + slip)
            if fill > pending_stop:
                qty = position_size(equity, fill, pending_stop, cfg)
                if qty > 0:
                    buy_fee = fill * qty * fee
                    cash -= fill * qty + buy_fee
                    pos = Trade(symbol, idx[i], fill, qty, pending_stop, target_price(fill, pending_stop, params.tp_r_multiple), initial_stop=pending_stop, fees=buy_fee)
                    highest_since_entry = fill
            pending_stop = None

        # ---- 2) 보유 중이면 청산 조건 판정 (이번 봉)
        if pos is not None:
            exit_price = None
            reason = None
            entered_this_bar = pos.entry_time == idx[i]
            # 진입 봉이 아니면 갭(시가) 체결을 먼저 본다. 진입 봉은 시가에 샀으므로 봉 안의 저/고만 본다.
            if not entered_this_bar and o[i] <= pos.stop:
                exit_price, reason = o[i], "stop_gap"
            elif l[i] <= pos.stop:
                exit_price, reason = pos.stop, "stop"
            elif not entered_this_bar and o[i] >= pos.target:
                exit_price, reason = o[i], "target_gap"
            elif h[i] >= pos.target:
                exit_price, reason = pos.target, "target"
            if exit_price is None and params.time_stop_bars and pos.bars_held >= params.time_stop_bars:
                exit_price, reason = c[i], "time"
            if exit_price is not None:
                exit_price = exit_price * (1.0 - slip)
                sell_fee = exit_price * pos.qty * fee
                cash += exit_price * pos.qty - sell_fee
                pos.fees += sell_fee
                pos.exit_time, pos.exit_price, pos.reason = idx[i], exit_price, reason
                trades.append(pos)
                pos = None
            else:
                pos.bars_held += 1
                highest_since_entry = max(highest_since_entry, h[i])
                # 본전 손절 / ATR 트레일링 (봉 마감 후 다음 봉부터 적용)
                r = pos.entry_price - pos.initial_stop
                if params.breakeven_r and r > 0 and h[i] >= pos.entry_price + params.breakeven_r * r:
                    pos.stop = max(pos.stop, pos.entry_price)
                if params.trail_atr_mult and not np.isnan(sig["atr"].iloc[i]):
                    pos.stop = max(pos.stop, highest_since_entry - params.trail_atr_mult * float(sig["atr"].iloc[i]))

        # ---- 3) 이번 봉 마감 신호 → 다음 봉 진입 예약
        if pos is None and bool(sig["signal"].iloc[i]):
            pending_stop = float(sig["stop"].iloc[i])

        equity = cash + (pos.qty * c[i] if pos is not None else 0.0)
        eq_curve[i] = equity

    # 마지막까지 보유 중이면 종가로 평가 청산 (기록용)
    if pos is not None:
        exit_price = c[-1]
        sell_fee = exit_price * pos.qty * fee
        pos.fees += sell_fee
        pos.exit_time, pos.exit_price, pos.reason = idx[-1], exit_price, "open_at_end"
        trades.append(pos)

    tdf = pd.DataFrame([
        {
            "symbol": t.symbol, "entry_time": t.entry_time, "entry_price": t.entry_price, "qty": t.qty,
            "stop": t.initial_stop, "final_stop": t.stop, "target": t.target, "exit_time": t.exit_time, "exit_price": t.exit_price,
            "reason": t.reason, "fees": t.fees, "pnl": t.pnl, "r_multiple": t.r_multiple, "bars_held": t.bars_held,
        }
        for t in trades
    ])
    eq = pd.Series(eq_curve, index=idx, name="equity")
    return BacktestResult(tdf, eq, compute_metrics(tdf, eq, cfg.init_equity), sig, params.to_dict())


def compute_metrics(trades: pd.DataFrame, equity: pd.Series, init_equity: float) -> dict:
    m: dict = {"trades": int(len(trades))}
    if len(equity):
        total_ret = equity.iloc[-1] / init_equity - 1.0
        m["total_return"] = float(total_ret)
        days = max((equity.index[-1] - equity.index[0]).days, 1) if isinstance(equity.index, pd.DatetimeIndex) else len(equity)
        years = days / 365.25
        m["cagr"] = float((equity.iloc[-1] / init_equity) ** (1 / years) - 1.0) if years > 0.1 else float("nan")
        dd = equity / equity.cummax() - 1.0
        m["max_drawdown"] = float(dd.min())
    if len(trades):
        closed = trades[trades["reason"] != "open_at_end"]
        wins = closed[closed["pnl"] > 0]
        losses = closed[closed["pnl"] <= 0]
        m["closed_trades"] = int(len(closed))
        m["win_rate"] = float(len(wins) / len(closed)) if len(closed) else float("nan")
        m["avg_r"] = float(closed["r_multiple"].mean()) if len(closed) else float("nan")
        gp, gl = float(wins["pnl"].sum()), float(-losses["pnl"].sum())
        m["profit_factor"] = float(gp / gl) if gl > 0 else float("inf") if gp > 0 else float("nan")
        m["avg_hold_bars"] = float(closed["bars_held"].mean()) if len(closed) else float("nan")
        m["exit_reasons"] = closed["reason"].value_counts().to_dict()
    return m


def format_metrics(m: dict) -> str:
    def pct(x):
        return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.1%}"
    lines = [
        f"거래 {m.get('trades', 0)}건 (청산 {m.get('closed_trades', 0)}건)",
        f"총수익률 {pct(m.get('total_return'))} · 연환산 {pct(m.get('cagr'))} · 최대낙폭 {pct(m.get('max_drawdown'))}",
        f"승률 {pct(m.get('win_rate'))} · 평균 R {m.get('avg_r', float('nan')):.2f} · 손익비(PF) {m.get('profit_factor', float('nan')):.2f} · 평균 보유 {m.get('avg_hold_bars', float('nan')):.1f}봉",
        f"청산 사유 {m.get('exit_reasons', {})}",
    ]
    return "\n".join(lines)
