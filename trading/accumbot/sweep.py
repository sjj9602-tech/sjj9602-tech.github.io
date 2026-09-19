"""파라미터 민감도 점검 (과최적화 경고 포함).

여러 종목 × 여러 파라미터 조합을 돌려 거래 수·평균 R·손익비를 한 표로 보여준다.
표에서 가장 좋은 조합을 그대로 쓰면 과거에 맞춘 값(과최적화)일 가능성이 크다.
→ 넓은 범위에서 '대체로' 플러스인지, 종목을 바꿔도 비슷한지를 보는 용도다.
"""
from __future__ import annotations

import itertools
from typing import Iterable

import pandas as pd

from .backtest import BacktestConfig, run_backtest
from .strategy import StrategyParams


PRESETS = {
    # 영상 문구 그대로 보수적으로 옮긴 기본값 (주식 일봉 기준)
    "stock": {},
    # 코인은 변동성이 커서 구간 폭·군집 밴드를 넓혀야 신호가 난다
    "crypto": {"zone_max_width": 0.15, "cluster_band": 0.05, "cluster_min_ratio": 0.6, "breakout_vol_ratio": 1.0, "vol_ratio_min": 0.8, "max_stop_pct": 0.18},
    # 신호를 더 자주 보고 싶을 때 (품질은 떨어짐)
    "loose": {"zone_lookback": 10, "zone_max_width": 0.12, "cluster_band": 0.05, "cluster_min_ratio": 0.5, "breakout_vol_ratio": 1.0, "vol_ratio_min": 0.7, "flow_indicator": "none"},
}


def grid(base: dict, space: dict[str, Iterable]) -> list[dict]:
    keys = list(space.keys())
    out = []
    for combo in itertools.product(*[list(space[k]) for k in keys]):
        d = dict(base)
        d.update(dict(zip(keys, combo)))
        out.append(d)
    return out


def run_sweep(datasets: dict[str, pd.DataFrame], param_dicts: list[dict], cfg: BacktestConfig) -> pd.DataFrame:
    rows = []
    for pd_ in param_dicts:
        params = StrategyParams.from_dict(pd_)
        agg = {"trades": 0, "pnl": 0.0, "r_sum": 0.0, "wins": 0, "gp": 0.0, "gl": 0.0}
        per_symbol_pos = 0
        for sym, df in datasets.items():
            res = run_backtest(df, params, cfg, symbol=sym)
            t = res.trades
            t = t[t["reason"] != "open_at_end"] if len(t) else t
            agg["trades"] += len(t)
            if len(t):
                agg["pnl"] += float(t["pnl"].sum())
                agg["r_sum"] += float(t["r_multiple"].fillna(0).sum())
                agg["wins"] += int((t["pnl"] > 0).sum())
                agg["gp"] += float(t.loc[t["pnl"] > 0, "pnl"].sum())
                agg["gl"] += float(-t.loc[t["pnl"] <= 0, "pnl"].sum())
                per_symbol_pos += int(t["pnl"].sum() > 0)
        n = agg["trades"]
        rows.append({
            **{k: pd_.get(k) for k in sorted({k for d in param_dicts for k in d})},
            "trades": n,
            "win_rate": agg["wins"] / n if n else float("nan"),
            "avg_r": agg["r_sum"] / n if n else float("nan"),
            "profit_factor": (agg["gp"] / agg["gl"]) if agg["gl"] > 0 else float("inf") if agg["gp"] > 0 else float("nan"),
            "total_pnl": agg["pnl"],
            "symbols_positive": f"{per_symbol_pos}/{len(datasets)}",
        })
    return pd.DataFrame(rows).sort_values(["trades", "avg_r"], ascending=[False, False]).reset_index(drop=True)
