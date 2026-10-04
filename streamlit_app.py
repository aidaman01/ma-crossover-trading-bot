"""Read-only dashboard for the MA crossover bot (Streamlit).

Run locally:   streamlit run streamlit_app.py
Secrets:       ALPACA_API_KEY / ALPACA_SECRET_KEY from Streamlit secrets
               (.streamlit/secrets.toml locally, the app's Secrets box on Streamlit Cloud).

This app never places, changes or cancels orders: it only reads the account, positions
and portfolio history, plus files committed to the repo.
"""
import os

import altair as alt
import pandas as pd
import streamlit as st

import bot
import config
from indicators import add_indicators

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_CSV = os.path.join(HERE, "logs", "runs.csv")
BACKTEST_DIR = os.path.join(HERE, "docs", "backtest")
REPO_URL = "https://github.com/aidaman01/ma-crossover-trading-bot"

FILTER_LABELS = {"trend": f"Trend (close > SMA{config.TREND_MA})",
                 "rsi_filter": f"RSI {config.RSI_BUY_MIN}–{config.RSI_BUY_MAX}",
                 "macd_filter": "MACD > signal",
                 "volume_filter": f"Volume > {config.VOLUME_MA}d avg",
                 "adx_filter": f"ADX > {config.ADX_MIN}"}
STATUS_ICON = {"PASS": "✅ Pass", "FAIL": "❌ Fail", "OFF": "⚪ Off"}

st.set_page_config(page_title="MA Crossover Bot", page_icon="📈", layout="wide")


# --------------------------------------------------------------------------- Alpaca (read-only)
@st.cache_resource
def trading_client():
    """Alpaca client from Streamlit secrets, or None if secrets are missing."""
    try:
        key, secret = st.secrets["ALPACA_API_KEY"], st.secrets["ALPACA_SECRET_KEY"]
    except Exception:
        return None
    from alpaca.trading.client import TradingClient
    return TradingClient(key, secret, paper=True)


@st.cache_data(ttl=300, show_spinner=False)
def load_account():
    a = trading_client().get_account()
    return {"equity": float(a.equity), "last_equity": float(a.last_equity), "cash": float(a.cash),
            "buying_power": float(a.buying_power), "status": str(a.status.value if hasattr(a.status, "value") else a.status)}


@st.cache_data(ttl=300, show_spinner=False)
def load_positions():
    return pd.DataFrame([{
        "Symbol": p.symbol, "Qty": float(p.qty), "Avg entry": float(p.avg_entry_price),
        "Price": float(p.current_price), "Market value": float(p.market_value),
        "Unrealized P/L": float(p.unrealized_pl), "P/L %": float(p.unrealized_plpc) * 100,
    } for p in trading_client().get_all_positions()])


@st.cache_data(ttl=300, show_spinner=False)
def load_clock():
    c = trading_client().get_clock()
    return {"is_open": c.is_open, "next_open": c.next_open, "next_close": c.next_close}


@st.cache_data(ttl=900, show_spinner=False)
def load_portfolio_history(period):
    from alpaca.trading.requests import GetPortfolioHistoryRequest
    h = trading_client().get_portfolio_history(GetPortfolioHistoryRequest(period=period, timeframe="1D"))
    df = pd.DataFrame({"Date": pd.to_datetime(h.timestamp, unit="s"), "Equity": h.equity})
    return df.dropna().query("Equity > 0")


# --------------------------------------------------------------------------- strategy snapshot
@st.cache_data(ttl=900, show_spinner=False)
def load_signal(symbol, market_open, position_qty, entry_price):
    """Today's indicator values and the bot's decision, using the bot's own code."""
    raw = bot.fetch_bars(symbol)
    df = add_indicators(bot.signal_bars(raw, market_open), config)
    prev, last = df.iloc[-2], df.iloc[-1]
    price = float(raw["Close"].iloc[-1])
    stop = None
    if position_qty > 0 and entry_price:
        # The bot stores ATR at entry locally; the dashboard approximates with current ATR.
        stop = entry_price - config.ATR_STOP_MULTIPLIER * float(last.atr)
    decision = bot.evaluate(prev, last, price, position_qty, stop)
    return {"bar_date": last.name.date(), "price": price, "last": last.to_dict(), "stop": stop, **decision}


# --------------------------------------------------------------------------- repo files
@st.cache_data(ttl=600, show_spinner=False)
def load_runs():
    if not os.path.exists(RUNS_CSV):
        return pd.DataFrame()
    df = pd.read_csv(RUNS_CSV)
    df["run_time"] = pd.to_datetime(df["run_time"])
    return df.sort_values("run_time", ascending=False)


@st.cache_data(show_spinner=False)
def load_backtest():
    summary_path = os.path.join(BACKTEST_DIR, "summary.csv")
    trades_path = os.path.join(BACKTEST_DIR, "trades.csv")
    if not os.path.exists(summary_path):
        return pd.DataFrame(), pd.DataFrame()
    trades = pd.read_csv(trades_path) if os.path.exists(trades_path) else pd.DataFrame()
    return pd.read_csv(summary_path), trades


def chart_path(symbol, variant):
    slug = "".join(c if c.isalnum() else "_" for c in variant).strip("_").lower()
    path = os.path.join(BACKTEST_DIR, "charts", f"{symbol}_{slug}.png")
    return path if os.path.exists(path) else None


def money(x):
    return f"${x:,.2f}"


# --------------------------------------------------------------------------- layout
st.title("📈 MA Crossover Bot")
st.caption("20/50-day SMA crossover with trend, RSI, MACD, volume and ADX filters · "
           f"{', '.join(config.SYMBOLS)} · [Source on GitHub]({REPO_URL})")
st.warning("**Paper trading — not financial advice.** This dashboard shows a simulated Alpaca "
           "paper account for an educational project. It is read-only and cannot place trades.",
           icon="⚠️")

client = trading_client()
clock, positions = None, pd.DataFrame()
if client is None:
    st.info("Alpaca secrets are not configured, so account data is hidden. "
            "Add `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in the app's Streamlit secrets.")
else:
    try:
        clock = load_clock()
        positions = load_positions()
    except Exception as exc:
        st.error(f"Could not reach Alpaca: {exc}")
        client = None

# ?tab=signals|runs|backtest opens that tab directly (handy for sharing links)
TABS = {"account": "Account", "signals": "Today's signals", "runs": "Run history", "backtest": "Backtest"}
tab_overview, tab_signals, tab_runs, tab_backtest = st.tabs(
    list(TABS.values()), default=TABS.get(st.query_params.get("tab", ""), "Account"))

# ---- Account
with tab_overview:
    if client is None:
        st.write("Account data unavailable.")
    else:
        acct = load_account()
        day_change = acct["equity"] - acct["last_equity"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Account value", money(acct["equity"]),
                  f"{day_change:+,.2f} today" if abs(day_change) >= 0.01 else None)
        c2.metric("Cash", money(acct["cash"]))
        c3.metric("Open positions", len(positions))
        c4.metric("Market", "Open" if clock["is_open"] else "Closed")
        c4.caption(f"Next {'close' if clock['is_open'] else 'open'}: "
                   f"{(clock['next_close'] if clock['is_open'] else clock['next_open']):%a %d %b, %H:%M} ET")

        st.subheader("Equity curve")
        period = st.radio("Period", ["1M", "3M", "6M", "1A"], index=3, horizontal=True,
                          format_func=lambda p: {"1A": "1Y"}.get(p, p), label_visibility="collapsed")
        try:
            hist = load_portfolio_history(period)
            if len(hist) < 5:
                st.info(f"Only {len(hist)} day(s) of account history so far. "
                        "The curve fills in as trading days pass.")
            else:
                st.altair_chart(
                    alt.Chart(hist).mark_line(strokeWidth=2).encode(
                        x=alt.X("Date:T", title=None, axis=alt.Axis(format="%b %d")),
                        y=alt.Y("Equity:Q", title=None, scale=alt.Scale(zero=False),
                                axis=alt.Axis(format="$,.0f")),
                        tooltip=[alt.Tooltip("Date:T", format="%Y-%m-%d"),
                                 alt.Tooltip("Equity:Q", format="$,.2f")]
                    ).properties(height=300),
                    width="stretch")
        except Exception as exc:
            st.warning(f"Portfolio history unavailable: {exc}")

        st.subheader("Open positions")
        if positions.empty:
            st.write("No open positions.")
        else:
            st.dataframe(positions, hide_index=True, width="stretch", column_config={
                c: st.column_config.NumberColumn(format="$%.2f")
                for c in ["Avg entry", "Price", "Market value", "Unrealized P/L"]
            } | {"P/L %": st.column_config.NumberColumn(format="%.2f%%")})
    st.caption(f"Bot mode: **{'Dry run' if config.DRY_RUN else 'Live (paper)'}** · "
               "Account data refreshes every 5 minutes.")

# ---- Today's signals
with tab_signals:
    st.caption("Indicator values from the last completed daily bar, evaluated with the bot's own "
               "decision function. A BUY needs an up-crossover **and** every enabled filter to pass.")
    market_open = bool(clock and clock["is_open"])
    cols = st.columns(len(config.SYMBOLS))
    for col, symbol in zip(cols, config.SYMBOLS):
        with col:
            st.subheader(symbol)
            pos = positions[positions["Symbol"] == symbol] if not positions.empty else pd.DataFrame()
            qty = float(pos["Qty"].iloc[0]) if not pos.empty else 0.0
            entry = float(pos["Avg entry"].iloc[0]) if not pos.empty else None
            try:
                s = load_signal(symbol, market_open, qty, entry)
            except Exception as exc:
                st.warning(f"Market data unavailable: {exc}")
                continue
            last = s["last"]
            m1, m2 = st.columns(2)
            m1.metric("Price", money(s["price"]))
            m2.metric("Signal", s["signal"])
            st.markdown(f"**Crossover:** {s['crossover']} &nbsp;·&nbsp; **Bar:** {s['bar_date']}"
                        + (f" &nbsp;·&nbsp; **Holding:** {qty:g} sh, stop ≈ {money(s['stop'])}" if qty else ""))
            st.dataframe(pd.DataFrame(
                [{"Filter": FILTER_LABELS[k], "Result": STATUS_ICON[v]} for k, v in s["filters"].items()]),
                hide_index=True, width="stretch")
            st.dataframe(pd.DataFrame([
                {"Indicator": f"SMA {config.FAST_MA}", "Value": f"{last['sma_fast']:.2f}"},
                {"Indicator": f"SMA {config.SLOW_MA}", "Value": f"{last['sma_slow']:.2f}"},
                {"Indicator": f"SMA {config.TREND_MA}", "Value": f"{last['sma_trend']:.2f}"},
                {"Indicator": f"RSI {config.RSI_PERIOD}", "Value": f"{last['rsi']:.1f}"},
                {"Indicator": "MACD / signal", "Value": f"{last['macd']:.2f} / {last['macd_signal']:.2f}"},
                {"Indicator": f"Volume / {config.VOLUME_MA}d avg", "Value": f"{last['Volume'] / 1e6:.1f}M / {last['vol_avg'] / 1e6:.1f}M"},
                {"Indicator": f"ADX {config.ADX_PERIOD}", "Value": f"{last['adx']:.1f}"},
                {"Indicator": f"ATR {config.ATR_PERIOD}", "Value": f"{last['atr']:.2f}"},
            ]), hide_index=True, width="stretch")
            st.caption(s["reason"])

# ---- Run history
with tab_runs:
    runs = load_runs()
    if runs.empty:
        st.info("No run history in the repo yet (logs/runs.csv).")
    else:
        st.caption(f"From `logs/runs.csv` in the repo, {len(runs)} rows; last run "
                   f"{runs['run_time'].iloc[0]:%Y-%m-%d %H:%M} ET. Updates when new runs are pushed to GitHub.")
        trades = runs[runs["signal"].isin(["BUY", "SELL"])]
        st.subheader("Trade log")
        if trades.empty:
            st.write("No BUY or SELL signals yet.")
        else:
            st.dataframe(trades[["run_time", "symbol", "signal", "price", "action", "reason"]],
                         hide_index=True, width="stretch")
        st.subheader("All runs")
        symbols = st.multiselect("Symbols", sorted(runs["symbol"].unique()), default=None,
                                 placeholder="All symbols")
        view = runs[runs["symbol"].isin(symbols)] if symbols else runs
        st.dataframe(view[["run_time", "symbol", "price", "crossover", "trend", "rsi_filter", "macd_filter",
                           "volume_filter", "adx_filter", "signal", "action", "reason"]],
                     hide_index=True, width="stretch",
                     column_config={"run_time": st.column_config.DatetimeColumn("Run (ET)", format="YYYY-MM-DD HH:mm")})

# ---- Backtest
with tab_backtest:
    summary, bt_trades = load_backtest()
    if summary.empty:
        st.info("No backtest results in docs/backtest/. Run `python backtest.py` and commit the output.")
    else:
        st.caption(f"{config.BACKTEST_YEARS} years of daily data, ${config.BACKTEST_START_CAPITAL:,} start, "
                   f"{config.BACKTEST_COST_PCT}% cost per side, no look-ahead. Same decision code as the live bot.")
        main = summary[summary["symbol"].isin(config.SYMBOLS)]
        rows = []
        for symbol in config.SYMBOLS:
            sm = main[main["symbol"] == symbol]
            if sm.empty:
                continue
            rows.append({"Symbol": symbol, "Variant": "Buy & hold", "Total %": sm["bh_total"].iloc[0] * 100,
                         "CAGR %": sm["bh_cagr"].iloc[0] * 100, "Max DD %": sm["bh_max_dd"].iloc[0] * 100,
                         "Trades": None, "Win rate %": None, "In market %": 100.0})
            for _, r in sm.iterrows():
                rows.append({"Symbol": symbol, "Variant": r["variant"], "Total %": r["total"] * 100,
                             "CAGR %": r["cagr"] * 100, "Max DD %": r["max_dd"] * 100, "Trades": r["trades"],
                             "Win rate %": r["win_rate"] * 100, "In market %": r["exposure"] * 100})
        pct = st.column_config.NumberColumn(format="%.1f%%")
        st.subheader("Filter comparison")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={c: pct for c in ["Total %", "CAGR %", "Max DD %", "Win rate %", "In market %"]})

        st.subheader("Equity vs. buy & hold")
        c1, c2 = st.columns(2)
        symbol = c1.selectbox("Symbol", sorted(summary["symbol"].unique(),
                                                key=lambda s: (s not in config.SYMBOLS, s)))
        variants = summary[summary["symbol"] == symbol]["variant"].tolist()
        with_charts = [v for v in variants if chart_path(symbol, v)] or variants
        default = with_charts.index("No filters (crossover only)") if "No filters (crossover only)" in with_charts else 0
        variant = c2.selectbox("Variant", with_charts, index=default)
        path = chart_path(symbol, variant)
        if path:
            st.image(path, width="stretch")
        else:
            st.write("No chart saved for this variant.")
        if not bt_trades.empty:
            t = bt_trades[(bt_trades["symbol"] == symbol) & (bt_trades["variant"] == variant)]
            st.dataframe(t[["entry_date", "exit_date", "entry_price", "exit_price", "pnl", "pnl_pct", "exit_rule"]]
                         .assign(pnl_pct=lambda d: d["pnl_pct"] * 100),
                         hide_index=True, width="stretch", column_config={
                             "entry_price": st.column_config.NumberColumn("Entry", format="$%.2f"),
                             "exit_price": st.column_config.NumberColumn("Exit", format="$%.2f"),
                             "pnl": st.column_config.NumberColumn("P/L", format="$%.0f"),
                             "pnl_pct": st.column_config.NumberColumn("P/L %", format="%.2f%%"),
                             "entry_date": "Entry date", "exit_date": "Exit date", "exit_rule": "Exit rule"})

st.divider()
st.caption("Educational project. Paper trading only. Not financial advice. "
           f"[GitHub]({REPO_URL})")
