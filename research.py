"""Research pipeline for the trend allocation bot.

    python research.py            # download data (fixed end date), run everything, write docs/research/
    python research.py --offline  # same, from data/cache/ (verified against docs/research/data_manifest.json)

Read-only with respect to the live bot: config.py, bot.py and bot_state.json are never
changed. The strategy settings (assets, average length, tranche schedule, cash buffer,
minimum order) are READ from config.py, and the trading rules come from allocation.py /
bot.evaluate(), so the research tests exactly the code the bot runs.

Stages
  1. Approach comparison - five approaches on the same 2007-2026 history. This is how
     approach 1 was chosen, so it is IN-SAMPLE model selection, not validation.
  2. Chosen strategy vs. benchmarks (SPY, 60/40, setup J): full period and two halves.
     The halves are robustness checks on data already used for the selection, not
     out-of-sample tests. The only genuine out-of-sample evidence is the paper-trading
     record that starts on 2026-10-05.
  3. Stress years, robustness grid (averages, schedule, costs, universe, signal
     definition), market regimes and rolling statistics.

Execution model (all monthly strategies): the decision for rebalance day d uses closes up
to the previous trading day; trades fill at d's open; every buy and sell, including the
cash asset (BIL), costs COST per side; idle money is held in BIL (T-bill rate before BIL
existed). Strategies use the live bot's 0.5% cash buffer per part and $25 minimum order;
the SPY and 60/40 benchmarks are fully invested with no buffer or minimum.
"""
from __future__ import annotations

import argparse
import functools
import os
import sys

import numpy as np
import pandas as pd

import allocation
import backtest
import bot
import config
import metrics
import regimes
import research_data as rd
from indicators import add_indicators

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "docs", "research")

START = pd.Timestamp("2007-01-03")   # first day all 9/10/11-month averages exist (DBC starts 2006-02-06)
END = rd.END                         # 2026-10-02, fixed
SPLIT_DATE = pd.Timestamp("2015-01-01")
STRESS_YEARS = [2008, 2020, 2022]
COST = 0.0005                        # 0.05% per side, every trade including BIL
CAPITAL = 100_000

# The live strategy, read from config.py (never written).
TREND_ASSETS = list(config.ALLOCATION_ASSETS)
TREND_MONTHS = config.ALLOCATION_MA_MONTHS
TRANCHE_OFFSETS = tuple(allocation.tranche_day(k, config.TRANCHE_SPACING_DAYS) - 1 for k in range(config.TRANCHES))
LIVE_EXECUTION = {"buffer": config.CASH_BUFFER_PCT / 100, "min_order": float(config.MIN_ORDER_VALUE)}
# Faber (2007) original five: US stocks, foreign stocks, 10-year Treasuries, commodities, REITs
FABER_ORIGINAL = ["SPY", "EFA", "IEF", "DBC", "VNQ"]
ETF12 = backtest.WATCHLIST_12
SETUP_J = backtest.CANDIDATES["J: EMA 10/30, crossover only, 12 ETFs"]
TICKERS = sorted(set(TREND_ASSETS + FABER_ORIGINAL + ETF12 + ["BIL"]))

CHOSEN = "Trend allocation (4 parts)"
BENCHMARKS = ["Setup J (archived)", "SPY buy & hold", "60/40 SPY/IEF"]


# --------------------------------------------------------------------------- weight rules
# Each rule returns {asset: weight} for risk assets; the engine puts 1 - sum(weights) in CASH.
def trend(m, s, months=TREND_MONTHS, assets=tuple(TREND_ASSETS)):
    """Faber-style trend allocation: 1/N in every asset above its N-month average (allocation.py)."""
    return allocation.weights_from(allocation.trend_signals(m, s, list(assets), months))


def trend_daily(m, s, days=200, assets=tuple(TREND_ASSETS)):
    """Same allocation with a daily simple average (e.g. 200 days ~ 10 months)."""
    return allocation.weights_from(allocation.hold_signals(m, s, list(assets), days))


def dual_momentum(m, s, months=12):
    """Antonacci-style: stronger of SPY / EFA over N months if it beats cash, else IEF."""
    def ret(series):
        pts = m.monthly_points(series, s, months + 1)
        return pts.iloc[-1] / pts.iloc[0] - 1 if len(pts) == months + 1 and pts.notna().all() else -np.inf
    r = {a: ret(m.close[a]) for a in ("SPY", "EFA")}
    best = max(r, key=r.get)
    return {best: 1.0} if r[best] > ret(m.cash_idx) else {"IEF": 1.0}


def vol_target(base, target=0.10, window=63):
    """Scale a rule's weights to the target annual volatility (63-day covariance), never above 100%."""
    def rule(m, s):
        w = base(m, s)
        if not w:
            return w
        r = m.rets.loc[:s, list(w)].tail(window)
        wv = np.array(list(w.values()))
        vol = float(np.sqrt(max(wv @ (r.cov().to_numpy() * metrics.PERIODS_PER_YEAR) @ wv, 1e-12)))
        k = min(1.0, target / vol)
        return {a: x * k for a, x in w.items()}
    return rule


def hold_by_default(m, s, days=200, assets=tuple(ETF12)):
    """The 12 ETFs equal weight; any ETF below its N-day average goes to cash."""
    return allocation.weights_from(allocation.hold_signals(m, s, list(assets), days))


def fixed(weights):
    return lambda m, s: dict(weights)


# --------------------------------------------------------------------------- engines
def signal_date(m, d, mode="fresh"):
    """Signal date for rebalance day d: the previous trading day ("fresh"), or the last
    month-end on or before it ("month_end": a standard month-end signal traded later).
    None if no such date exists yet (the engine then holds only the cash asset)."""
    s = m.calendar[m.pos[d] - 1]
    if mode == "month_end":
        ends = m.month_end_idx[m.month_end_idx <= s]
        return ends[-1] if len(ends) else None
    return s


def run_monthly(m, rule, *, start=START, end=END, offset=0, once=False, capital=CAPITAL, cost=COST,
                buffer=0.0, min_order=0.0, signal="fresh", invest_at_start=False, cash_as_asset=True):
    """Monthly rebalancing to rule(signal date) at the open of the (1 + offset)-th trading day.

    Orders: sells and buys move each holding to target; trades smaller than ``min_order``
    are skipped (as in the live bot); ``buffer`` of the value stays in cash; every trade pays
    ``cost`` per side. With ``cash_as_asset`` the unallocated weight is bought as CASH (BIL)
    and pays costs like any other trade; otherwise it stays as frictionless cash.
    """
    cal = m.calendar[(m.calendar >= start) & (m.calendar <= end)]
    firsts = cal.to_series().groupby(cal.to_period("M")).min()
    if once:
        rebal = {cal[0]}
    else:
        rebal = {cal[min(cal.get_loc(d) + offset, len(cal) - 1)] for d in firsts}
        if invest_at_start:
            rebal.add(cal[0])
    cash, shares = float(capital), {}
    equity, invested, orders, switches, turnover, rebalances = [], [], [], [], [], []
    for d in cal:
        if d != cal[0]:
            cash *= 1 + m.cash_ret[d]          # residual cash (buffer, rounding) earns the cash rate
        if d in rebal:
            s = signal_date(m, d, signal)
            w = dict(rule(m, s)) if s is not None else {}
            leftover = 1.0 - sum(w.values())
            if cash_as_asset and leftover > 1e-9:
                w[rd.CASH] = w.get(rd.CASH, 0.0) + leftover
            o = m.open.loc[d]
            pv = cash + sum(q * o[a] for a, q in shares.items())
            names = set(w) | set(shares)
            est_cost = cost * sum(abs(w.get(a, 0.0) * pv * (1 - buffer) - shares.get(a, 0.0) * o[a]) for a in names)
            invest = (pv - est_cost) * (1 - buffer)
            diffs = {a: w.get(a, 0.0) * invest - shares.get(a, 0.0) * o[a] for a in names}
            traded = False
            for a in sorted(names, key=lambda a: diffs[a]):          # sells first, like the live bot
                diff = diffs[a]
                if abs(diff) < max(min_order, 1e-6):
                    continue
                before = shares.get(a, 0.0)
                after = 0.0 if w.get(a, 0.0) == 0 else before + diff / o[a]
                cash -= (after - before) * o[a] + cost * abs(diff)
                orders.append((d, a, "BUY" if diff > 0 else "SELL", abs(diff)))
                turnover.append((d, abs(diff) / pv))
                if before == 0 and after > 0:
                    switches.append((d, a, "in"))
                elif before > 0 and after == 0:
                    switches.append((d, a, "out"))
                shares[a] = after
                traded = True
            shares = {a: q for a, q in shares.items() if q > 1e-12}
            if traded:
                rebalances.append(d)
        c = m.close.loc[d]
        risk = sum(q * c[a] for a, q in shares.items() if a != rd.CASH)
        total = cash + sum(q * c[a] for a, q in shares.items())
        equity.append(total)
        invested.append(risk / total)
    return {"equity": pd.Series(equity, index=cal), "invested": pd.Series(invested, index=cal),
            "orders": orders, "switches": switches, "turnover": turnover, "rebalances": rebalances}


def run_split(m, rule, offsets=TRANCHE_OFFSETS, invest_at_start=True, min_order=0.0, **kw):
    """Split rebalancing: len(offsets) equal parts that never merge, part k rebalancing on
    trading day 1 + offsets[k]. All parts invest on the first day (as the live bot does),
    then follow their own schedule. ``min_order`` is split across the parts."""
    n = len(offsets)
    parts = [run_monthly(m, rule, offset=o, capital=CAPITAL / n, invest_at_start=invest_at_start,
                         min_order=min_order / n, **kw) for o in offsets]
    equity = sum(p["equity"] for p in parts)
    invested = sum(p["invested"] * p["equity"] for p in parts) / equity
    return {"equity": equity, "invested": invested,
            "orders": sorted(o for p in parts for o in p["orders"]),
            "switches": sorted(s for p in parts for s in p["switches"]),
            "turnover": [(d, v / n) for p in parts for d, v in p["turnover"]],
            "rebalances": sorted({d for p in parts for d in p["rebalances"]})}


def run_daily(m, settings, *, start=START, end=END, trail_mult=None, cash_interest=True, cost=COST):
    """Setup J (daily EMA crossover) with the live bot's bot.evaluate() / bot.position_size().

    Signals from completed bars t-2 and t-1, fills at t's open +/- cost. Idle cash earns the
    cash rate when ``cash_interest`` (it is not traded into BIL, so it pays no cost).
    Optional trailing stop: highest high since entry - trail_mult x ATR(14), at the open.
    """
    with backtest.config_overrides(settings):
        symbols = list(config.SYMBOLS)
        frames = {s: add_indicators(m.raw[s], config) for s in symbols}
        dates = frames[symbols[0]].index
        for s in symbols[1:]:
            dates = dates.intersection(frames[s].index)
        frames = {s: frames[s].loc[dates] for s in symbols}
        i0 = max(2, int(dates.searchsorted(start)))
        i1 = int(dates.searchsorted(end, side="right"))
        rows = {s: list(frames[s].itertuples()) for s in symbols}
        ready = {s: frames[s][bot.INDICATOR_COLUMNS].notna().all(axis=1).to_numpy() for s in symbols}
        cash = float(CAPITAL)
        shares, entry, entry_cross = dict.fromkeys(symbols, 0), dict.fromkeys(symbols), dict.fromkeys(symbols)
        equity, invested, orders, switches, turnover, exits = [], [], [], [], [], []
        for i in range(i0, i1):
            if i > i0 and cash_interest:
                cash *= 1 + m.cash_ret.get(dates[i], 0.0)
            equity_open = cash + sum(shares[s] * rows[s][i].Open for s in symbols)
            for s in symbols:
                if not (ready[s][i - 2] and ready[s][i - 1]):
                    continue
                prev, last, today = rows[s][i - 2], rows[s][i - 1], rows[s][i]
                price = today.Open
                d = bot.evaluate(prev, last, price, shares[s], entry[s]["stop"] if entry[s] else None, entry_cross[s])
                signal, rules = d["signal"], list(d["exit_rules"])
                if shares[s] and trail_mult and price <= entry[s]["peak"] - trail_mult * last.atr:
                    signal, rules = "SELL", [*rules, "Trailing stop"]
                if signal == "BUY":
                    qty = bot.position_size(equity_open, cash / (1 + cost), price, last.atr)
                    if qty >= 1:
                        fill = price * (1 + cost)
                        cash -= qty * fill
                        orders.append((dates[i], s, "BUY", qty * price))
                        switches.append((dates[i], s, "in"))
                        turnover.append((dates[i], qty * price / equity_open))
                        shares[s] = qty
                        entry[s] = {"stop": fill - config.ATR_STOP_MULTIPLIER * last.atr, "peak": -np.inf}
                        entry_cross[s] = d["cross_date"]
                elif signal == "SELL":
                    value = shares[s] * price
                    cash += value * (1 - cost)
                    orders.append((dates[i], s, "SELL", value))
                    switches.append((dates[i], s, "out"))
                    turnover.append((dates[i], value / equity_open))
                    exits.append(", ".join(rules))
                    shares[s], entry[s] = 0, None
            for s in symbols:  # highest high since entry, known from the next morning on
                if shares[s]:
                    entry[s]["peak"] = max(entry[s]["peak"], rows[s][i].High)
            pos_val = sum(shares[s] * rows[s][i].Close for s in symbols)
            equity.append(cash + pos_val)
            invested.append(pos_val / (cash + pos_val))
    idx = dates[i0:i1]
    return {"equity": pd.Series(equity, index=idx), "invested": pd.Series(invested, index=idx),
            "orders": orders, "switches": switches, "turnover": turnover,
            "rebalances": sorted({o[0] for o in orders}), "exit_rules": exits}


# --------------------------------------------------------------------------- evaluation
def evaluate(res, m, start=None, end=None) -> dict:
    """metrics.summarize() on the (optionally sliced) curve plus trading-activity metrics.

    orders_per_year: executed buy/sell orders (including resizes and BIL trades)
    switches_per_year: positions opened + positions closed (each part counted separately)
    rebalances_per_year: days with at least one order
    turnover_per_year: one-way, sum(|trade value| / portfolio value) / 2 per year
    avg_invested: average share of the portfolio in risk assets (excluding BIL and cash)
    """
    full = res["equity"]
    eq = full.loc[start:end] if (start is not None or end is not None) else full
    pos = full.index.get_loc(eq.index[0])
    base = float(full.iloc[pos - 1]) if pos > 0 else float(CAPITAL)
    out = metrics.summarize(eq, m.cash_ret, base)
    years = (eq.index[-1] - (eq.index[0] - pd.Timedelta(days=1))).days / 365.25
    lo, hi = eq.index[0], eq.index[-1]
    out["orders_per_year"] = sum(1 for o in res["orders"] if lo <= o[0] <= hi) / years
    out["switches_per_year"] = sum(1 for s in res["switches"] if lo <= s[0] <= hi) / years
    out["rebalances_per_year"] = sum(1 for d in res["rebalances"] if lo <= d <= hi) / years
    out["turnover_per_year"] = sum(v for d, v in res["turnover"] if lo <= d <= hi) / 2 / years
    out["avg_invested"] = float(res["invested"].loc[lo:hi].mean())
    return out


def stress(res, year) -> dict:
    """Calendar-year return and worst drawdown within the year (peak starts at the prior year-end)."""
    eq = res["equity"]
    prior = eq.loc[:f"{year - 1}-12-31"]
    base = float(prior.iloc[-1]) if len(prior) else float(CAPITAL)
    yr = eq.loc[f"{year}-01-01":f"{year}-12-31"]
    return {"return": float(yr.iloc[-1]) / base - 1, "max_dd": metrics.max_drawdown(yr, base)}


# --------------------------------------------------------------------------- studies
def trend_live(m, **kw):
    """The live configuration: trend allocation, 4 parts, BIL cash, buffer and minimum order."""
    params = {"rule": trend, "offsets": TRANCHE_OFFSETS, **LIVE_EXECUTION}
    params.update(kw)
    return run_split(m, **params)


def approach_runs(m) -> dict:
    """The five approaches compared on the same history (in-sample selection) + benchmarks."""
    live = LIVE_EXECUTION
    return {
        "1 Trend allocation (1 part, day 1)": lambda: run_monthly(m, trend, **live),
        CHOSEN: lambda: trend_live(m),
        "2 Dual momentum (12m)": lambda: run_monthly(m, dual_momentum, **live),
        "3 Trend allocation + 10% vol target": lambda: run_monthly(m, vol_target(trend), **live),
        "4 Hold by default (12 ETFs, 200d)": lambda: run_monthly(m, hold_by_default, **live),
        "4 Hold by default (4 parts)": lambda: run_split(m, hold_by_default, **live),
        "5 Setup J + 3x ATR trailing stop": lambda: run_daily(m, {**SETUP_J, "USE_RSI_EXIT": False}, trail_mult=3.0),
        "Setup J (archived)": lambda: run_daily(m, SETUP_J),
        "SPY buy & hold": lambda: run_monthly(m, fixed({"SPY": 1.0}), once=True),
        "60/40 SPY/IEF": lambda: run_monthly(m, fixed({"SPY": 0.6, "IEF": 0.4})),
    }


def robustness_runs(m) -> list[tuple[str, str, bool, object]]:
    """(group, variant, is_standard, thunk) for the chosen strategy's robustness grid."""
    p = functools.partial
    out = []
    for months in (6, 8, 9, 10, 11, 12):
        out.append(("Average length (months)", f"{months}-month", months == TREND_MONTHS,
                    p(trend_live, m, rule=p(trend, months=months))))
    for days in (150, 200, 250):
        out.append(("Daily average instead of monthly", f"{days}-day", False,
                    p(trend_live, m, rule=p(trend_daily, days=days))))
    out.append(("Signal for parts 2-4", "21-trading-day steps", True, p(trend_live, m)))
    out.append(("Signal for parts 2-4", "last month-end signal", False, p(trend_live, m, signal="month_end")))
    schedules = [("1 part, day 1", (0,)), ("1 part, day 6", (5,)), ("1 part, day 11", (10,)),
                 ("1 part, day 16", (15,)), ("2 parts, days 1/11", (0, 10)),
                 ("4 parts, days 1/6/11/16", TRANCHE_OFFSETS), ("4 parts, days 3/8/13/18", (2, 7, 12, 17))]
    for label, offsets in schedules:
        out.append(("Rebalance schedule", label, offsets == TRANCHE_OFFSETS, p(trend_live, m, offsets=offsets)))
    for bps in (0, 5, 10, 25):
        out.append(("Cost per side", f"{bps} bp", bps / 10_000 == COST, p(trend_live, m, cost=bps / 10_000)))
    out.append(("Universe", f"{len(TREND_ASSETS)} assets (live)", True, p(trend_live, m)))
    out.append(("Universe", "Faber original 5: " + " ".join(FABER_ORIGINAL), False,
                p(trend_live, m, rule=p(trend, assets=tuple(FABER_ORIGINAL)))))
    for drop in TREND_ASSETS:
        rest = tuple(a for a in TREND_ASSETS if a != drop)
        out.append(("Universe", f"without {drop}", False, p(trend_live, m, rule=p(trend, assets=rest))))
    out.append(("Cash handling", "BIL, 0.05% cost, 0.5% buffer, $25 minimum", True, p(trend_live, m)))
    out.append(("Cash handling", "frictionless cash, no buffer, no minimum", False,
                p(trend_live, m, cash_as_asset=False, buffer=0.0, min_order=0.0)))
    return out


def selection_check_runs(m) -> list[tuple[str, object]]:
    """Re-run of the selection rule's inputs with the current engine: approach 4 (hold by
    default) by rebalance day and as 4 parts with nearby parameters."""
    p = functools.partial
    live = LIVE_EXECUTION
    out = [(f"Hold by default, 1 part, day {o + 1}", p(run_monthly, m, hold_by_default, offset=o, **live))
           for o in (0, 5, 10, 15)]
    out += [("Hold by default, 4 parts, 200-day", p(run_split, m, hold_by_default, **live)),
            ("Hold by default, 4 parts, 150-day", p(run_split, m, p(hold_by_default, days=150), **live)),
            ("Hold by default, 4 parts, 250-day", p(run_split, m, p(hold_by_default, days=250), **live)),
            ("Hold by default, 4 parts, days 3/8/13/18",
             p(run_split, m, hold_by_default, offsets=(2, 7, 12, 17), **live))]
    return out


def weekly(series: pd.Series) -> pd.Series:
    return series.resample("W-FRI").last().dropna()


# --------------------------------------------------------------------------- output helpers
def pct(x, d=1, signed=False) -> str:
    if x is None or pd.isna(x):
        return "–"
    s = f"{x * 100:+.{d}f}%" if signed else f"{x * 100:.{d}f}%"
    return s.replace("-", "−")


def num(x, d=2) -> str:
    return "–" if x is None or pd.isna(x) else f"{x:.{d}f}".replace("-", "−")


def metrics_table(rows: dict, cols=("cagr", "vol", "sharpe", "sortino", "max_dd", "calmar", "worst_year")) -> str:
    heads = {"cagr": "CAGR", "vol": "Volatility", "sharpe": "Sharpe", "sortino": "Sortino", "max_dd": "Max DD",
             "calmar": "Calmar", "worst_year": "Worst year", "total_return": "Total return",
             "turnover_per_year": "Turnover/yr", "orders_per_year": "Orders/yr", "switches_per_year": "Switches/yr",
             "avg_invested": "Avg invested", "downside_dev": "Downside dev", "monthly_win_rate": "Positive months"}
    fmt = {"sharpe": num, "sortino": num, "calmar": num, "orders_per_year": lambda x: num(x, 1),
           "switches_per_year": lambda x: num(x, 1)}
    lines = ["| Strategy | " + " | ".join(heads[c] for c in cols) + " |",
             "|---|" + "---:|" * len(cols)]
    for name, r in rows.items():
        cells = []
        for c in cols:
            v = r[c]
            cell = fmt.get(c, pct)(v)
            if c == "worst_year" and r.get("worst_year_label"):
                cell += f" ({r['worst_year_label']})"
            cells.append(cell)
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- main
def main(argv=None):
    import research_charts as charts

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="use data/cache/ instead of downloading")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows consoles default to cp125x
    os.makedirs(OUT_DIR, exist_ok=True)

    print(f"Data: {'cache' if args.offline else 'download'} for {len(TICKERS)} tickers + {rd.RATE_TICKER}, "
          f"end {END.date()}")
    raw, manifest = rd.get_data(TICKERS, offline=args.offline)
    m = rd.Market(raw)
    print(f"  combined SHA-256 {manifest['combined_sha256'][:16]}..., {manifest.get('verification', 'new manifest')}")

    # 1-2. approaches (in-sample selection) and benchmarks
    results = {}
    for name, fn in approach_runs(m).items():
        print(f"  running {name}")
        results[name] = fn()
    main_names = [CHOSEN, *BENCHMARKS]
    periods = {"Full period": (None, None),
               f"{START.year}-2014": (None, SPLIT_DATE - pd.Timedelta(days=1)),
               f"2015-{END.year}": (SPLIT_DATE, None)}
    summary = [{"period": p, "strategy": n, **evaluate(r, m, a, b)}
               for p, (a, b) in periods.items() for n, r in results.items()]
    pd.DataFrame(summary).to_csv(os.path.join(OUT_DIR, "summary.csv"), index=False)
    full = {r["strategy"]: r for r in summary if r["period"] == "Full period"}

    # 3. stress years, yearly returns
    stress_rows = [{"strategy": n, "year": y, **stress(results[n], y)} for n in results for y in STRESS_YEARS]
    pd.DataFrame(stress_rows).to_csv(os.path.join(OUT_DIR, "stress.csv"), index=False)
    pd.DataFrame({n: metrics.calendar_returns(r["equity"], "YE", CAPITAL) for n, r in results.items()}).to_csv(
        os.path.join(OUT_DIR, "yearly.csv"))

    # 4. robustness grid
    robust = []
    for group, variant, standard, thunk in robustness_runs(m):
        print(f"  robustness: {group} / {variant}")
        res = thunk()
        row = {"group": group, "variant": variant, "standard": standard, **evaluate(res, m)}
        for p, (a, b) in list(periods.items())[1:]:
            row[f"sharpe {p}"] = evaluate(res, m, a, b)["sharpe"]
        robust.append(row)
    robust_df = pd.DataFrame(robust)
    robust_df.to_csv(os.path.join(OUT_DIR, "robustness.csv"), index=False)

    # 4b. selection rule inputs re-checked with the current engine
    selection = [{"variant": label, **evaluate(thunk(), m)} for label, thunk in selection_check_runs(m)]
    pd.DataFrame(selection).to_csv(os.path.join(OUT_DIR, "selection_check.csv"), index=False)

    # 5. regimes (labels from information up to the previous close)
    spy = m.close["SPY"]
    labels = {"Trend": regimes.trend_regime(spy), "Volatility": regimes.volatility_regime(spy)}
    regime_rows = []
    for kind, lab in labels.items():
        for n in main_names:
            r = metrics.daily_returns(results[n]["equity"], CAPITAL)
            for regime, st in regimes.regime_stats(r, lab, m.cash_ret).iterrows():
                regime_rows.append({"regime_type": kind, "regime": regime, "strategy": n, **st.to_dict()})
    regime_df = pd.DataFrame(regime_rows)
    regime_df.to_csv(os.path.join(OUT_DIR, "regimes.csv"), index=False)

    # 6. curves for charts and the dashboard (weekly to keep the files small)
    eq_weekly = pd.DataFrame({n: weekly(r["equity"]) for n, r in results.items()})
    eq_weekly.round(2).to_csv(os.path.join(OUT_DIR, "equity_weekly.csv"))
    rolling = {}
    for n in main_names:
        r = metrics.daily_returns(results[n]["equity"], CAPITAL)
        rolling[f"{n} | vol 63d"] = metrics.rolling_volatility(r, 63)
        rolling[f"{n} | sharpe 252d"] = metrics.rolling_sharpe(r, m.cash_ret, 252)
    pd.DataFrame({k: weekly(v) for k, v in rolling.items()}).round(4).to_csv(
        os.path.join(OUT_DIR, "rolling_weekly.csv"))

    # 7. charts
    charts.equity_main(results, main_names, os.path.join(OUT_DIR, "equity_main.png"), CAPITAL)
    charts.drawdowns(results, main_names, os.path.join(OUT_DIR, "drawdowns_main.png"))
    charts.rolling(rolling, main_names, os.path.join(OUT_DIR, "rolling.png"))
    charts.robustness(robust_df, os.path.join(OUT_DIR, "robustness.png"))
    charts.regime_bars(regime_df, main_names, os.path.join(OUT_DIR, "regimes.png"))
    approach_names = [n for n in results if n[0].isdigit() or n == CHOSEN] + ["SPY buy & hold", "60/40 SPY/IEF"]
    charts.approaches(results, approach_names, os.path.join(OUT_DIR, "approaches.png"))

    # 8. markdown tables (copied into docs/research.md and the README)
    md = [f"# Generated research tables\n\nGenerated by `python research.py` from data ending {END.date()} "
          f"(combined input SHA-256 `{manifest['combined_sha256']}`). Do not edit by hand.\n"]
    for p in periods:
        rows = {r["strategy"]: r for r in summary if r["period"] == p and r["strategy"] in main_names}
        md += [f"## Chosen strategy vs. benchmarks: {p}\n",
               metrics_table(rows, ("cagr", "vol", "sharpe", "sortino", "max_dd", "calmar", "worst_year",
                                    "total_return")) + "\n"]
    md += ["## Activity (full period)\n",
           metrics_table({n: full[n] for n in main_names}, ("orders_per_year", "switches_per_year",
                                                            "turnover_per_year", "avg_invested",
                                                            "monthly_win_rate", "downside_dev")) + "\n"]
    md += ["## Approach comparison (in-sample model selection, full period)\n",
           metrics_table({n: full[n] for n in results}, ("cagr", "vol", "sharpe", "sortino", "max_dd", "calmar",
                                                         "worst_year", "switches_per_year")) + "\n"]
    md += ["## Stress years (calendar-year return / max drawdown within the year)\n",
           "| Strategy | " + " | ".join(str(y) for y in STRESS_YEARS) + " |",
           "|---|" + "---|" * len(STRESS_YEARS)]
    for n in results:
        cells = [f"{pct(s['return'], signed=True)} / {pct(s['max_dd'])}"
                 for s in stress_rows if s["strategy"] == n]
        md.append(f"| {n} | " + " | ".join(cells) + " |")
    md += ["\n## Robustness grid (chosen strategy, full period)\n",
           "| Group | Variant | CAGR | Volatility | Sharpe | Sortino | Max DD | Turnover/yr | "
           f"Sharpe {START.year}-2014 | Sharpe 2015-{END.year} |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in robust:
        star = " *" if r["standard"] else ""
        md.append(f"| {r['group']} | {r['variant']}{star} | {pct(r['cagr'])} | {pct(r['vol'])} | {num(r['sharpe'])} | "
                  f"{num(r['sortino'])} | {pct(r['max_dd'])} | {pct(r['turnover_per_year'], 0)} | "
                  f"{num(r[f'sharpe {START.year}-2014'])} | {num(r[f'sharpe 2015-{END.year}'])} |")
    md.append("\n`*` = live configuration.\n")
    ref = full["60/40 SPY/IEF"]["sharpe"]
    md += ["## Selection rule re-checked (approach 4, current engine)\n",
           f"Rule: approach 4 only if every 4-part variant has max DD >= -22% and Sharpe > 60/40 ({num(ref)}).\n",
           "| Variant | CAGR | Sharpe | Max DD | Passes |", "|---|---:|---:|---:|---|"]
    for r in selection:
        passes = "" if "1 part" in r["variant"] else ("yes" if r["max_dd"] >= -0.22 and r["sharpe"] > ref else "no")
        md.append(f"| {r['variant']} | {pct(r['cagr'])} | {num(r['sharpe'])} | {pct(r['max_dd'])} | {passes} |")
    md.append("")
    md += ["## Market regimes (daily returns, labels from the previous close)\n",
           "| Regime | Strategy | Share of days | Ann. mean return | Ann. compounded return | Ann. volatility | "
           "Sharpe | Positive days |",
           "|---|---|---:|---:|---:|---:|---:|---:|"]
    for _, r in regime_df.iterrows():
        md.append(f"| {r['regime_type']}: {r['regime']} | {r['strategy']} | {pct(r['share_of_days'], 0)} | "
                  f"{pct(r['ann_return'])} | {pct(r['ann_compounded'])} | {pct(r['ann_vol'])} | {num(r['sharpe'])} | "
                  f"{pct(r['positive_days'], 0)} |")
    with open(os.path.join(OUT_DIR, "tables.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    print("\n" + metrics_table({n: full[n] for n in main_names}))
    print(f"\nOutputs in {OUT_DIR}")
    return results, m


if __name__ == "__main__":
    main()
