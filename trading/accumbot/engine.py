"""실시간(모의/실거래) 엔진. 한 번 호출(run_once)에 아래를 한다.

  각 종목에 대해
    1) 봉 데이터 갱신 → 신호 계산
    2) 보유 중이면: 현재가로 손절·익절·시간청산 판정 → 시장가 매도. 본전/트레일링 손절 갱신.
    3) 미보유이고 '마지막 마감 봉'에 신호가 있고 그 봉을 아직 처리하지 않았으면: 위험 기준으로 금액 계산 → 시장가 매수,
       손절·익절가를 상태에 기록.
  상태는 JSON 파일에 저장된다. 스케줄러(cron / 윈도 작업 스케줄러)로 봉 마감 직후 반복 실행하거나, --loop 로 계속 돈다.

mode: paper(기본) → PaperBroker, 잔고는 config.paper_cash 에서 시작.
mode: live        → UpbitBroker, 실제 주문. 키는 환경변수 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY.
"""
from __future__ import annotations

import time
from dataclasses import asdict
from datetime import datetime
from typing import Optional

import pandas as pd

from . import data as D
from .backtest import BacktestConfig, position_size
from .broker import Broker, PaperBroker, UpbitBroker, Position, State
from .notify import notify
from .strategy import StrategyParams, compute_signals, target_price, explain_row


DEFAULT_CONFIG = {
    "mode": "paper",                  # paper | live
    "source": "upbit",                # upbit | yfinance (yfinance 는 paper 전용)
    "symbols": ["BTC/KRW", "ETH/KRW"],
    "timeframe": "1d",
    "history_days": 400,
    "state_file": "state/state.json",
    "paper_cash": 1_000_000,
    "risk_pct": 0.01,
    "max_position_pct": 0.3,
    "max_positions": 3,
    "fee_bps": 5,
    "slippage_bps": 5,
    "min_notional": 5000,
    "lot_size": 0,
    "strategy": {},
}


def load_config(path: Optional[str]) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path:
        import yaml

        with open(path, encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        cfg.update(user)
    return cfg


def _bt_config(cfg: dict) -> BacktestConfig:
    return BacktestConfig(
        init_equity=float(cfg["paper_cash"]), risk_pct=float(cfg["risk_pct"]), max_position_pct=float(cfg["max_position_pct"]),
        fee_bps=float(cfg["fee_bps"]), slippage_bps=float(cfg["slippage_bps"]), lot_size=float(cfg.get("lot_size", 0)),
        min_notional=float(cfg["min_notional"]),
    )


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.params = StrategyParams.from_dict(cfg.get("strategy"))
        self.bt = _bt_config(cfg)
        self.state = State.load(cfg["state_file"], float(cfg["paper_cash"]))
        self.exchange = None
        self._last_close: dict[str, float] = {}
        if cfg["source"] == "upbit":
            self.exchange = D.make_upbit()
        if cfg["mode"] == "live":
            if cfg["source"] != "upbit":
                raise ValueError("실거래(live)는 source: upbit 만 지원합니다")
            if not (self.exchange.apiKey and self.exchange.secret):
                raise ValueError("실거래에는 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 환경변수가 필요합니다")
            self.broker: Broker = UpbitBroker(self.exchange)
        else:
            self.broker = PaperBroker(self._price, fee_rate=self.bt.fee_bps / 10_000, slippage=self.bt.slippage_bps / 10_000)

    # ---- 가격
    def _price(self, symbol: str) -> float:
        if self.exchange is not None:
            try:
                return float(self.exchange.fetch_ticker(symbol)["last"])
            except Exception:
                pass
        return self._last_close[symbol]

    def _load(self, symbol: str) -> pd.DataFrame:
        if self.cfg["source"] == "upbit":
            df = D.load_upbit(symbol, self.cfg["timeframe"], int(self.cfg["history_days"]), exchange=self.exchange)
        elif self.cfg["source"] == "yfinance":
            df = D.load_yfinance(symbol, period=f"{max(1, int(self.cfg['history_days']) // 365 + 1)}y")
        else:
            raise ValueError("source 는 upbit 또는 yfinance")
        self._last_close[symbol] = float(df["close"].iloc[-1])
        return df

    # ---- 자본
    def equity(self) -> float:
        total = self.state.cash
        for sym, p in self.state.positions.items():
            total += p["qty"] * self._last_close.get(sym, p["entry_price"])
        return total

    # ---- 한 종목 처리
    def process_symbol(self, symbol: str) -> None:
        df = self._load(symbol)
        sig = compute_signals(df, self.params)
        last_bar = df.index[-1]
        row = sig.iloc[-1]
        price = self.broker.get_price(symbol)
        pos = self.state.positions.get(symbol)

        if pos:
            self._manage_position(symbol, pos, price, float(row["atr"]) if pd.notna(row["atr"]) else None, last_bar)
            return

        if not bool(row["signal"]):
            return
        bar_key = str(last_bar)
        if self.state.last_signal_bar.get(symbol) == bar_key:
            return  # 이 봉의 신호는 이미 처리함
        if len(self.state.positions) >= int(self.cfg["max_positions"]):
            notify(f"{symbol} 신호 있으나 최대 보유 종목 수({self.cfg['max_positions']}) 초과로 건너뜀")
            self.state.last_signal_bar[symbol] = bar_key
            return
        stop = float(row["stop"])
        if price <= stop:
            notify(f"{symbol} 신호 있으나 현재가 {price:,.0f} 가 손절가 {stop:,.0f} 이하라 건너뜀")
            self.state.last_signal_bar[symbol] = bar_key
            return
        qty = position_size(self.equity(), price, stop, self.bt)
        krw = qty * price
        if qty <= 0 or krw < self.bt.min_notional or krw > self.state.cash:
            notify(f"{symbol} 신호 있으나 주문 금액 {krw:,.0f}원이 조건에 안 맞아 건너뜀 (현금 {self.state.cash:,.0f})")
            self.state.last_signal_bar[symbol] = bar_key
            return
        fill = self.broker.buy_market(symbol, krw)
        target = target_price(fill["price"], stop, self.params.tp_r_multiple)
        p = Position(symbol=symbol, qty=fill["amount"], entry_price=fill["price"], stop=stop, target=target,
                     entered_at=datetime.now().isoformat(timespec="seconds"), initial_stop=stop, signal_bar=bar_key, highest=fill["price"], order_id=fill.get("order_id"))
        self.state.positions[symbol] = asdict(p)
        self.state.cash -= fill["cost"] + (fill["fee"] if self.broker.name == "paper" else 0.0)
        self.state.last_signal_bar[symbol] = bar_key
        self.state.history.append({"t": p.entered_at, "symbol": symbol, "side": "buy", **fill, "stop": stop, "target": target, "why": explain_row(row)})
        notify(f"[{self.broker.name}] {symbol} 매수 {fill['amount']:.6g} @ {fill['price']:,.0f} (금액 {fill['cost']:,.0f}원)\n"
               f"손절 {stop:,.0f} · 익절 {target:,.0f} · 근거: {explain_row(row)}")

    def _manage_position(self, symbol: str, pos: dict, price: float, atr: Optional[float], last_bar) -> None:
        reason = None
        if price <= pos["stop"]:
            reason = "손절"
        elif price >= pos["target"]:
            reason = "익절"
        else:
            # 봉이 하나 지났으면 보유 봉수 +1, 본전/트레일링 갱신
            if pos.get("last_bar") != str(last_bar):
                pos["bars_held"] = int(pos.get("bars_held", 0)) + (1 if pos.get("last_bar") else 0)
                pos["last_bar"] = str(last_bar)
            pos["highest"] = max(float(pos.get("highest", 0.0)), price)
            r = pos["entry_price"] - float(pos.get("initial_stop") or pos["stop"])
            if self.params.breakeven_r and r > 0 and pos["highest"] >= pos["entry_price"] + self.params.breakeven_r * r:
                pos["stop"] = max(pos["stop"], pos["entry_price"])
            if self.params.trail_atr_mult and atr:
                pos["stop"] = max(pos["stop"], pos["highest"] - self.params.trail_atr_mult * atr)
            if self.params.time_stop_bars and pos["bars_held"] >= self.params.time_stop_bars:
                reason = "시간청산"
        if reason:
            fill = self.broker.sell_market(symbol, pos["qty"])
            pnl = (fill["price"] - pos["entry_price"]) * pos["qty"] - fill["fee"]
            self.state.cash += fill["cost"]
            del self.state.positions[symbol]
            self.state.history.append({"t": datetime.now().isoformat(timespec="seconds"), "symbol": symbol, "side": "sell", **fill, "reason": reason, "pnl": pnl})
            notify(f"[{self.broker.name}] {symbol} {reason} 매도 {fill['amount']:.6g} @ {fill['price']:,.0f} · 손익 {pnl:+,.0f}원")
        else:
            self.state.positions[symbol] = pos

    def run_once(self) -> None:
        for symbol in self.cfg["symbols"]:
            try:
                self.process_symbol(symbol)
            except Exception as e:
                notify(f"{symbol} 처리 중 오류: {type(e).__name__}: {e}")
        self.state.save(self.cfg["state_file"])

    def run_loop(self, interval_sec: int) -> None:
        notify(f"엔진 시작 [{self.cfg['mode']}] {self.cfg['symbols']} {self.cfg['timeframe']} · {interval_sec}초마다 점검")
        while True:
            self.run_once()
            time.sleep(interval_sec)

    def summary(self) -> str:
        lines = [f"모드 {self.cfg['mode']} · 현금 {self.state.cash:,.0f}원 · 보유 {len(self.state.positions)}종목"]
        for sym, p in self.state.positions.items():
            lines.append(f"  {sym}: {p['qty']:.6g} @ {p['entry_price']:,.0f} · 손절 {p['stop']:,.0f} · 익절 {p['target']:,.0f} · 보유 {p.get('bars_held', 0)}봉")
        if self.state.history:
            sells = [h for h in self.state.history if h.get("side") == "sell"]
            if sells:
                lines.append(f"  청산 {len(sells)}건 · 누적손익 {sum(h.get('pnl', 0) for h in sells):+,.0f}원")
        return "\n".join(lines)
