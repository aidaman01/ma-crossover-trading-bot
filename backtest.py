"""ARCHIVED: backtest of setup J (daily EMA crossover) - 2021-2026 setup comparison.

This module documents how setup J was chosen in an earlier phase of the project. The live
bot now runs monthly trend allocation (see research.py and docs/research.md); setup J is
kept as a comparison strategy and as config.STRATEGY = "ema_crossover". research.py still
imports WATCHLIST_12, CANDIDATES and config_overrides from here.

Note: the results in docs/backtest/ were generated on 2026-10-04 with a 5-year window
ending at that date; rerunning uses a window ending today, so the numbers will move.
Curve metrics now come from the shared metrics.py (CAGR counts the first day from the
starting capital, which changes the archived CAGRs by less than 0.01 percentage points).

Usage:
    python backtest.py                 # writes docs/backtest/ and backtests/report.html
    python backtest.py --years 5 --cost-pct 0.05

Every decision comes from bot.evaluate() and every position size from
bot.position_size() - the same functions the live bot uses - with the settings of each
setup applied on top of config.py.

Timing mirrors the scheduled live bot (9:35 ET): on day t the signal uses the completed
bars t-2 and t-1 (no look-ahead), the stop is checked against day t's open, and orders
fill at day t's open plus/minus BACKTEST_COST_PCT. Symbols are processed in watchlist
order each morning, like the live bot.

Portfolio: one shared cash account for the whole watchlist, whole shares, never more
than the available cash (no margin). Benchmark: an equal-weight buy-and-hold basket of
the same symbols, bought at the first open and not rebalanced.
"""
import argparse
import base64
import contextlib
import glob
import io
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

import bot  # noqa: E402
import config  # noqa: E402
import metrics  # noqa: E402
from indicators import add_indicators  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "backtests")             # HTML report (git-ignored)
PUBLISH_DIR = os.path.join(HERE, "docs", "backtest")  # CSVs + charts (committed; used by README/dashboard)
CHART_DIR = os.path.join(PUBLISH_DIR, "charts")

TARGET_TRADES_PER_MONTH = (3, 5)

# --------------------------------------------------------------------------- setups
# Settings that define a setup; everything else (exits, ATR stop, indicator periods)
# comes from config.py.
STRATEGY_KEYS = [
    "SYMBOLS", "MA_TYPE", "FAST_MA", "SLOW_MA", "ENTRY_WINDOW_DAYS", "ONE_ENTRY_PER_CROSSOVER",
    "USE_TREND_FILTER", "USE_RSI_FILTER", "RSI_BUY_MIN", "RSI_BUY_MAX", "USE_MACD_FILTER",
    "USE_VOLUME_FILTER", "USE_ADX_FILTER", "ADX_MIN", "POSITION_SIZING",
]
WATCHLIST_8 = ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV"]
WATCHLIST_12 = WATCHLIST_8 + ["XLI", "XLY", "XLP", "XLU"]
ALL_ON = dict(USE_TREND_FILTER=True, USE_RSI_FILTER=True, RSI_BUY_MIN=50, RSI_BUY_MAX=70,
              USE_MACD_FILTER=True, USE_VOLUME_FILTER=True, USE_ADX_FILTER=True, ADX_MIN=20)
LOOSENED = dict(ALL_ON, RSI_BUY_MIN=45, USE_VOLUME_FILTER=False, ADX_MIN=15)
TREND_MACD = dict(ALL_ON, USE_RSI_FILTER=False, USE_VOLUME_FILTER=False, USE_ADX_FILTER=False)
TREND_ONLY = dict(TREND_MACD, USE_MACD_FILTER=False)
NO_FILTERS = dict(ALL_ON, USE_TREND_FILTER=False, USE_RSI_FILTER=False, USE_MACD_FILTER=False,
                  USE_VOLUME_FILTER=False, USE_ADX_FILTER=False)
BASE = dict(ONE_ENTRY_PER_CROSSOVER=True, POSITION_SIZING="equal_weight")


def setup(symbols, ma, fast, slow, window, filters):
    return dict(BASE, **filters, SYMBOLS=symbols, MA_TYPE=ma, FAST_MA=fast, SLOW_MA=slow, ENTRY_WINDOW_DAYS=window)


OLD_SETUPS = {
    "Old: SMA 20/50, all filters, SPY+QQQ": setup(["SPY", "QQQ"], "SMA", 20, 50, 0, ALL_ON),
    "Old: SMA 20/50, crossover only, SPY+QQQ": setup(["SPY", "QQQ"], "SMA", 20, 50, 0, NO_FILTERS),
}
# Round 1 (8 ETFs) stayed below 3 trades/month; round 2 runs the same designs on 12 ETFs.
CANDIDATES = {
    "A: EMA 9/21, all filters, 10d window, 8 ETFs": setup(WATCHLIST_8, "EMA", 9, 21, 10, ALL_ON),
    "B: EMA 9/21, loosened filters, 10d window, 8 ETFs": setup(WATCHLIST_8, "EMA", 9, 21, 10, LOOSENED),
    "C: EMA 10/30, loosened filters, 10d window, 8 ETFs": setup(WATCHLIST_8, "EMA", 10, 30, 10, LOOSENED),
    "D: EMA 9/21, trend + MACD, 10d window, 8 ETFs": setup(WATCHLIST_8, "EMA", 9, 21, 10, TREND_MACD),
    "E: EMA 10/30, trend only, 10d window, 8 ETFs": setup(WATCHLIST_8, "EMA", 10, 30, 10, TREND_ONLY),
    "F: EMA 9/21, loosened filters, 10d window, 12 ETFs": setup(WATCHLIST_12, "EMA", 9, 21, 10, LOOSENED),
    "G: EMA 10/30, loosened filters, 10d window, 12 ETFs": setup(WATCHLIST_12, "EMA", 10, 30, 10, LOOSENED),
    "H: EMA 9/21, trend + MACD, 10d window, 12 ETFs": setup(WATCHLIST_12, "EMA", 9, 21, 10, TREND_MACD),
    "I: EMA 10/30, trend only, 10d window, 12 ETFs": setup(WATCHLIST_12, "EMA", 10, 30, 10, TREND_ONLY),
    "J: EMA 10/30, crossover only, 12 ETFs": setup(WATCHLIST_12, "EMA", 10, 30, 10, NO_FILTERS),
}
FILTER_FLAGS = {"trend": "USE_TREND_FILTER", "RSI": "USE_RSI_FILTER", "MACD": "USE_MACD_FILTER",
                "volume": "USE_VOLUME_FILTER", "ADX": "USE_ADX_FILTER"}


def current_settings():
    return {key: getattr(config, key) for key in STRATEGY_KEYS}


def current_setup_name():
    """Name of the setup that matches config.py, or a generic name if none does."""
    now = current_settings()
    for name, settings in {**OLD_SETUPS, **CANDIDATES}.items():
        if all(settings[k] == now[k] for k in STRATEGY_KEYS):
            return name
    return "Current config.py"


def filter_study():
    """Current config, then with each filter flipped (on -> off, off -> on) one at a time."""
    now = current_settings()
    out = {"Current setup": now}
    for name, flag in FILTER_FLAGS.items():
        out[f"{'Without' if now[flag] else 'With'} {name} filter"] = {**now, flag: not now[flag]}
    if any(now[flag] for flag in FILTER_FLAGS.values()):
        out["No filters (crossover only)"] = {**now, **{flag: False for flag in FILTER_FLAGS.values()}}
        if now["ENTRY_WINDOW_DAYS"] > 0:
            out["No entry window (crossover day only)"] = {**now, "ENTRY_WINDOW_DAYS": 0}
    return out


@contextlib.contextmanager
def config_overrides(settings):
    old = {key: getattr(config, key) for key in settings}
    for key, value in settings.items():
        setattr(config, key, value)
    try:
        yield
    finally:
        for key, value in old.items():
            setattr(config, key, value)


# --------------------------------------------------------------------------- data
def load(symbol, years):
    """Raw daily bars; 2 extra years before the test window warm up the indicators."""
    start = pd.Timestamp.today().normalize() - pd.DateOffset(years=years + 2)
    df = yf.Ticker(symbol).history(start=start.strftime("%Y-%m-%d"), interval="1d", auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"yfinance returned no data for {symbol}")
    df = df[["Open", "High", "Low", "Close", "Volume"]]
    df.index = df.index.tz_localize(None).normalize()
    return df


# --------------------------------------------------------------------------- simulation
def simulate(raw, name, years, cost, capital, start=None):
    """Run the current config (with any overrides applied) as one portfolio.

    The test window is the last `years` years, or from `start` (a date) if given.
    """
    symbols = list(config.SYMBOLS)
    # Indicators are causal (rolling / exponential), so each row only uses data up to that day.
    frames = {s: add_indicators(raw[s], config) for s in symbols}
    dates = frames[symbols[0]].index
    for s in symbols[1:]:
        dates = dates.intersection(frames[s].index)
    frames = {s: frames[s].loc[dates] for s in symbols}
    first_day = pd.Timestamp(start) if start is not None else dates[-1] - pd.DateOffset(years=years)
    start_i = max(2, int(dates.searchsorted(first_day)))

    rows = {s: list(frames[s].itertuples()) for s in symbols}
    ready = {s: frames[s][bot.INDICATOR_COLUMNS].notna().all(axis=1).to_numpy() for s in symbols}
    cash = float(capital)
    shares = dict.fromkeys(symbols, 0)
    entry = dict.fromkeys(symbols)
    entry_cross = dict.fromkeys(symbols)
    trades, equity, open_count = [], [], []

    for i in range(start_i, len(dates)):
        equity_open = cash + sum(shares[s] * rows[s][i].Open for s in symbols)
        for s in symbols:  # same order as the live bot
            if not (ready[s][i - 2] and ready[s][i - 1]):
                continue
            prev, last, today = rows[s][i - 2], rows[s][i - 1], rows[s][i]
            price = today.Open  # live bot runs at 9:35 ET, right after the open
            decision = bot.evaluate(prev, last, price, shares[s],
                                    entry[s]["stop"] if entry[s] else None, entry_cross[s])
            if decision["signal"] == "BUY":
                qty = bot.position_size(equity_open, cash / (1 + cost), price, last.atr)
                if qty >= 1:
                    fill = price * (1 + cost)
                    cash -= qty * fill
                    assert cash >= -1e-6, "position sizing used more than the available cash"
                    shares[s] = qty
                    entry[s] = {"date": today.Index, "price": fill, "shares": qty, "cost": qty * fill,
                                "stop": fill - config.ATR_STOP_MULTIPLIER * last.atr, "i": i}
                    entry_cross[s] = decision["cross_date"]
            elif decision["signal"] == "SELL":
                fill = price * (1 - cost)
                proceeds = shares[s] * fill
                cash += proceeds
                trades.append(trade_record(name, s, entry[s], today.Index, fill, proceeds,
                                           ", ".join(decision["exit_rules"]), i, "closed"))
                shares[s], entry[s] = 0, None
        equity.append(cash + sum(shares[s] * rows[s][i].Close for s in symbols))
        open_count.append(sum(1 for s in symbols if shares[s]))

    for s in symbols:  # still open at the end: mark to the last close
        if entry[s]:
            close = rows[s][-1].Close
            trades.append(trade_record(name, s, entry[s], dates[-1], close, shares[s] * close,
                                       "open at end of test", len(dates) - 1, "open"))

    index = dates[start_i:]
    basket = sum(frames[s]["Close"].iloc[start_i:] * (capital / len(symbols))
                 / (frames[s]["Open"].iloc[start_i] * (1 + cost)) for s in symbols)
    return {"name": name, "symbols": symbols, "settings": current_settings(),
            "equity": pd.Series(equity, index=index), "basket": basket,
            "open_count": pd.Series(open_count, index=index),
            "trades": sorted(trades, key=lambda t: t["entry_date"])}


def trade_record(setup_name, symbol, entry, exit_date, exit_price, proceeds, rule, exit_i, status):
    pnl = proceeds - entry["cost"]
    return {
        "setup": setup_name, "symbol": symbol, "status": status,
        "entry_date": entry["date"].date(), "entry_price": entry["price"],
        "exit_date": exit_date.date(), "exit_price": exit_price, "shares": entry["shares"],
        "stop": entry["stop"], "pnl": pnl, "pnl_pct": pnl / entry["cost"],
        "days_held": exit_i - entry["i"], "exit_rule": rule,
    }


def run_setup(raw, name, settings, years, cost, capital):
    with config_overrides(settings):
        return simulate(raw, name, years, cost, capital)


# --------------------------------------------------------------------------- metrics
def curve_stats(curve, capital):
    """Total return, CAGR, max drawdown and CAGR/|max DD| from the shared metrics module."""
    return {"total": metrics.total_return(curve, capital), "cagr": metrics.cagr(curve, capital),
            "max_dd": metrics.max_drawdown(curve, capital), "mar": metrics.calmar_ratio(curve, capital)}


def yearly_returns(curve, capital):
    year_end = curve.groupby(curve.index.year).last()
    return year_end / year_end.shift(1).fillna(capital) - 1


def summarize(res, capital, study):
    strat, basket = curve_stats(res["equity"], capital), curve_stats(res["basket"], capital)
    trades = res["trades"]
    closed = [t for t in trades if t["status"] == "closed"]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    worst = min(closed, key=lambda t: t["pnl"]) if closed else None
    months = (res["equity"].index[-1] - res["equity"].index[0]).days / 30.44
    s = res["settings"]
    return {
        "study": study, "setup": res["name"], "symbols": " ".join(res["symbols"]),
        "ma": f"{s['MA_TYPE']} {s['FAST_MA']}/{s['SLOW_MA']}", "entry_window": s["ENTRY_WINDOW_DAYS"],
        "trades": len(trades), "trades_per_month": len(trades) / months,
        "total": strat["total"], "cagr": strat["cagr"], "max_dd": strat["max_dd"], "mar": strat["mar"],
        "win_rate": len(wins) / len(closed) if closed else float("nan"),
        "avg_win": sum(t["pnl_pct"] for t in wins) / len(wins) if wins else float("nan"),
        "avg_loss": sum(t["pnl_pct"] for t in losses) / len(losses) if losses else float("nan"),
        "worst_pct": worst["pnl_pct"] if worst and worst["pnl"] < 0 else float("nan"),
        "worst_usd": worst["pnl"] if worst and worst["pnl"] < 0 else float("nan"),
        "time_in_market": (res["open_count"] > 0).mean(),
        "avg_positions": res["open_count"].mean(),
        "basket_total": basket["total"], "basket_cagr": basket["cagr"],
        "basket_max_dd": basket["max_dd"], "basket_mar": basket["mar"],
    }


def pick_best(summaries):
    """Highest CAGR / |max DD| among candidates with 3-5 trades per month."""
    lo, hi = TARGET_TRADES_PER_MONTH
    eligible = [m for m in summaries if m["setup"] in CANDIDATES and lo <= m["trades_per_month"] <= hi
                and not pd.isna(m["mar"])]
    return max(eligible, key=lambda m: m["mar"])["setup"] if eligible else None


# --------------------------------------------------------------------------- output
def pct(x, signed=True):
    if pd.isna(x):
        return "–"
    return f"{x * 100:+.1f}%" if signed else f"{x * 100:.1f}%"


def usd(x):
    return "–" if pd.isna(x) else f"${x:,.0f}"


def slug(name):
    return "_".join("".join(c if c.isalnum() else " " for c in name).lower().split())


def chart(res, capital, save=True):
    eq, basket, n = res["equity"], res["basket"], res["open_count"]
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(9, 4.2), dpi=110, sharex=True,
                                  gridspec_kw={"height_ratios": [3.2, 1]})
    ax.plot(basket.index, basket.values, color="#9ca3af", lw=1.3, label="Buy & hold (equal-weight basket)")
    ax.plot(eq.index, eq.values, color="#2563eb", lw=1.8, label="Strategy")
    ax.axhline(capital, color="#d1d5db", lw=0.8, ls="--")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v / 1000:,.0f}k"))
    ax.set_title(res["name"], fontsize=10, loc="left")
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    ax2.fill_between(n.index, n.values, step="post", color="#2563eb", alpha=0.25, lw=0)
    ax2.set_ylabel("open\npositions", fontsize=8)
    ax2.set_ylim(0, max(len(res["symbols"]), 1))
    ax2.yaxis.set_major_locator(mticker.MaxNLocator(integer=True, nbins=3))
    for a in (ax, ax2):
        a.grid(alpha=0.25)
        for side in ("top", "right"):
            a.spines[side].set_visible(False)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    if save:
        fig.savefig(os.path.join(CHART_DIR, f"{slug(res['name'])}.png"), bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def setups_table(sums, marks):
    head = ("<tr><th>Setup</th><th>Trades/mo</th><th>Total</th><th>CAGR</th><th>Max DD</th><th>CAGR/DD</th>"
            "<th>Win rate</th><th>Avg positions</th><th class='bh'>Basket total</th><th class='bh'>Basket CAGR</th>"
            "<th class='bh'>Basket max DD</th><th class='bh'>Basket CAGR/DD</th></tr>")
    rows = []
    for m in sums:
        badges = "".join(f" <span class='badge'>{b}</span>" for b in marks.get(m["setup"], []))
        rows.append(
            f"<tr><td>{m['setup']}{badges}</td><td>{m['trades_per_month']:.1f}</td><td>{pct(m['total'])}</td>"
            f"<td>{pct(m['cagr'])}</td><td>{pct(m['max_dd'])}</td><td>{m['mar']:.2f}</td>"
            f"<td>{pct(m['win_rate'], False)}</td><td>{m['avg_positions']:.1f}</td>"
            f"<td class='bh'>{pct(m['basket_total'])}</td><td class='bh'>{pct(m['basket_cagr'])}</td>"
            f"<td class='bh'>{pct(m['basket_max_dd'])}</td><td class='bh'>{m['basket_mar']:.2f}</td></tr>")
    return f"<div class='scroll'><table><thead>{head}</thead><tbody>{''.join(rows)}</tbody></table></div>"


def detail_section(res, m, capital):
    yr_s, yr_b = yearly_returns(res["equity"], capital), yearly_returns(res["basket"], capital)
    years = "".join(f"<tr><td>{y}</td><td>{pct(yr_s[y])}</td><td>{pct(yr_b[y])}</td></tr>" for y in yr_s.index)
    trades = pd.DataFrame(res["trades"])
    per_symbol = ""
    if not trades.empty:
        g = trades.groupby("symbol")
        per = pd.DataFrame({"trades": g.size(), "wins": g.apply(lambda d: (d["pnl"] > 0).sum()),
                            "pnl": g["pnl"].sum()}).reindex(res["symbols"]).fillna(0)
        per_symbol = "".join(
            f"<tr><td>{s}</td><td>{int(r.trades)}</td><td>{pct(r.wins / r.trades, False) if r.trades else '–'}</td>"
            f"<td class='{'pos' if r.pnl > 0 else 'neg'}'>{usd(r.pnl)}</td></tr>" for s, r in per.iterrows())
    trade_rows = "".join(
        f"<tr><td>{t['symbol']}</td><td>{t['entry_date']}</td>"
        f"<td>{t['exit_date']}{' (open)' if t['status'] == 'open' else ''}</td>"
        f"<td>${t['entry_price']:,.2f}</td><td>${t['exit_price']:,.2f}</td><td>{t['shares']:g}</td>"
        f"<td class='{'pos' if t['pnl'] > 0 else 'neg'}'>{usd(t['pnl'])}</td>"
        f"<td class='{'pos' if t['pnl'] > 0 else 'neg'}'>{pct(t['pnl_pct'])}</td>"
        f"<td>{t['days_held']}</td><td>{t['exit_rule']}</td></tr>" for t in res["trades"])
    metrics = (
        "<table class='kv'><tr><th></th><th>Strategy</th><th>Basket</th></tr>"
        f"<tr><td>Total return</td><td>{pct(m['total'])}</td><td>{pct(m['basket_total'])}</td></tr>"
        f"<tr><td>CAGR</td><td>{pct(m['cagr'])}</td><td>{pct(m['basket_cagr'])}</td></tr>"
        f"<tr><td>Max drawdown</td><td>{pct(m['max_dd'])}</td><td>{pct(m['basket_max_dd'])}</td></tr>"
        f"<tr><td>Trades (per month)</td><td>{m['trades']} ({m['trades_per_month']:.1f})</td><td></td></tr>"
        f"<tr><td>Win rate</td><td>{pct(m['win_rate'], False)}</td><td></td></tr>"
        f"<tr><td>Average win / loss</td><td>{pct(m['avg_win'])} / {pct(m['avg_loss'])}</td><td></td></tr>"
        f"<tr><td>Biggest loss</td><td>{pct(m['worst_pct'])} ({usd(m['worst_usd'])})</td><td></td></tr>"
        f"<tr><td>Time with ≥1 position</td><td>{pct(m['time_in_market'], False)}</td><td>100%</td></tr>"
        f"<tr><td>Average open positions</td><td>{m['avg_positions']:.1f} of {len(res['symbols'])}</td><td></td></tr>"
        "</table>")
    return (
        f"<img src='data:image/png;base64,{chart(res, capital)}' alt='equity chart'>"
        f"<div class='cols'>{metrics}"
        f"<table class='kv'><tr><th>Year</th><th>Strategy</th><th>Basket</th></tr>{years}</table>"
        f"<table class='kv'><tr><th>Symbol</th><th>Trades</th><th>Win</th><th>P/L</th></tr>{per_symbol}</table></div>"
        "<details><summary>Every trade</summary><div class='scroll'><table><thead><tr><th>Symbol</th><th>Entry</th>"
        "<th>Exit</th><th>Entry price</th><th>Exit price</th><th>Shares</th><th>P/L</th><th>P/L %</th><th>Days</th>"
        f"<th>Exit rule</th></tr></thead><tbody>{trade_rows}</tbody></table></div></details>")


CSS = """
body{font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:#111827;background:#fff;margin:0}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:36px 0 8px;border-bottom:1px solid #e5e7eb;padding-bottom:4px}
.muted{color:#6b7280}.note{background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:10px 14px}
table{border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13px}
th,td{padding:4px 9px;text-align:right;border-bottom:1px solid #f0f0f0;white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:#374151;font-weight:600}
.bh{color:#6b7280}.pos{color:#047857}.neg{color:#b91c1c}
.badge{background:#2563eb;color:#fff;border-radius:4px;padding:0 5px;font-size:11px}
.scroll{overflow-x:auto;margin:8px 0}.cols{display:flex;gap:32px;flex-wrap:wrap;margin:8px 0;align-items:flex-start}
details{border:1px solid #e5e7eb;border-radius:8px;padding:8px 14px;margin:12px 0}
summary{cursor:pointer;font-weight:600}img{max-width:100%;margin-top:8px}
"""


# --------------------------------------------------------------------------- study
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--years", type=int, default=config.BACKTEST_YEARS)
    parser.add_argument("--cost-pct", type=float, default=config.BACKTEST_COST_PCT,
                        help="cost per side in percent of trade value")
    args = parser.parse_args(argv)
    years, cost, capital = args.years, args.cost_pct / 100, config.BACKTEST_START_CAPITAL
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(CHART_DIR, exist_ok=True)
    for old_chart in glob.glob(os.path.join(CHART_DIR, "*.png")):
        os.remove(old_chart)

    setups = {**OLD_SETUPS, **CANDIDATES}
    current = current_setup_name()
    if current not in setups:
        setups[current] = current_settings()
    symbols = sorted({s for st in setups.values() for s in st["SYMBOLS"]})
    print(f"Downloading {years}y (+2y warm-up) of daily data for {', '.join(symbols)}...")
    raw = {s: load(s, years) for s in symbols}

    # 1. Setup comparison
    results = {name: run_setup(raw, name, st, years, cost, capital) for name, st in setups.items()}
    sums = [summarize(results[n], capital, "setups") for n in setups]
    best = pick_best(sums)

    # 2. Filter study on the current config
    study = {name: run_setup(raw, name, st, years, cost, capital) for name, st in filter_study().items()}
    study_sums = [summarize(r, capital, "filter study") for r in study.values()]

    # CSV outputs (docs/backtest is committed and read by the README and dashboard)
    marks = {}
    for m in sums:
        m["is_current"], m["is_best"] = m["setup"] == current, m["setup"] == best
        marks[m["setup"]] = [b for b, on in (("current", m["is_current"]), ("best", m["is_best"])) if on]
    pd.DataFrame(sums + study_sums).to_csv(os.path.join(PUBLISH_DIR, "summary.csv"), index=False)
    pd.DataFrame([t for r in results.values() for t in r["trades"]]).to_csv(
        os.path.join(PUBLISH_DIR, "trades.csv"), index=False)
    pd.DataFrame([{"setup": n, "year": y, "strategy": yearly_returns(r["equity"], capital)[y],
                   "basket": yearly_returns(r["basket"], capital)[y]}
                  for n, r in results.items() for y in yearly_returns(r["equity"], capital).index]).to_csv(
        os.path.join(PUBLISH_DIR, "yearly.csv"), index=False)

    # Console summary
    print(f"\n{'Setup':<52} {'tr/mo':>5} {'total':>8} {'CAGR':>7} {'maxDD':>7} {'C/DD':>5} {'win':>6}"
          f"  | basket {'total':>8} {'CAGR':>7} {'maxDD':>7}")
    for m in sums + [None] + study_sums:
        if m is None:
            print("\nFilter study on the current config:")
            continue
        print(f"{m['setup']:<52} {m['trades_per_month']:>5.1f} {pct(m['total']):>8} {pct(m['cagr']):>7} "
              f"{pct(m['max_dd']):>7} {m['mar']:>5.2f} {pct(m['win_rate'], False):>6}  | basket "
              f"{pct(m['basket_total']):>8} {pct(m['basket_cagr']):>7} {pct(m['basket_max_dd']):>7}")
    print(f"\nBest candidate (3-5 trades/month, highest CAGR/maxDD): {best}\nCurrent config: {current}")

    # HTML report
    cur = results[current]
    cur_sum = next(m for m in sums if m["setup"] == current)
    start, end = cur["equity"].index[[0, -1]]
    html = [
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Strategy Backtest</title><style>{CSS}</style></head><body><main>",
        "<h1>Strategy backtest</h1>",
        f"<p class='muted'>{start:%Y-%m-%d} to {end:%Y-%m-%d} · start capital {usd(capital)} · cost "
        f"{args.cost_pct:.2f}% per side · generated {pd.Timestamp.now():%Y-%m-%d %H:%M}</p>",
        "<p class='note'>Every decision comes from <code>bot.evaluate</code> and every position size from "
        "<code>bot.position_size</code>, the same code the live bot runs. Signals use completed bars only "
        "(through yesterday); orders fill at the next open, like the 9:35 ET scheduled run, and the stop is "
        "checked once a day at the open. One shared cash account per setup, equal-weight positions "
        "(equity ÷ number of symbols), whole shares, no margin. Benchmark: equal-weight buy-and-hold basket "
        "of the same symbols. Exits for every setup: fast MA crosses below slow MA, "
        f"RSI &gt; {config.RSI_EXIT_ABOVE}, "
        f"stop {config.ATR_STOP_MULTIPLIER}×ATR({config.ATR_PERIOD}) below entry.</p>",
        "<h2>Setup comparison</h2>",
        f"<p class='muted'>Best = highest CAGR ÷ |max drawdown| among candidates A–J with "
        f"{TARGET_TRADES_PER_MONTH[0]}–{TARGET_TRADES_PER_MONTH[1]} trades per month. Trades = positions opened.</p>",
        setups_table(sums, marks),
        f"<h2>Current config: {current}</h2>",
        detail_section(cur, cur_sum, capital),
        "<h2>Filter study (current config)</h2>",
        "<p class='muted'>The current setup with each filter flipped (switched on if it is off, off if it is on) "
        "one at a time. With filters on, the 10-day entry window lets them confirm after the crossover.</p>",
        setups_table(study_sums, {}),
        "<h2>Other setups</h2>",
    ]
    for name, res in results.items():
        if name != current:
            html.append(f"<details><summary>{name}</summary>"
                        f"<img src='data:image/png;base64,{chart(res, capital)}' alt='equity chart'></details>")
    html.append("</main></body></html>")
    report = os.path.join(OUT_DIR, "report.html")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(html))
    print(f"\nReport: {report}\nCSV + charts: {PUBLISH_DIR}")


if __name__ == "__main__":
    main()
