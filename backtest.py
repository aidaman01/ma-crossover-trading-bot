"""Backtest the live strategy on daily data and compare filter combinations.

Usage:
    python backtest.py                 # full study, writes backtests/report.html
    python backtest.py --years 5 --cost-pct 0.05

The decision on each day comes from bot.evaluate() - the same function the live bot
uses - with the indicator settings, filters, exit rules and ATR stop from config.py.

Timing mirrors the scheduled live bot (9:35 ET): on day t the signal is computed from
the completed bars t-2 and t-1 (no look-ahead), the stop is checked against day t's
open, and orders fill at day t's open plus/minus BACKTEST_COST_PCT. Positions are
sized all-in (fractional shares, compounding) so returns compare directly with buy and
hold; config.TRADE_QTY / risk sizing only matter for live dollar amounts.
"""
import argparse
import base64
import contextlib
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
from indicators import add_indicators  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "backtests")             # HTML report (git-ignored)
PUBLISH_DIR = os.path.join(HERE, "docs", "backtest")  # CSVs + charts (committed; used by README/dashboard)
CHART_DIR = os.path.join(PUBLISH_DIR, "charts")

FILTER_FLAGS = {
    "trend": "USE_TREND_FILTER", "RSI": "USE_RSI_FILTER", "MACD": "USE_MACD_FILTER",
    "volume": "USE_VOLUME_FILTER", "ADX": "USE_ADX_FILTER",
}
ALL_FILTERS = "All filters on"
NO_FILTERS = "No filters (crossover only)"


def variants():
    all_on = {flag: True for flag in FILTER_FLAGS.values()}
    out = {ALL_FILTERS: all_on, NO_FILTERS: {flag: False for flag in all_on}}
    for name, flag in FILTER_FLAGS.items():
        out[f"Without {name} filter"] = {**all_on, flag: False}
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
    """Daily bars with indicators. Extra history before the test window warms them up."""
    start = pd.Timestamp.today().normalize() - pd.DateOffset(years=years + 2)
    df = yf.Ticker(symbol).history(start=start.strftime("%Y-%m-%d"), interval="1d", auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"yfinance returned no data for {symbol}")
    df = df[["Open", "High", "Low", "Close", "Volume"]]
    df.index = df.index.tz_localize(None).normalize()
    # Indicators are causal (rolling / exponential), so each row only uses data up to that day.
    return add_indicators(df, config)


# --------------------------------------------------------------------------- simulation
def simulate(df, symbol, variant, years, cost, capital):
    test_start = df.index[-1] - pd.DateOffset(years=years)
    start_i = max(2, int(df.index.searchsorted(test_start)))
    rows = list(df.itertuples())
    ready = df[bot.INDICATOR_COLUMNS].notna().all(axis=1).to_numpy()
    cash, shares, entry = float(capital), 0.0, None
    trades, equity, held = [], [], []

    for i in range(start_i, len(rows)):
        prev, last, today = rows[i - 2], rows[i - 1], rows[i]
        price = today.Open  # live bot runs at 9:35 ET, i.e. right after the open
        if ready[i - 2] and ready[i - 1]:
            decision = bot.evaluate(prev, last, price, shares, entry["stop"] if entry else None)
            if decision["signal"] == "BUY":
                fill = price * (1 + cost)
                shares, cash = cash / fill, 0.0
                entry = {"date": today.Index, "price": fill, "shares": shares, "cost": shares * fill,
                         "stop": fill - config.ATR_STOP_MULTIPLIER * last.atr, "i": i}
            elif decision["signal"] == "SELL":
                fill = price * (1 - cost)
                cash, shares = shares * fill, 0.0
                trades.append(trade_record(symbol, variant, entry, today.Index, fill, cash,
                                           ", ".join(decision["exit_rules"]), i, "closed"))
                entry = None
        equity.append(cash + shares * today.Close)
        held.append(shares > 0)

    if entry:  # still open at the end: mark to the last close
        last_close = rows[-1].Close
        trades.append(trade_record(symbol, variant, entry, rows[-1].Index, last_close,
                                   entry["shares"] * last_close, "open at end of test", len(rows) - 1, "open"))

    index = df.index[start_i:]
    test = df.iloc[start_i:]
    bh_shares = capital / (test["Open"].iloc[0] * (1 + cost))
    return {
        "symbol": symbol, "variant": variant,
        "equity": pd.Series(equity, index=index),
        "bh": test["Close"] * bh_shares,
        "held": pd.Series(held, index=index),
        "trades": trades,
    }


def trade_record(symbol, variant, entry, exit_date, exit_price, proceeds, rule, exit_i, status):
    pnl = proceeds - entry["cost"]
    return {
        "symbol": symbol, "variant": variant, "status": status,
        "entry_date": entry["date"].date(), "entry_price": entry["price"],
        "exit_date": exit_date.date(), "exit_price": exit_price, "shares": entry["shares"],
        "stop": entry["stop"], "pnl": pnl, "pnl_pct": pnl / entry["cost"],
        "days_held": exit_i - entry["i"], "exit_rule": rule,
    }


# --------------------------------------------------------------------------- metrics
def curve_stats(curve, capital):
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    peak = curve.cummax().clip(lower=capital)
    return {
        "total": curve.iloc[-1] / capital - 1,
        "cagr": (curve.iloc[-1] / capital) ** (1 / years) - 1,
        "max_dd": (curve / peak - 1).min(),
    }


def yearly_returns(curve, capital):
    year_end = curve.groupby(curve.index.year).last()
    start = year_end.shift(1).fillna(capital)
    return year_end / start - 1


def summarize(result, capital):
    strat, bh = curve_stats(result["equity"], capital), curve_stats(result["bh"], capital)
    closed = [t for t in result["trades"] if t["status"] == "closed"]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    worst = min(closed, key=lambda t: t["pnl"]) if closed else None
    mar = strat["cagr"] / abs(strat["max_dd"]) if strat["max_dd"] < 0 else float("nan")
    return {
        "symbol": result["symbol"], "variant": result["variant"],
        "total": strat["total"], "cagr": strat["cagr"], "max_dd": strat["max_dd"], "mar": mar,
        "bh_total": bh["total"], "bh_cagr": bh["cagr"], "bh_max_dd": bh["max_dd"],
        "trades": len(closed), "open_trade": len(closed) != len(result["trades"]),
        "win_rate": len(wins) / len(closed) if closed else float("nan"),
        "avg_win": sum(t["pnl_pct"] for t in wins) / len(wins) if wins else float("nan"),
        "avg_loss": sum(t["pnl_pct"] for t in losses) / len(losses) if losses else float("nan"),
        "worst_pct": worst["pnl_pct"] if worst and worst["pnl"] < 0 else float("nan"),
        "worst_usd": worst["pnl"] if worst and worst["pnl"] < 0 else float("nan"),
        "exposure": result["held"].mean(),
    }


# --------------------------------------------------------------------------- output
def pct(x, signed=True):
    if pd.isna(x):
        return "–"
    return f"{x * 100:+.1f}%" if signed else f"{x * 100:.1f}%"


def usd(x):
    return "–" if pd.isna(x) else f"${x:,.0f}"


def chart(result, capital):
    eq, bh = result["equity"], result["bh"]
    fig, ax = plt.subplots(figsize=(9, 3.4), dpi=110)
    for t in result["trades"]:
        ax.axvspan(pd.Timestamp(t["entry_date"]), pd.Timestamp(t["exit_date"]), color="#2563eb", alpha=0.08, lw=0)
    ax.plot(bh.index, bh.values, color="#9ca3af", lw=1.3, label="Buy & hold")
    ax.plot(eq.index, eq.values, color="#2563eb", lw=1.8, label="Strategy")
    ax.axhline(capital, color="#d1d5db", lw=0.8, ls="--")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v / 1000:,.0f}k"))
    ax.set_title(f"{result['symbol']} — {result['variant']}  (shaded = holding)", fontsize=10, loc="left")
    ax.grid(alpha=0.25)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    slug = "".join(c if c.isalnum() else "_" for c in result["variant"]).strip("_").lower()
    fig.savefig(os.path.join(CHART_DIR, f"{result['symbol']}_{slug}.png"), bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def comparison_table(summaries, symbols, best=None):
    head = "".join(f"<th colspan='6' class='grp'>{s}</th>" for s in symbols)
    sub = "".join("<th>Total</th><th>CAGR</th><th>Max DD</th><th>Trades</th><th>Win</th><th>In mkt</th>"
                  for _ in symbols)
    rows = []
    bh_cells = "".join(
        f"<td>{pct(summaries[s][0]['bh_total'])}</td><td>{pct(summaries[s][0]['bh_cagr'])}</td>"
        f"<td>{pct(summaries[s][0]['bh_max_dd'])}</td><td>–</td><td>–</td><td>100%</td>" for s in symbols)
    rows.append(f"<tr class='bh'><td>Buy &amp; hold</td>{bh_cells}</tr>")
    for k in range(len(summaries[symbols[0]])):
        name = summaries[symbols[0]][k]["variant"]
        cells = ""
        for s in symbols:
            m = summaries[s][k]
            beat = "pos" if m["total"] > m["bh_total"] else ""
            cells += (f"<td class='{beat}'>{pct(m['total'])}</td><td>{pct(m['cagr'])}</td>"
                      f"<td>{pct(m['max_dd'])}</td><td>{m['trades']}{'+1' if m['open_trade'] else ''}</td>"
                      f"<td>{pct(m['win_rate'], False)}</td><td>{pct(m['exposure'], False)}</td>")
        mark = " <span class='badge'>best</span>" if name == best else ""
        rows.append(f"<tr><td>{name}{mark}</td>{cells}</tr>")
    return (f"<div class='scroll'><table><thead><tr><th></th>{head}</tr><tr><th>Variant</th>{sub}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def detail_section(result, m, capital, open_=True):
    yr_s, yr_b = yearly_returns(result["equity"], capital), yearly_returns(result["bh"], capital)
    years = "".join(f"<tr><td>{y}</td><td>{pct(yr_s[y])}</td><td>{pct(yr_b[y])}</td></tr>" for y in yr_s.index)
    trades = "".join(
        f"<tr><td>{t['entry_date']}</td><td>{t['exit_date']}{' (open)' if t['status'] == 'open' else ''}</td>"
        f"<td>${t['entry_price']:,.2f}</td><td>${t['exit_price']:,.2f}</td><td>${t['stop']:,.2f}</td>"
        f"<td class='{'pos' if t['pnl'] > 0 else 'neg'}'>{usd(t['pnl'])}</td>"
        f"<td class='{'pos' if t['pnl'] > 0 else 'neg'}'>{pct(t['pnl_pct'])}</td>"
        f"<td>{t['days_held']}</td><td>{t['exit_rule']}</td></tr>" for t in result["trades"])
    trades = trades or "<tr><td colspan='9'>No trades in the test period.</td></tr>"
    metrics = (
        "<table class='kv'>"
        "<tr><th></th><th>Strategy</th><th>Buy &amp; hold</th></tr>"
        f"<tr><td>Total return</td><td>{pct(m['total'])}</td><td>{pct(m['bh_total'])}</td></tr>"
        f"<tr><td>Yearly return (CAGR)</td><td>{pct(m['cagr'])}</td><td>{pct(m['bh_cagr'])}</td></tr>"
        f"<tr><td>Max drawdown</td><td>{pct(m['max_dd'])}</td><td>{pct(m['bh_max_dd'])}</td></tr>"
        f"<tr><td>Closed trades</td><td>{m['trades']}{' (+1 open)' if m['open_trade'] else ''}</td><td>1</td></tr>"
        f"<tr><td>Win rate</td><td>{pct(m['win_rate'], False)}</td><td></td></tr>"
        f"<tr><td>Average win / loss</td><td>{pct(m['avg_win'])} / {pct(m['avg_loss'])}</td><td></td></tr>"
        f"<tr><td>Biggest loss</td><td>{pct(m['worst_pct'])} ({usd(m['worst_usd'])})</td><td></td></tr>"
        f"<tr><td>Time holding the ETF</td><td>{pct(m['exposure'], False)}</td><td>100%</td></tr>"
        "</table>")
    return (
        f"<details {'open' if open_ else ''}><summary>{result['symbol']} — {result['variant']}: "
        f"{pct(m['total'])} vs buy &amp; hold {pct(m['bh_total'])}</summary>"
        f"<img src='data:image/png;base64,{chart(result, capital)}' alt='equity chart'>"
        f"<div class='cols'>{metrics}<table class='kv'><tr><th>Year</th><th>Strategy</th><th>Buy &amp; hold</th></tr>"
        f"{years}</table></div>"
        "<div class='scroll'><table><thead><tr><th>Entry</th><th>Exit</th><th>Entry price</th><th>Exit price</th>"
        "<th>Stop</th><th>P/L</th><th>P/L %</th><th>Days</th><th>Exit rule</th></tr></thead>"
        f"<tbody>{trades}</tbody></table></div></details>")


CSS = """
body{font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:#111827;background:#fff;margin:0}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:36px 0 8px;border-bottom:1px solid #e5e7eb;padding-bottom:4px}
.muted{color:#6b7280}.note{background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:10px 14px}
table{border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13px}
th,td{padding:4px 9px;text-align:right;border-bottom:1px solid #f0f0f0;white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:#374151;font-weight:600}
th.grp{text-align:center;border-bottom:2px solid #d1d5db}
tr.bh td{color:#6b7280;font-style:italic}.pos{color:#047857}.neg{color:#b91c1c}
.badge{background:#2563eb;color:#fff;border-radius:4px;padding:0 5px;font-size:11px}
.scroll{overflow-x:auto;margin:8px 0}.cols{display:flex;gap:32px;flex-wrap:wrap;margin:8px 0}
details{border:1px solid #e5e7eb;border-radius:8px;padding:8px 14px;margin:12px 0}
summary{cursor:pointer;font-weight:600}img{max-width:100%;margin-top:8px}
"""


# --------------------------------------------------------------------------- study
def run_variant(data, symbol, name, settings, years, cost, capital):
    with config_overrides(settings):
        return simulate(data[symbol], symbol, name, years, cost, capital)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--years", type=int, default=config.BACKTEST_YEARS)
    parser.add_argument("--cost-pct", type=float, default=config.BACKTEST_COST_PCT,
                        help="cost per side in percent of trade value")
    args = parser.parse_args(argv)
    years, cost, capital = args.years, args.cost_pct / 100, config.BACKTEST_START_CAPITAL
    main_symbols, extra = list(config.SYMBOLS), list(config.BACKTEST_EXTRA_SYMBOLS)
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(CHART_DIR, exist_ok=True)

    print(f"Downloading {years}y (+2y warm-up) of daily data...")
    data = {s: load(s, years) for s in main_symbols + extra}

    # 1. Filter study on the main symbols
    results = {s: [run_variant(data, s, n, v, years, cost, capital) for n, v in variants().items()]
               for s in main_symbols}
    summaries = {s: [summarize(r, capital) for r in results[s]] for s in main_symbols}

    # Best = highest average CAGR / |max drawdown| across the main symbols (needs trades on each).
    def score(k):
        ms = [summaries[s][k] for s in main_symbols]
        return float("-inf") if any(m["trades"] == 0 or pd.isna(m["mar"]) for m in ms) \
            else sum(m["mar"] for m in ms) / len(ms)
    names = list(variants())
    best_k = max(range(len(names)), key=score)
    best = names[best_k]

    # 2. Robustness: best variant (and the current all-filters setup) on other ETFs
    robust_names = [best] if best == ALL_FILTERS else [best, ALL_FILTERS]
    robust = {s: [run_variant(data, s, n, variants()[n], years, cost, capital) for n in robust_names]
              for s in extra}
    robust_sum = {s: [summarize(r, capital) for r in robust[s]] for s in extra}

    # CSV outputs
    all_results = [r for s in main_symbols for r in results[s]] + [r for s in extra for r in robust[s]]
    all_sums = [m for s in main_symbols for m in summaries[s]] + [m for s in extra for m in robust_sum[s]]
    pd.DataFrame(all_sums).to_csv(os.path.join(PUBLISH_DIR, "summary.csv"), index=False)
    pd.DataFrame([t for r in all_results for t in r["trades"]]).to_csv(os.path.join(PUBLISH_DIR, "trades.csv"), index=False)

    # Console summary
    for s in main_symbols + extra:
        sums = summaries.get(s) or robust_sum[s]
        print(f"\n{s}  buy&hold {pct(sums[0]['bh_total'])}  CAGR {pct(sums[0]['bh_cagr'])}  "
              f"maxDD {pct(sums[0]['bh_max_dd'])}")
        for m in sums:
            print(f"  {m['variant']:<30} total {pct(m['total']):>8}  CAGR {pct(m['cagr']):>7}  "
                  f"maxDD {pct(m['max_dd']):>7}  trades {m['trades']:>3}  win {pct(m['win_rate'], False):>6}  "
                  f"in-mkt {pct(m['exposure'], False):>6}")
    print(f"\nBest variant (avg CAGR / |max DD| on {', '.join(main_symbols)}): {best}")

    # HTML report
    start, end = results[main_symbols[0]][0]["equity"].index[[0, -1]]
    html = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Strategy Backtest</title><style>{CSS}</style></head><body><main>",
            "<h1>Strategy backtest</h1>",
            f"<p class='muted'>{start:%Y-%m-%d} to {end:%Y-%m-%d} · start capital {usd(capital)} · "
            f"cost {args.cost_pct:.2f}% per side · generated {pd.Timestamp.now():%Y-%m-%d %H:%M}</p>",
            "<p class='note'>Same decision function as the live bot (<code>bot.evaluate</code>), settings from "
            f"config.py: SMA {config.FAST_MA}/{config.SLOW_MA}, trend SMA {config.TREND_MA}, RSI {config.RSI_PERIOD} "
            f"in [{config.RSI_BUY_MIN}, {config.RSI_BUY_MAX}], MACD {config.MACD_FAST}/{config.MACD_SLOW}/{config.MACD_SIGNAL}, "
            f"volume &gt; {config.VOLUME_MA}d avg, ADX {config.ADX_PERIOD} &gt; {config.ADX_MIN}; exits: MA cross down, "
            f"RSI &gt; {config.RSI_EXIT_ABOVE}, stop {config.ATR_STOP_MULTIPLIER}×ATR({config.ATR_PERIOD}) below entry. "
            "Signals use completed bars only (through yesterday); orders fill at the next open, like the 9:35 ET "
            "scheduled run. The stop is checked once a day at the open, as live. All-in sizing; prices are "
            "dividend-adjusted for both strategy and buy &amp; hold.</p>",
            "<h2>Which filters help? (" + ", ".join(main_symbols) + ")</h2>",
            "<p class='muted'>Green total = beat buy &amp; hold. Trades “+1” = a position still open at the end. "
            "Best = highest average CAGR ÷ |max drawdown| across the symbols.</p>",
            comparison_table(summaries, main_symbols, best),
            f"<h2>Detail — {ALL_FILTERS} (current config)</h2>"]
    for s in main_symbols:
        html.append(detail_section(results[s][0], summaries[s][0], capital))
    more_details = [(NO_FILTERS, names.index(NO_FILTERS))]
    if best not in (ALL_FILTERS, NO_FILTERS):
        more_details.append((f"best variant: {best}", best_k))
    for title, k in more_details:
        html.append(f"<h2>Detail — {title}</h2>")
        for s in main_symbols:
            html.append(detail_section(results[s][k], summaries[s][k], capital))
    html.append(f"<h2>Robustness check — other ETFs</h2>"
                f"<p class='muted'>“{best}” chosen on {', '.join(main_symbols)}, then run unchanged on these.</p>")
    html.append(comparison_table(robust_sum, extra, best))
    for s in extra:
        for r, m in zip(robust[s], robust_sum[s]):
            html.append(detail_section(r, m, capital, open_=False))
    html.append("</main></body></html>")
    report = os.path.join(OUT_DIR, "report.html")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(html))
    print(f"\nReport: {report}\nCSV:    {os.path.join(PUBLISH_DIR, 'summary.csv')}, {os.path.join(PUBLISH_DIR, 'trades.csv')}")


if __name__ == "__main__":
    main()
