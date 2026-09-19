"""명령줄.

  python -m accumbot backtest --source upbit --symbol BTC/KRW --days 900
  python -m accumbot backtest --source yfinance --symbol 005930.KS --start 2018-01-01 --fee-bps 25 --lot 1
  python -m accumbot scan --source yfinance --symbols 005930.KS 000660.KS 035420.KS
  python -m accumbot run --config config.yaml            # 한 번 점검 (스케줄러용)
  python -m accumbot run --config config.yaml --loop 300 # 5분마다 점검
  python -m accumbot status --config config.yaml
"""
from __future__ import annotations

import argparse
import json
import sys

from . import data as D
from .backtest import BacktestConfig, run_backtest, format_metrics
from .strategy import StrategyParams, compute_signals, explain_row


def _params_from_args(a) -> StrategyParams:
    from .sweep import PRESETS

    d = dict(PRESETS.get(getattr(a, "preset", None) or "", {}))
    if getattr(a, "params", None):
        d.update(json.loads(a.params))
    return StrategyParams.from_dict(d)


def cmd_backtest(a) -> int:
    df = D.load(a.source, a.symbol, start=a.start, end=a.end, period=a.period, timeframe=a.timeframe, days=a.days)
    params = _params_from_args(a)
    cfg = BacktestConfig(init_equity=a.cash, risk_pct=a.risk, max_position_pct=a.max_pos, fee_bps=a.fee_bps, slippage_bps=a.slip_bps, lot_size=a.lot, min_notional=a.min_notional)
    res = run_backtest(df, params, cfg, symbol=a.symbol)
    print(f"# {a.symbol} {a.source} {df.index[0].date()} ~ {df.index[-1].date()} ({len(df)}봉)")
    print(format_metrics(res.metrics))
    if len(res.trades):
        cols = ["entry_time", "entry_price", "stop", "target", "exit_time", "exit_price", "reason", "r_multiple", "pnl"]
        with __import__("pandas").option_context("display.width", 200, "display.max_columns", 20):
            print(res.trades[cols].tail(a.show).to_string(index=False))
    if a.out:
        res.trades.to_csv(a.out, index=False)
        res.equity.to_csv(a.out.replace(".csv", "_equity.csv"))
        print(f"저장: {a.out}")
    return 0


def cmd_scan(a) -> int:
    params = _params_from_args(a)
    for sym in a.symbols:
        try:
            df = D.load(a.source, sym, period=a.period, timeframe=a.timeframe, days=a.days)
            sig = compute_signals(df, params)
            row = sig.iloc[-1]
            flag = "★ 신호" if bool(row["signal"]) else "  -"
            print(f"{flag} {sym} {df.index[-1].date()} 종가 {df['close'].iloc[-1]:,.0f} | {explain_row(row)}"
                  + (f" | 손절 {row['stop']:,.0f}" if bool(row['signal']) else ""))
        except Exception as e:
            print(f"  ! {sym}: {type(e).__name__}: {e}")
    return 0


def cmd_sweep(a) -> int:
    import json as _json
    from .sweep import PRESETS, grid, run_sweep

    datasets = {}
    for sym in a.symbols:
        try:
            datasets[sym] = D.load(a.source, sym, start=a.start, period=a.period, timeframe=a.timeframe, days=a.days)
        except Exception as e:
            print(f"  ! {sym}: {type(e).__name__}: {e}")
    if not datasets:
        return 1
    base = dict(PRESETS.get(a.preset or "stock", {}))
    if a.params:
        base.update(_json.loads(a.params))
    space = _json.loads(a.space) if a.space else {
        "zone_max_width": [0.06, 0.08, 0.12, 0.15],
        "cluster_min_ratio": [0.5, 0.7],
        "breakout_vol_ratio": [1.0, 1.5, 2.0],
    }
    cfg = BacktestConfig(init_equity=a.cash, risk_pct=a.risk, max_position_pct=a.max_pos, fee_bps=a.fee_bps, slippage_bps=a.slip_bps, lot_size=a.lot, min_notional=a.min_notional)
    table = run_sweep(datasets, grid(base, space), cfg)
    import pandas as pd
    with pd.option_context("display.width", 220, "display.max_columns", 30, "display.max_rows", 200):
        print(table.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    print("\n주의: 이 표에서 제일 좋은 조합을 고르면 과거에 맞춘 값일 수 있습니다. 넓은 범위에서 고르게 플러스인지, 종목을 바꿔도 비슷한지를 보세요.")
    if a.out:
        table.to_csv(a.out, index=False); print(f"저장: {a.out}")
    return 0


def cmd_run(a) -> int:
    from .engine import Engine, load_config

    cfg = load_config(a.config)
    if a.live:
        cfg["mode"] = "live"
    eng = Engine(cfg)
    if cfg["mode"] == "live":
        print("!!! 실거래 모드입니다. 실제 주문이 나갑니다. !!!", file=sys.stderr)
    if a.loop:
        eng.run_loop(int(a.loop))
    else:
        eng.run_once()
        print(eng.summary())
    return 0


def cmd_status(a) -> int:
    from .engine import Engine, load_config

    cfg = load_config(a.config)
    eng = Engine(cfg)
    print(eng.summary())
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="accumbot", description="매집 구간 추종 자동매매")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_data_args(p):
        p.add_argument("--source", default="upbit", choices=["upbit", "yfinance", "csv"])
        p.add_argument("--timeframe", default="1d")
        p.add_argument("--days", type=int, default=900, help="upbit: 받을 일수")
        p.add_argument("--period", default="5y", help="yfinance: 기간 (start 없을 때)")
        p.add_argument("--params", help='전략 파라미터 JSON, 예: {"zone_lookback": 20}')
        p.add_argument("--preset", default=None, choices=["stock", "crypto", "loose"], help="전략 프리셋 (params 보다 먼저 적용)")

    b = sub.add_parser("backtest")
    add_data_args(b)
    b.add_argument("--symbol", required=True)
    b.add_argument("--start"); b.add_argument("--end")
    b.add_argument("--cash", type=float, default=10_000_000)
    b.add_argument("--risk", type=float, default=0.01)
    b.add_argument("--max-pos", dest="max_pos", type=float, default=0.3)
    b.add_argument("--fee-bps", dest="fee_bps", type=float, default=5)
    b.add_argument("--slip-bps", dest="slip_bps", type=float, default=5)
    b.add_argument("--lot", type=float, default=0)
    b.add_argument("--min-notional", dest="min_notional", type=float, default=5000)
    b.add_argument("--show", type=int, default=15)
    b.add_argument("--out")
    b.set_defaults(fn=cmd_backtest)

    s = sub.add_parser("scan")
    add_data_args(s)
    s.add_argument("--symbols", nargs="+", required=True)
    s.set_defaults(fn=cmd_scan)

    sw = sub.add_parser("sweep", help="파라미터 민감도 표")
    add_data_args(sw)
    sw.add_argument("--symbols", nargs="+", required=True)
    sw.add_argument("--start")
    sw.add_argument("--space", help='격자 JSON, 예: {"zone_max_width":[0.08,0.12]}')
    sw.add_argument("--cash", type=float, default=10_000_000)
    sw.add_argument("--risk", type=float, default=0.01)
    sw.add_argument("--max-pos", dest="max_pos", type=float, default=0.3)
    sw.add_argument("--fee-bps", dest="fee_bps", type=float, default=5)
    sw.add_argument("--slip-bps", dest="slip_bps", type=float, default=5)
    sw.add_argument("--lot", type=float, default=0)
    sw.add_argument("--min-notional", dest="min_notional", type=float, default=5000)
    sw.add_argument("--out")
    sw.set_defaults(fn=cmd_sweep)

    r = sub.add_parser("run")
    r.add_argument("--config", default="config.yaml")
    r.add_argument("--loop", type=int, help="초 단위 반복 간격")
    r.add_argument("--live", action="store_true", help="실거래 (config 의 mode 를 덮어씀)")
    r.set_defaults(fn=cmd_run)

    st = sub.add_parser("status")
    st.add_argument("--config", default="config.yaml")
    st.set_defaults(fn=cmd_status)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
