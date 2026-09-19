"""주문 실행 계층.

PaperBroker : 실제 주문 없이 현재가로 체결을 흉내 낸다. 잔고·포지션은 상태 파일(JSON)에 남는다.
UpbitBroker : ccxt 로 업비트 KRW 마켓에 시장가 주문을 낸다.
              업비트에는 서버 쪽 손절/익절(스톱) 주문이 없으므로, 손절·익절은 봇이 가격을 보고 시장가 매도로 처리한다.
              시장가 매수는 ord_type='price' (금액 지정, ccxt 는 params['cost']), 시장가 매도는 ord_type='market' (수량 지정).

Fill = dict(price=체결단가, amount=수량, cost=체결금액, fee=수수료, order_id=주문번호)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float
    stop: float
    target: float
    entered_at: str           # ISO 시각
    signal_bar: str           # 신호 봉 시각 (같은 봉에 두 번 진입 방지)
    initial_stop: float = 0.0  # 진입 당시 손절가 (R 계산용, 본전 이동 후에도 유지)
    bars_held: int = 0
    highest: float = 0.0
    order_id: Optional[str] = None


@dataclass
class State:
    cash: float
    positions: dict = field(default_factory=dict)   # symbol -> Position(dict)
    last_signal_bar: dict = field(default_factory=dict)  # symbol -> 마지막으로 처리한 신호 봉 시각
    history: list = field(default_factory=list)     # 체결 기록

    @classmethod
    def load(cls, path: str, init_cash: float) -> "State":
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return cls(cash=d.get("cash", init_cash), positions=d.get("positions", {}), last_signal_bar=d.get("last_signal_bar", {}), history=d.get("history", []))
        return cls(cash=init_cash)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, path)


class Broker:
    name = "base"
    fee_rate = 0.0005

    def get_price(self, symbol: str) -> float:
        raise NotImplementedError

    def buy_market(self, symbol: str, krw_cost: float) -> dict:
        raise NotImplementedError

    def sell_market(self, symbol: str, amount: float) -> dict:
        raise NotImplementedError

    def min_notional(self, symbol: str) -> float:
        return 5000.0


class PaperBroker(Broker):
    """현재가는 외부에서 price_fn 으로 넣어 준다 (실시간 티커 또는 마지막 종가)."""
    name = "paper"

    def __init__(self, price_fn, fee_rate: float = 0.0005, slippage: float = 0.0005):
        self.price_fn = price_fn
        self.fee_rate = fee_rate
        self.slippage = slippage

    def get_price(self, symbol: str) -> float:
        return float(self.price_fn(symbol))

    def buy_market(self, symbol: str, krw_cost: float) -> dict:
        px = self.get_price(symbol) * (1 + self.slippage)
        fee = krw_cost * self.fee_rate
        amount = (krw_cost - fee) / px
        return {"price": px, "amount": amount, "cost": krw_cost, "fee": fee, "order_id": f"paper-{int(time.time()*1000)}"}

    def sell_market(self, symbol: str, amount: float) -> dict:
        px = self.get_price(symbol) * (1 - self.slippage)
        cost = px * amount
        fee = cost * self.fee_rate
        return {"price": px, "amount": amount, "cost": cost - fee, "fee": fee, "order_id": f"paper-{int(time.time()*1000)}"}


class UpbitBroker(Broker):
    name = "upbit"

    def __init__(self, exchange, poll_sec: float = 0.5, poll_tries: int = 20):
        self.ex = exchange
        self.poll_sec = poll_sec
        self.poll_tries = poll_tries
        self.ex.load_markets()
        m = self.ex.market("BTC/KRW")
        self.fee_rate = float(m.get("taker") or 0.0005)

    def get_price(self, symbol: str) -> float:
        t = self.ex.fetch_ticker(symbol)
        return float(t["last"])

    def _wait_fill(self, order_id: str, symbol: str) -> dict:
        last = None
        for _ in range(self.poll_tries):
            last = self.ex.fetch_order(order_id, symbol)
            if last.get("status") in ("closed", "canceled") or (last.get("remaining") in (0, 0.0)):
                break
            time.sleep(self.poll_sec)
        return last or {}

    def buy_market(self, symbol: str, krw_cost: float) -> dict:
        # ord_type=price: 금액(KRW)으로 시장가 매수. ccxt: createOrder(type='market', side='buy', params={'cost': KRW})
        order = self.ex.create_order(symbol, "market", "buy", None, None, {"cost": float(krw_cost)})
        o = self._wait_fill(order["id"], symbol)
        filled = float(o.get("filled") or 0.0)
        avg = float(o.get("average") or 0.0)
        fee = float((o.get("fee") or {}).get("cost") or 0.0)
        if filled <= 0 or avg <= 0:
            raise RuntimeError(f"매수 체결 확인 실패: {o}")
        return {"price": avg, "amount": filled, "cost": avg * filled, "fee": fee, "order_id": order["id"]}

    def sell_market(self, symbol: str, amount: float) -> dict:
        amount = float(self.ex.amount_to_precision(symbol, amount))
        order = self.ex.create_order(symbol, "market", "sell", amount)
        o = self._wait_fill(order["id"], symbol)
        filled = float(o.get("filled") or 0.0)
        avg = float(o.get("average") or 0.0)
        fee = float((o.get("fee") or {}).get("cost") or 0.0)
        if filled <= 0 or avg <= 0:
            raise RuntimeError(f"매도 체결 확인 실패: {o}")
        return {"price": avg, "amount": filled, "cost": avg * filled - fee, "fee": fee, "order_id": order["id"]}

    def balance_krw(self) -> float:
        b = self.ex.fetch_balance()
        return float((b.get("KRW") or {}).get("free") or 0.0)
