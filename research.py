"""Long-history research: are there more robust approaches than the live setup?

    python research.py      # writes docs/research/ (CSVs + charts) and backtests/research_report.html

Read-only with respect to the live bot: config.py, bot.py and bot_state.json are never
changed. The trend and hold-by-default rules come from allocation.py, which the live bot
also uses; setup J variants run through bot.evaluate() and bot.position_size() with
temporary in-memory overrides. Split rebalancing (run_split) divides the portfolio into
4 parts rebalancing on trading days 1, 6, 11 and 16, like the live allocation mode.

Approaches (standard, un-optimized parameters):
  1. Trend allocation (Faber): SPY EFA IEF TLT GLD DBC VNQ, equal weight in each asset
     whose month-end close is above its 10-month average, the rest in cash.
  2. Dual momentum (Antonacci): stronger of SPY / EFA by 12-month return if it beats
     cash, otherwise IEF.
  3. (1) + volatility targeting: scale the whole portfolio to ~10% annual volatility
     (63-day covariance), never above 100% invested.
  4. Hold by default: the 12 live ETFs equal weight; any ETF below its 200-day average
     sits in cash.
  5. Setup J with a 3x ATR trailing stop (highest high since entry - 3 x ATR14)
     replacing the RSI > 75 exit.
Benchmarks: SPY buy and hold, 60/40 SPY/IEF rebalanced monthly, setup J.

Rules: dividend-adjusted prices; 1-4 and 60/40 rebalance at the open of the first
trading day of each month using closes through the previous month-end (no look-ahead);
J variants trade daily at the open from bars through the previous close; 0.05% cost per
side on every buy and sell; idle cash earns BIL's return, or the 3-month T-bill rate
(^IRX) before BIL existed, with no trading cost.
"""
import base64
import io
import math
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

import allocation  # noqa: E402
import backtest  # noqa: E402
import bot  # noqa: E402
import config  # noqa: E402
from indicators import add_indicators  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "docs", "research")
REPORT = os.path.join(HERE, "backtests", "research_report.html")

# DBC (commodities) starts 2006-02-06, the latest of all inputs. Its 11th month-end close
# is Dec 2006, so 9/10/11-month averages are all defined at the first rebalance:
# the first trading day of 2007 (Jan 2 2007 was a market holiday).
START = pd.Timestamp("2007-01-03")
SPLIT = pd.Timestamp("2015-01-01")
STRESS_YEARS = [2008, 2020, 2022]
COST = 0.0005
CAPITAL = 100_000
GTAA = ["SPY", "EFA", "IEF", "TLT", "GLD", "DBC", "VNQ"]
ETF12 = backtest.WATCHLIST_12
SETUP_J = backtest.CANDIDATES["J: EMA 10/30, crossover only, 12 ETFs"]
TICKERS = sorted(set(GTAA + ETF12 + ["BIL"]))


# --------------------------------------------------------------------------- data
def load_history(tickers):
    raw = {}
    for t in tickers:
        df = yf.Ticker(t).history(period="max", interval="1d", auto_adjust=True)
        if df.empty:
            raise RuntimeError(f"no data for {t}")
        df = df[["Open", "High", "Low", "Close", "Volume"]]
        df.index = df.index.tz_localize(None).normalize()
        raw[t] = df[~df.index.duplicated()]
    return raw


class Market(allocation.Prices):
    """Aligned daily prices on the NYSE calendar (SPY's trading days) plus the cash return.

    Month-end / moving-average helpers come from allocation.Prices, shared with the live bot.
    """

    def __init__(self, raw):
        self.raw = raw
        calendar = raw["SPY"].index
        super().__init__(pd.DataFrame({t: d["Close"] for t, d in raw.items()}).reindex(calendar).ffill(limit=5))
        self.open = pd.DataFrame({t: d["Open"] for t, d in raw.items()}).reindex(calendar).ffill(limit=5)
        self.rets = self.close.pct_change(fill_method=None)
        self.cash_ret = self._cash_returns()
        self.cash_idx = (1 + self.cash_ret).cumprod()

    def _cash_returns(self):
        irx = yf.Ticker("^IRX").history(period="max")["Close"]
        irx.index = irx.index.tz_localize(None).normalize()
        irx = irx[~irx.index.duplicated()].clip(lower=0).reindex(self.calendar).ffill()
        days = self.calendar.to_series().diff().dt.days
        tbill = irx.shift(1) / 100 * days / 360          # T-bill discount rate, accrued per calendar day
        bil = self.rets["BIL"]                           # BIL total return once it trades
        self.bil_start = self.raw["BIL"].index[0]
        return bil.where(bil.notna(), tbill).fillna(0.0)


# --------------------------------------------------------------------------- weight rules
def faber(m, s, months=10, assets=GTAA):
    """Equal weight in every asset whose close is above its N-month average (allocation.py)."""
    return allocation.weights_from(allocation.trend_signals(m, s, assets, months))


def dual_momentum(m, s, months=12):
    """Stronger of SPY / EFA over N months if it beats cash, else IEF."""
    def ret(series):
        pts = m.monthly_points(series, s, months + 1)
        return pts.iloc[-1] / pts.iloc[0] - 1 if len(pts) == months + 1 and pts.notna().all() else -np.inf
    r = {a: ret(m.close[a]) for a in ("SPY", "EFA")}
    best = max(r, key=r.get)
    return {best: 1.0} if r[best] > ret(m.cash_idx) else {"IEF": 1.0}


def vol_target(base, target=0.10, window=63):
    """Scale a weight rule's portfolio to the target volatility, never above 100% invested."""
    def rule(m, s):
        w = base(m, s)
        if not w:
            return w
        r = m.rets.loc[:s, list(w)].tail(window)
        wv = np.array(list(w.values()))
        vol = math.sqrt(max(wv @ (r.cov().values * 252) @ wv, 1e-12))
        k = min(1.0, target / vol)
        return {a: x * k for a, x in w.items()}
    return rule


def hold_by_default(m, s, days=200, assets=ETF12):
    """Equal weight in every ETF above its N-day average (allocation.py)."""
    return allocation.weights_from(allocation.hold_signals(m, s, assets, days))


def fixed(weights):
    return lambda m, s: dict(weights)


# --------------------------------------------------------------------------- engines
def run_monthly(m, rule, start=START, end=None, offset=0, once=False, capital=CAPITAL):
    """Rebalance to rule(signal date) at the open of the first trading day of each month
    (+offset trading days); signals use closes through the previous trading day."""
    cal = m.calendar[(m.calendar >= start) & (m.calendar <= (end or m.calendar[-1]))]
    firsts = cal.to_series().groupby(cal.to_period("M")).min()
    rebal = {cal[min(cal.get_loc(d) + offset, len(cal) - 1)] for d in firsts} if not once else {cal[0]}
    cash, shares = float(capital), {}
    equity, invested, trades, turnover = [], [], [], []
    for d in cal:
        if d != cal[0]:
            cash *= 1 + m.cash_ret[d]
        if d in rebal:
            s = m.calendar[m.pos[d] - 1]
            w = rule(m, s)
            o = m.open.loc[d]
            pv = cash + sum(q * o[a] for a, q in shares.items())
            names = set(w) | set(shares)
            est_cost = COST * sum(abs(w.get(a, 0) * pv - shares.get(a, 0) * o[a]) for a in names)
            pv_net = pv - est_cost
            for a in sorted(names):
                target = w.get(a, 0) * pv_net / o[a]
                trade = target - shares.get(a, 0.0)
                value = abs(trade) * o[a]
                if value < 1e-6:
                    continue
                cash -= trade * o[a] + COST * value
                turnover.append((d, value / pv))
                if shares.get(a, 0) == 0 and target > 0:
                    trades.append((d, a, "BUY"))
                elif target == 0:
                    trades.append((d, a, "SELL"))
                shares[a] = target
            shares = {a: q for a, q in shares.items() if q > 0}
        c = m.close.loc[d]
        pos_val = sum(q * c[a] for a, q in shares.items())
        equity.append(cash + pos_val)
        invested.append(pos_val / (cash + pos_val))
    return {"equity": pd.Series(equity, index=cal), "invested": pd.Series(invested, index=cal),
            "trades": trades, "turnover": turnover}


def run_split(m, rule, offsets=(0, 5, 10, 15), start=START):
    """Split rebalancing: len(offsets) equal parts, each rebalancing monthly on its own
    trading day (offset 0 = 1st trading day, 5 = 6th, ...). Parts are never merged, so
    each drifts on its own, exactly like separate sub-accounts."""
    parts = [run_monthly(m, rule, start=start, offset=o, capital=CAPITAL / len(offsets)) for o in offsets]
    equity = sum(p["equity"] for p in parts)
    invested = sum(p["invested"] * p["equity"] for p in parts) / equity
    n = len(offsets)
    return {"equity": equity, "invested": invested,
            "trades": sorted(t for p in parts for t in p["trades"]),
            "turnover": [(d, v / n) for p in parts for d, v in p["turnover"]]}


def run_daily(m, settings, start=START, end=None, trail_mult=None, cash_interest=True):
    """Setup J-style daily strategy with the live bot's evaluate()/position_size().

    Same rules as backtest.simulate, plus idle cash interest and an optional trailing
    stop (highest high since entry - trail_mult x ATR, checked at the open)."""
    with backtest.config_overrides(settings):
        symbols = list(config.SYMBOLS)
        frames = {s: add_indicators(m.raw[s], config) for s in symbols}
        dates = frames[symbols[0]].index
        for s in symbols[1:]:
            dates = dates.intersection(frames[s].index)
        frames = {s: frames[s].loc[dates] for s in symbols}
        i0 = max(2, int(dates.searchsorted(start)))
        i1 = int(dates.searchsorted(end or dates[-1], side="right"))
        rows = {s: list(frames[s].itertuples()) for s in symbols}
        ready = {s: frames[s][bot.INDICATOR_COLUMNS].notna().all(axis=1).to_numpy() for s in symbols}
        cash = float(CAPITAL)
        shares, entry, entry_cross = dict.fromkeys(symbols, 0), dict.fromkeys(symbols), dict.fromkeys(symbols)
        equity, invested, trades, turnover, exits = [], [], [], [], []
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
                    signal, rules = "SELL", rules + ["Trailing stop"]
                if signal == "BUY":
                    qty = bot.position_size(equity_open, cash / (1 + COST), price, last.atr)
                    if qty >= 1:
                        fill = price * (1 + COST)
                        cash -= qty * fill
                        turnover.append((dates[i], qty * fill / equity_open))
                        shares[s] = qty
                        entry[s] = {"stop": fill - config.ATR_STOP_MULTIPLIER * last.atr, "peak": -np.inf}
                        entry_cross[s] = d["cross_date"]
                        trades.append((dates[i], s, "BUY"))
                elif signal == "SELL":
                    value = shares[s] * price
                    cash += value * (1 - COST)
                    turnover.append((dates[i], value / equity_open))
                    trades.append((dates[i], s, "SELL"))
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
            "trades": trades, "turnover": turnover, "exit_rules": exits}


# --------------------------------------------------------------------------- metrics
def stats(res, m, start=None, end=None):
    eq = res["equity"]
    if start is not None or end is not None:
        eq = eq.loc[start:end]
    base = res["equity"].loc[:eq.index[0]].iloc[-2] if eq.index[0] != res["equity"].index[0] else CAPITAL
    curve = pd.concat([pd.Series([base], index=[eq.index[0] - pd.Timedelta(days=1)]), eq])
    r = curve.pct_change().dropna()
    excess = r - m.cash_ret.reindex(r.index).fillna(0)
    years = (eq.index[-1] - curve.index[0]).days / 365.25
    yearly = curve.groupby(curve.index.year).last()
    yr_ret = (yearly / yearly.shift(1) - 1).dropna()
    full_years = yr_ret[[y for y in yr_ret.index if y < m.calendar[-1].year]]
    n_trades = sum(1 for t in res["trades"] if eq.index[0] <= t[0] <= eq.index[-1])
    dd = curve / curve.cummax() - 1
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1
    return {
        "cagr": cagr, "vol": r.std() * math.sqrt(252),
        "sharpe": excess.mean() / excess.std() * math.sqrt(252) if excess.std() > 0 else float("nan"),
        "max_dd": dd.min(), "calmar": cagr / abs(dd.min()) if dd.min() < 0 else float("nan"),
        "worst_year": full_years.min() if len(full_years) else float("nan"),
        "worst_year_label": int(full_years.idxmin()) if len(full_years) else None,
        "trades_per_year": n_trades / years,
        "turnover_per_year": sum(v for d, v in res["turnover"] if eq.index[0] <= d <= eq.index[-1]) / years,
        "avg_invested": res["invested"].loc[eq.index[0]:eq.index[-1]].mean(),
    }


def stress(res, year):
    eq = res["equity"]
    prior = eq.loc[:f"{year - 1}-12-31"]
    base = prior.iloc[-1] if len(prior) else CAPITAL
    yr = eq.loc[f"{year}-01-01":f"{year}-12-31"]
    curve = pd.concat([pd.Series([base]), yr.reset_index(drop=True)])
    return {"return": yr.iloc[-1] / base - 1, "max_dd": (curve / curve.cummax() - 1).min()}


def yearly_table(res):
    eq = res["equity"]
    ye = eq.groupby(eq.index.year).last()
    return ye / ye.shift(1).fillna(CAPITAL) - 1


# --------------------------------------------------------------------------- charts
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
BENCH_STYLE = {"SPY buy & hold": ("#3d3c39", "-"), "60/40 SPY/IEF": ("#8a8986", "--"),
               "Setup J (live)": ("#8a8986", ":")}
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def style_axes(ax):
    ax.grid(color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#b9b8b2")
    ax.tick_params(colors=MUTED, labelsize=8)


def money_log_axis(ax, lo, hi):
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v / 1000:,.0f}k"))
    ax.yaxis.set_minor_formatter(mticker.NullFormatter())
    ax.yaxis.set_minor_locator(mticker.NullLocator())
    ticks = [t * 1000 for t in (50, 75, 100, 150, 200, 300, 400, 600, 800, 1200)]
    ax.set_yticks([t for t in ticks if lo * 0.85 <= t <= hi * 1.15])


def save(fig, name):
    os.makedirs(OUT_DIR, exist_ok=True)
    fig.savefig(os.path.join(OUT_DIR, name), bbox_inches="tight", facecolor="white")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def color_of(name, names):
    if name in BENCH_STYLE:
        return BENCH_STYLE[name]
    return SLOTS[names.index(name) % len(SLOTS)], "-"


def equity_chart(results, approaches):
    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=120)
    order = sorted(results, key=lambda n: -results[n]["equity"].iloc[-1])
    for name in order:
        color, ls = color_of(name, approaches)
        eq = results[name]["equity"]
        ax.plot(eq.index, eq.values, color=color, ls=ls, lw=2 if name in approaches else 1.4,
                label=f"{name}  (${eq.iloc[-1] / 1000:,.0f}k)")
    money_log_axis(ax, min(r["equity"].min() for r in results.values()),
                   max(r["equity"].max() for r in results.values()))
    style_axes(ax)
    ax.set_title(f"Growth of $100k, {START:%b %Y} – {results[order[0]]['equity'].index[-1]:%b %Y} (log scale)",
                 loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=8, loc="upper left", labelcolor=MUTED)
    return save(fig, "equity_log.png")


def split_chart(curves):
    """Chosen strategy (split rebalancing) vs. alternative and benchmarks, log scale."""
    styles = {0: (SLOTS[0], "-", 2.2), 1: (SLOTS[3], "-", 1.6), 2: BENCH_STYLE["60/40 SPY/IEF"] + (1.4,),
              3: BENCH_STYLE["SPY buy & hold"] + (1.4,)}
    fig, ax = plt.subplots(figsize=(10, 4.8), dpi=120)
    for i, (name, eq) in enumerate(curves.items()):
        color, ls, lw = styles[i]
        ax.plot(eq.index, eq.values, color=color, ls=ls, lw=lw, label=f"{name}  (${eq.iloc[-1] / 1000:,.0f}k)")
    money_log_axis(ax, min(c.min() for c in curves.values()), max(c.max() for c in curves.values()))
    style_axes(ax)
    first = next(iter(curves.values()))
    ax.set_title(f"Split rebalancing (4 parts): growth of $100k, {first.index[0]:%b %Y} – {first.index[-1]:%b %Y}",
                 loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=8, loc="upper left", labelcolor=MUTED)
    return save(fig, "split_equity.png")


def drawdown_chart(results, approaches):
    names = approaches + [n for n in results if n not in approaches]
    fig, axes = plt.subplots(4, 2, figsize=(10, 8), dpi=120, sharex=True, sharey=True)
    for ax, name in zip(axes.flat, names):
        eq = results[name]["equity"]
        dd = eq / eq.cummax() - 1
        color, _ = color_of(name, approaches)
        ax.fill_between(dd.index, dd.values * 100, 0, color=color, alpha=0.35, lw=0)
        ax.plot(dd.index, dd.values * 100, color=color, lw=1)
        ax.set_title(f"{name}   worst {dd.min() * 100:.0f}%", loc="left", fontsize=9, color=INK)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
        style_axes(ax)
    fig.suptitle("Drawdown from previous peak", x=0.01, ha="left", fontsize=11, color=INK)
    fig.tight_layout()
    return save(fig, "drawdowns.png")


def sensitivity_chart(groups, approaches):
    fig, axes = plt.subplots(len(groups), 1, figsize=(10, 3.0 * len(groups)), dpi=120, sharex=True)
    greys = ["#3d3c39", "#8a8986", "#6f6e6a", "#b9b8b2", "#52514e", "#a3a29c"]
    dashes = ["--", ":", "-.", (0, (5, 1, 1, 1)), (0, (1, 3)), (0, (8, 2))]
    for ax, (approach, variants) in zip(np.atleast_1d(axes), groups.items()):
        k = 0
        for label, res in variants.items():
            eq = res["equity"]
            if label.endswith("(standard)"):
                ax.plot(eq.index, eq.values, color=SLOTS[approaches.index(approach)], lw=2.2, label=label, zorder=3)
            else:
                ax.plot(eq.index, eq.values, color=greys[k % 6], ls=dashes[k % 6], lw=1.2, label=label)
                k += 1
        money_log_axis(ax, min(r["equity"].min() for r in variants.values()),
                       max(r["equity"].max() for r in variants.values()))
        ax.set_title(approach, loc="left", fontsize=10, color=INK)
        ax.legend(frameon=False, fontsize=7.5, loc="upper left", labelcolor=MUTED, ncol=2)
        style_axes(ax)
    fig.tight_layout()
    return save(fig, "sensitivity.png")


# --------------------------------------------------------------------------- report helpers
def pct(x, d=1, signed=False):
    return "–" if x is None or pd.isna(x) else (f"{x * 100:+.{d}f}%" if signed else f"{x * 100:.{d}f}%")


def num(x, d=2):
    return "–" if x is None or pd.isna(x) else f"{x:.{d}f}"


def metrics_rows(table):
    head = ("<tr><th>Strategy</th><th>CAGR</th><th>Volatility</th><th>Sharpe</th><th>Max DD</th>"
            "<th>Worst year</th><th>Trades/yr</th><th>Turnover/yr</th><th>Avg invested</th></tr>")
    body = "".join(
        f"<tr><td>{n}</td><td>{pct(s['cagr'])}</td><td>{pct(s['vol'])}</td><td>{num(s['sharpe'])}</td>"
        f"<td>{pct(s['max_dd'])}</td><td>{pct(s['worst_year'])} ({s['worst_year_label']})</td>"
        f"<td>{num(s['trades_per_year'], 1)}</td><td>{pct(s['turnover_per_year'], 0)}</td>"
        f"<td>{pct(s['avg_invested'], 0)}</td></tr>" for n, s in table.items())
    return f"<div class='scroll'><table><thead>{head}</thead><tbody>{body}</tbody></table></div>"


CSS = """
body{font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:#0b0b0b;background:#fcfcfb;margin:0}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}
h2{font-size:17px;margin:32px 0 8px;border-bottom:1px solid #e4e3df;padding-bottom:4px}
.muted{color:#52514e}.note{background:#f4f3f0;border-radius:8px;padding:10px 14px}
table{border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13px}
th,td{padding:4px 9px;text-align:right;border-bottom:1px solid #eeede9;white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:#52514e;font-weight:600}
.scroll{overflow-x:auto}img{max-width:100%}
"""


# --------------------------------------------------------------------------- main
def main():
    print(f"Downloading full history for {', '.join(TICKERS)} and ^IRX...")
    m = Market(load_history(TICKERS))
    end = m.calendar[-1]

    approaches = {
        "1 Trend allocation (Faber 10m)": lambda: run_monthly(m, faber),
        "2 Dual momentum (12m)": lambda: run_monthly(m, dual_momentum),
        "3 Trend allocation + 10% vol target": lambda: run_monthly(m, vol_target(faber)),
        "4 Hold by default (12 ETFs, 200d)": lambda: run_monthly(m, hold_by_default),
        "5 Setup J + 3x ATR trailing stop": lambda: run_daily(m, {**SETUP_J, "USE_RSI_EXIT": False}, trail_mult=3.0),
    }
    benchmarks = {
        "SPY buy & hold": lambda: run_monthly(m, fixed({"SPY": 1.0}), once=True),
        "60/40 SPY/IEF": lambda: run_monthly(m, fixed({"SPY": 0.6, "IEF": 0.4})),
        "Setup J (live)": lambda: run_daily(m, SETUP_J),
    }
    results = {}
    for name, fn in {**approaches, **benchmarks}.items():
        print(f"  running {name}")
        results[name] = fn()
    names = list(approaches)

    sensitivity = {
        names[0]: {
            "9-month average": lambda: run_monthly(m, lambda mm, s: faber(mm, s, 9)),
            "10-month average (standard)": lambda: results[names[0]],
            "11-month average": lambda: run_monthly(m, lambda mm, s: faber(mm, s, 11)),
            "10-month, rebalance 5th trading day": lambda: run_monthly(m, faber, offset=5),
            "10-month, rebalance mid-month": lambda: run_monthly(m, faber, offset=10),
            "10-month, rebalance 15th trading day": lambda: run_monthly(m, faber, offset=15),
        },
        names[1]: {
            "6-month momentum": lambda: run_monthly(m, lambda mm, s: dual_momentum(mm, s, 6)),
            "9-month momentum": lambda: run_monthly(m, lambda mm, s: dual_momentum(mm, s, 9)),
            "12-month momentum (standard)": lambda: results[names[1]],
            "12-month, rebalance mid-month": lambda: run_monthly(m, dual_momentum, offset=10),
        },
        names[2]: {
            "8% target": lambda: run_monthly(m, vol_target(faber, 0.08)),
            "10% target, 63-day (standard)": lambda: results[names[2]],
            "12% target": lambda: run_monthly(m, vol_target(faber, 0.12)),
            "10% target, 21-day window": lambda: run_monthly(m, vol_target(faber, 0.10, 21)),
            "10% target, 126-day window": lambda: run_monthly(m, vol_target(faber, 0.10, 126)),
        },
        names[3]: {
            "150-day average": lambda: run_monthly(m, lambda mm, s: hold_by_default(mm, s, 150)),
            "200-day average (standard)": lambda: results[names[3]],
            "250-day average": lambda: run_monthly(m, lambda mm, s: hold_by_default(mm, s, 250)),
            "200-day, rebalance 5th trading day": lambda: run_monthly(m, hold_by_default, offset=5),
            "200-day, rebalance mid-month": lambda: run_monthly(m, hold_by_default, offset=10),
            "200-day, rebalance 15th trading day": lambda: run_monthly(m, hold_by_default, offset=15),
        },
        names[4]: {
            "2.5x ATR trail": lambda: run_daily(m, {**SETUP_J, "USE_RSI_EXIT": False}, trail_mult=2.5),
            "3x ATR trail (standard)": lambda: results[names[4]],
            "3.5x ATR trail": lambda: run_daily(m, {**SETUP_J, "USE_RSI_EXIT": False}, trail_mult=3.5),
            "EMA 9/27, 3x trail": lambda: run_daily(m, {**SETUP_J, "FAST_MA": 9, "SLOW_MA": 27,
                                                         "USE_RSI_EXIT": False}, trail_mult=3.0),
            "EMA 11/33, 3x trail": lambda: run_daily(m, {**SETUP_J, "FAST_MA": 11, "SLOW_MA": 33,
                                                          "USE_RSI_EXIT": False}, trail_mult=3.0),
        },
    }
    sens_results = {}
    for approach, variants in sensitivity.items():
        print(f"  sensitivity: {approach}")
        sens_results[approach] = {label: fn() for label, fn in variants.items()}

    # ---- split rebalancing: 4 parts rebalancing on trading days 1, 6, 11 and 16
    print("  split rebalancing")
    shifted = (2, 7, 12, 17)
    split_results = {
        "1 Trend allocation, split": {
            "10-month (standard)": run_split(m, faber),
            "9-month": run_split(m, lambda mm, s: faber(mm, s, 9)),
            "11-month": run_split(m, lambda mm, s: faber(mm, s, 11)),
            "10-month, days 3/8/13/18": run_split(m, faber, offsets=shifted),
        },
        "4 Hold by default, split": {
            "200-day (standard)": run_split(m, hold_by_default),
            "150-day": run_split(m, lambda mm, s: hold_by_default(mm, s, 150)),
            "250-day": run_split(m, lambda mm, s: hold_by_default(mm, s, 250)),
            "200-day, days 3/8/13/18": run_split(m, hold_by_default, offsets=shifted),
        },
    }

    # ---- tables
    periods = {"Full period": (None, None), f"{START.year}–2014": (None, "2014-12-31"),
               f"2015–{end.year}": ("2015-01-01", None)}
    tables = {p: {n: stats(r, m, a, b) for n, r in results.items()} for p, (a, b) in periods.items()}
    stress_tab = {n: {y: stress(r, y) for y in STRESS_YEARS} for n, r in results.items()}
    sens_tab = {ap: {lab: {**stats(r, m), **{f"{p}_cagr": stats(r, m, a, b)["cagr"] for p, (a, b) in list(periods.items())[1:]},
                           **{f"{p}_sharpe": stats(r, m, a, b)["sharpe"] for p, (a, b) in list(periods.items())[1:]}}
                     for lab, r in v.items()} for ap, v in sens_results.items()}

    os.makedirs(OUT_DIR, exist_ok=True)
    pd.DataFrame([{"period": p, "strategy": n, **s} for p, t in tables.items() for n, s in t.items()]).to_csv(
        os.path.join(OUT_DIR, "summary.csv"), index=False)
    pd.DataFrame([{"strategy": n, "year": y, **v} for n, t in stress_tab.items() for y, v in t.items()]).to_csv(
        os.path.join(OUT_DIR, "stress.csv"), index=False)
    pd.DataFrame([{"approach": ap, "variant": lab, **s} for ap, t in sens_tab.items() for lab, s in t.items()]).to_csv(
        os.path.join(OUT_DIR, "sensitivity.csv"), index=False)
    pd.DataFrame({n: yearly_table(r) for n, r in results.items()}).to_csv(os.path.join(OUT_DIR, "yearly.csv"))

    # ---- console
    for p, t in tables.items():
        print(f"\n== {p} ==")
        print(f"{'Strategy':<40}{'CAGR':>7}{'Vol':>7}{'Sharpe':>7}{'MaxDD':>8}{'Worst yr':>14}{'Tr/yr':>7}{'Turn/yr':>8}{'Inv':>6}")
        for n, s in t.items():
            print(f"{n:<40}{pct(s['cagr']):>7}{pct(s['vol']):>7}{num(s['sharpe']):>7}{pct(s['max_dd']):>8}"
                  f"{pct(s['worst_year']) + ' (' + str(s['worst_year_label']) + ')':>14}{num(s['trades_per_year'], 1):>7}"
                  f"{pct(s['turnover_per_year'], 0):>8}{pct(s['avg_invested'], 0):>6}")
    print("\n== Stress years: return / max drawdown within the year ==")
    print(f"{'Strategy':<40}" + "".join(f"{y:>18}" for y in STRESS_YEARS))
    for n, t in stress_tab.items():
        print(f"{n:<40}" + "".join(f"{pct(t[y]['return']) + ' / ' + pct(t[y]['max_dd']):>18}" for y in STRESS_YEARS))
    print("\n== Sensitivity (full period CAGR / Sharpe / MaxDD | Sharpe 1st half / 2nd half) ==")
    for ap, t in sens_tab.items():
        print(ap)
        for lab, s in t.items():
            halves = [s[f"{p}_sharpe"] for p in list(periods)[1:]]
            print(f"   {lab:<34}{pct(s['cagr']):>7}{num(s['sharpe']):>7}{pct(s['max_dd']):>8}   |{num(halves[0]):>6}{num(halves[1]):>6}")
    # Split rebalancing table + decision rule (fixed before running):
    # approach 4 wins only if every split variant keeps max DD >= -22% ("around -20%")
    # and beats the 60/40 full-period Sharpe; otherwise approach 1.
    split_tab = {ap: {lab: {**stats(r, m), **{f"{p}_sharpe": stats(r, m, a, b)["sharpe"]
                                              for p, (a, b) in list(periods.items())[1:]},
                            "dd2020": stress(r, 2020)["max_dd"]}
                      for lab, r in v.items()} for ap, v in split_results.items()}
    ref_sharpe = tables["Full period"]["60/40 SPY/IEF"]["sharpe"]
    four = split_tab["4 Hold by default, split"]
    approach4_passes = all(s["max_dd"] >= -0.22 and s["sharpe"] > ref_sharpe for s in four.values())
    decision = "approach 4 (hold by default)" if approach4_passes else "approach 1 (trend allocation)"
    pd.DataFrame([{"approach": ap, "variant": lab, **s} for ap, t in split_tab.items() for lab, s in t.items()]).to_csv(
        os.path.join(OUT_DIR, "split.csv"), index=False)
    short = {"1 Trend allocation, split": split_tab["1 Trend allocation, split"]["10-month (standard)"],
             "4 Hold by default, split": four["200-day (standard)"],
             "60/40 SPY/IEF": tables["Full period"]["60/40 SPY/IEF"],
             "SPY buy & hold": tables["Full period"]["SPY buy & hold"]}
    print("\n== Split rebalancing (4 parts, trading days 1/6/11/16), full period ==")
    print(f"{'Strategy':<30}{'CAGR':>7}{'Vol':>7}{'Sharpe':>7}{'MaxDD':>8}{'Worst yr':>15}{'Tr/yr':>7}")
    for n, s in short.items():
        print(f"{n:<30}{pct(s['cagr']):>7}{pct(s['vol']):>7}{num(s['sharpe']):>7}{pct(s['max_dd']):>8}"
              f"{pct(s['worst_year']) + ' (' + str(s['worst_year_label']) + ')':>15}{num(s['trades_per_year'], 1):>7}")
    print("\n== Split variants: CAGR / Sharpe / MaxDD / 2020 DD | Sharpe halves ==")
    for ap, t in split_tab.items():
        print(ap)
        for lab, s in t.items():
            halves = [s[f"{p}_sharpe"] for p in list(periods)[1:]]
            print(f"   {lab:<26}{pct(s['cagr']):>7}{num(s['sharpe']):>7}{pct(s['max_dd']):>8}{pct(s['dd2020']):>8}"
                  f"   |{num(halves[0]):>6}{num(halves[1]):>6}")
    print(f"\nDecision rule: approach 4 needs every split variant max DD >= -22% and Sharpe > 60/40 ({ref_sharpe:.2f}).")
    for lab, s in four.items():
        print(f"   {lab:<26} max DD {pct(s['max_dd']):>7} {'ok' if s['max_dd'] >= -0.22 else 'FAIL'}   "
              f"Sharpe {num(s['sharpe'])} {'ok' if s['sharpe'] > ref_sharpe else 'FAIL'}")
    print(f"=> {decision}")

    j_exits = pd.Series(results[names[4]]["exit_rules"]).value_counts().to_dict()
    print("\nExit rules, setup J + trailing stop:", j_exits)
    print(f"Cash: ^IRX T-bill rate until {m.bil_start.date()}, BIL total return after.")

    # ---- charts + HTML report
    img_eq = equity_chart(results, names)
    img_dd = drawdown_chart(results, names)
    img_sens = sensitivity_chart(sens_results, names)
    img_split = split_chart({
        "Trend allocation, split (chosen)": split_results["1 Trend allocation, split"]["10-month (standard)"]["equity"],
        "Hold by default, split": split_results["4 Hold by default, split"]["200-day (standard)"]["equity"],
        "60/40 SPY/IEF": results["60/40 SPY/IEF"]["equity"],
        "SPY buy & hold": results["SPY buy & hold"]["equity"],
    })
    stress_rows = "".join(
        f"<tr><td>{n}</td>" + "".join(f"<td>{pct(t[y]['return'], signed=True)}</td><td>{pct(t[y]['max_dd'])}</td>"
                                      for y in STRESS_YEARS) + "</tr>" for n, t in stress_tab.items())
    sens_rows = "".join(
        f"<tr><td>{ap if i == 0 else ''}</td><td>{lab}</td><td>{pct(s['cagr'])}</td><td>{num(s['sharpe'])}</td>"
        f"<td>{pct(s['max_dd'])}</td><td>{pct(s['worst_year'])}</td>"
        + "".join(f"<td>{num(s[f'{p}_sharpe'])}</td>" for p in list(periods)[1:]) + "</tr>"
        for ap, t in sens_tab.items() for i, (lab, s) in enumerate(t.items()))
    html = [
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Strategy Research</title><style>{CSS}</style></head><body><main>",
        "<h1>Strategy research: long-history robustness check</h1>",
        f"<p class='muted'>{START:%Y-%m-%d} to {end:%Y-%m-%d} · $100k start · 0.05% cost per side · "
        f"generated {pd.Timestamp.now():%Y-%m-%d %H:%M}</p>",
        f"<p class='note'>Start date {START:%Y-%m-%d}: DBC (commodities) begins 2006-02-06, the latest of all "
        "inputs; its 11th month-end close is December 2006, so 9-, 10- and 11-month averages are all defined at the "
        "first rebalance, the first trading day of 2007. Cash earns the 3-month T-bill rate (^IRX) until BIL "
        f"started ({m.bil_start:%Y-%m-%d}), then BIL's total return. Monthly strategies trade at the open of the "
        "first trading day of each month using closes through the previous month-end; setup J variants run the "
        "live bot's own decision and sizing code daily. Prices are dividend-adjusted. Sharpe uses excess return "
        "over cash. Trades = positions opened plus positions closed; turnover = traded value ÷ portfolio value.</p>",
        "<h2>Split rebalancing: chosen strategy vs. benchmarks</h2>",
        f"<p>Decision rule: approach 4 needed every split variant's max drawdown ≥ −22% and Sharpe above 60/40 "
        f"({ref_sharpe:.2f}). Result: <b>{decision}</b>.</p>",
        metrics_rows(short),
        "<div class='scroll'><table><thead><tr><th>Split variant</th><th>CAGR</th><th>Sharpe</th><th>Max DD</th>"
        "<th>2020 DD</th>" + "".join(f"<th>Sharpe {p}</th>" for p in list(periods)[1:]) + "</tr></thead><tbody>"
        + "".join(f"<tr><td>{ap} · {lab}</td><td>{pct(s['cagr'])}</td><td>{num(s['sharpe'])}</td><td>{pct(s['max_dd'])}</td>"
                  f"<td>{pct(s['dd2020'])}</td>" + "".join(f"<td>{num(s[f'{p}_sharpe'])}</td>" for p in list(periods)[1:])
                  + "</tr>" for ap, t in split_tab.items() for lab, s in t.items())
        + "</tbody></table></div>",
        f"<img src='data:image/png;base64,{img_split}' alt='split rebalancing equity'>",
        "<h2>Equity curves (single rebalance day)</h2>", f"<img src='data:image/png;base64,{img_eq}' alt='equity curves'>",
    ]
    for p, t in tables.items():
        html += [f"<h2>{p}</h2>", metrics_rows(t)]
    html += ["<h2>Stress years</h2><p class='muted'>Calendar-year return / worst drawdown within the year.</p>",
             "<div class='scroll'><table><thead><tr><th>Strategy</th>"
             + "".join(f"<th>{y} return</th><th>{y} max DD</th>" for y in STRESS_YEARS)
             + f"</tr></thead><tbody>{stress_rows}</tbody></table></div>",
             "<h2>Drawdowns</h2>", f"<img src='data:image/png;base64,{img_dd}' alt='drawdowns'>",
             "<h2>Sensitivity to nearby parameters</h2>",
             "<div class='scroll'><table><thead><tr><th>Approach</th><th>Variant</th><th>CAGR</th><th>Sharpe</th>"
             "<th>Max DD</th><th>Worst year</th>" + "".join(f"<th>Sharpe {p}</th>" for p in list(periods)[1:])
             + f"</tr></thead><tbody>{sens_rows}</tbody></table></div>",
             f"<img src='data:image/png;base64,{img_sens}' alt='sensitivity'>",
             "</main></body></html>"]
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(html))
    print(f"\nReport: {REPORT}\nCSV + charts: {OUT_DIR}")


if __name__ == "__main__":
    main()
