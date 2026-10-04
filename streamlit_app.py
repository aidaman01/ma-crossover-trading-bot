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

FILTERS = {"trend": ("Trend", f"Close > SMA{config.TREND_MA}"),
           "rsi_filter": ("RSI", f"RSI {config.RSI_BUY_MIN}–{config.RSI_BUY_MAX}"),
           "macd_filter": ("MACD", "MACD > signal"),
           "volume_filter": ("Volume", f"Volume > {config.VOLUME_MA}d avg"),
           "adx_filter": ("ADX", f"ADX > {config.ADX_MIN}")}
ICON = {"PASS": "✅", "FAIL": "❌", "OFF": "–"}
FAST, SLOW = bot.ma_label(config.FAST_MA), bot.ma_label(config.SLOW_MA)
ENABLED = [label for key, (label, _) in FILTERS.items()
           if getattr(config, {"trend": "USE_TREND_FILTER", "rsi_filter": "USE_RSI_FILTER",
                               "macd_filter": "USE_MACD_FILTER", "volume_filter": "USE_VOLUME_FILTER",
                               "adx_filter": "USE_ADX_FILTER"}[key])]
SETUP_TEXT = (f"{FAST}/{SLOW} crossover · entry filters: {', '.join(ENABLED) if ENABLED else 'none'} · "
              f"{config.ENTRY_WINDOW_DAYS}-day entry window · {len(config.SYMBOLS)} ETFs · "
              f"{'equal-weight positions' if config.POSITION_SIZING == 'equal_weight' else config.POSITION_SIZING + ' sizing'}")

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
    return {"equity": float(a.equity), "last_equity": float(a.last_equity), "cash": float(a.cash)}


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
    """Latest indicator values and the bot's decision, using the bot's own code."""
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
    df = pd.read_csv(RUNS_CSV).rename(columns=bot.CSV_RENAMES)
    df["run_time"] = pd.to_datetime(df["run_time"])
    return df.sort_values("run_time", ascending=False)


@st.cache_data(show_spinner=False)
def load_backtest():
    def read(name):
        path = os.path.join(BACKTEST_DIR, name)
        return pd.read_csv(path) if os.path.exists(path) else pd.DataFrame()
    return read("summary.csv"), read("trades.csv"), read("yearly.csv")


def chart_path(setup_name):
    slug = "_".join("".join(c if c.isalnum() else " " for c in setup_name).lower().split())
    path = os.path.join(BACKTEST_DIR, "charts", f"{slug}.png")
    return path if os.path.exists(path) else None


def money(x):
    return f"${x:,.2f}"


PCT = st.column_config.NumberColumn(format="%.1f%%")


def setups_frame(df):
    """Backtest summary rows -> display table (percentages as numbers for sorting)."""
    current = df["is_current"].eq(True) if "is_current" in df else pd.Series(False, index=df.index)
    out = pd.DataFrame({
        "Setup": df["setup"] + current.map({True: " ⭐ current", False: ""}),
        "Trades/mo": df["trades_per_month"].round(1),
        "Total %": df["total"] * 100, "CAGR %": df["cagr"] * 100, "Max DD %": df["max_dd"] * 100,
        "CAGR/DD": df["mar"].round(2), "Win rate %": df["win_rate"] * 100,
        "Basket total %": df["basket_total"] * 100, "Basket CAGR %": df["basket_cagr"] * 100,
        "Basket max DD %": df["basket_max_dd"] * 100,
    })
    return out


# --------------------------------------------------------------------------- layout
st.title("📈 MA Crossover Bot")
st.caption(f"{SETUP_TEXT} · [Source on GitHub]({REPO_URL})")
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
        invested = positions["Market value"].sum() if not positions.empty else 0.0
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Account value", money(acct["equity"]),
                  f"{day_change:+,.2f} today" if abs(day_change) >= 0.01 else None)
        c2.metric("Cash", money(acct["cash"]))
        c3.metric("Open positions", f"{len(positions)} of {len(config.SYMBOLS)}")
        c3.caption(f"{invested / acct['equity']:.0%} of equity invested")
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
    st.caption(f"Bot mode: **{'Dry run' if config.DRY_RUN else 'Live orders on the paper account'}** · "
               f"Each ETF gets up to 1/{len(config.SYMBOLS)} of equity · Account data refreshes every 5 minutes.")

# ---- Today's signals
with tab_signals:
    st.caption(f"Latest completed daily bar, evaluated with the bot's own decision function. A BUY needs a "
               f"{FAST}/{SLOW} up-crossover in the last {config.ENTRY_WINDOW_DAYS} days"
               + (f" and these filters to pass: {', '.join(ENABLED)}." if ENABLED else " (no entry filters enabled).")
               + " The bot's saved state (crossovers already traded) is not on this server, so a BUY shown "
                 "here may already have been taken.")
    market_open = bool(clock and clock["is_open"])
    signals, failed = {}, []
    with st.spinner("Loading market data..."):
        for symbol in config.SYMBOLS:
            pos = positions[positions["Symbol"] == symbol] if not positions.empty else pd.DataFrame()
            qty = float(pos["Qty"].iloc[0]) if not pos.empty else 0.0
            entry = float(pos["Avg entry"].iloc[0]) if not pos.empty else None
            try:
                signals[symbol] = (load_signal(symbol, market_open, qty, entry), qty)
            except Exception as exc:
                failed.append(f"{symbol} ({exc})")
    if failed:
        st.warning("Market data unavailable for: " + ", ".join(failed))
    if signals:
        rsi_col = f"RSI {config.RSI_PERIOD}"
        table = pd.DataFrame([{
            "Symbol": sym, "Signal": s["signal"], "Price": s["price"], "Held": qty,
            "Days since up-cross": (str(s["cross_age"]) if s["cross_age"] is not None
                                    and s["last"]["ma_fast"] > s["last"]["ma_slow"] else "–"),
            FAST: s["last"]["ma_fast"], SLOW: s["last"]["ma_slow"], rsi_col: s["last"]["rsi"],
            **{f"{label} filter": ICON[s["filters"][key]] for key, (label, _) in FILTERS.items()},
        } for sym, (s, qty) in signals.items()])
        st.dataframe(table, hide_index=True, width="stretch", column_config={
            "Price": st.column_config.NumberColumn(format="$%.2f"),
            FAST: st.column_config.NumberColumn(format="%.2f"), SLOW: st.column_config.NumberColumn(format="%.2f"),
            rsi_col: st.column_config.NumberColumn(format="%.1f"),
            "Held": st.column_config.NumberColumn(format="%g"),
            "Days since up-cross": st.column_config.TextColumn(help="– = fast MA is below the slow MA"),
        })
        st.caption(f"Bar date {next(iter(signals.values()))[0]['bar_date']} · ✅ pass · ❌ fail · – filter off")

        st.subheader("Details")
        symbol = st.selectbox("Symbol", list(signals), label_visibility="collapsed")
        s, qty = signals[symbol]
        last = s["last"]
        m1, m2, m3 = st.columns(3)
        m1.metric("Price", money(s["price"]))
        m2.metric("Signal", s["signal"])
        m3.metric("Crossover", s["crossover"], f"{s['cross_age']} day(s) since up-cross"
                  if s["cross_age"] is not None else None, delta_color="off", delta_arrow="off")
        if qty:
            st.markdown(f"**Holding:** {qty:g} shares, stop ≈ {money(s['stop'])}")
        d1, d2 = st.columns(2)
        d1.dataframe(pd.DataFrame([{"Filter": desc, "Result": {"PASS": "✅ Pass", "FAIL": "❌ Fail", "OFF": "Off"}[s["filters"][k]]}
                                   for k, (_, desc) in FILTERS.items()]), hide_index=True, width="stretch")
        d2.dataframe(pd.DataFrame([
            {"Indicator": FAST, "Value": f"{last['ma_fast']:.2f}"},
            {"Indicator": SLOW, "Value": f"{last['ma_slow']:.2f}"},
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
        st.subheader("Trade log")
        orders = runs[runs["action"].astype(str).str.contains("submitted", na=False)]
        if orders.empty:
            st.write("No orders submitted yet.")
        else:
            st.dataframe(orders[["run_time", "symbol", "signal", "qty", "price", "action", "reason"]],
                         hide_index=True, width="stretch",
                         column_config={"run_time": st.column_config.DatetimeColumn("Run (ET)", format="YYYY-MM-DD HH:mm")})
        st.subheader("All runs")
        symbols = st.multiselect("Symbols", sorted(runs["symbol"].unique()), default=None, placeholder="All symbols")
        view = runs[runs["symbol"].isin(symbols)] if symbols else runs
        cols = [c for c in ["run_time", "symbol", "price", "crossover", "cross_age", "trend", "rsi_filter",
                            "macd_filter", "volume_filter", "adx_filter", "signal", "qty", "action", "reason"]
                if c in view.columns]
        st.dataframe(view[cols], hide_index=True, width="stretch",
                     column_config={"run_time": st.column_config.DatetimeColumn("Run (ET)", format="YYYY-MM-DD HH:mm")})

# ---- Backtest
with tab_backtest:
    summary, bt_trades, yearly = load_backtest()
    if summary.empty or "study" not in summary.columns:
        st.info("No backtest results in docs/backtest/. Run `python backtest.py` and commit the output.")
    else:
        setups = summary[summary["study"] == "setups"].reset_index(drop=True)
        study = summary[summary["study"] == "filter study"].reset_index(drop=True)
        st.caption(f"{config.BACKTEST_YEARS} years of daily data, ${config.BACKTEST_START_CAPITAL:,} start, "
                   f"{config.BACKTEST_COST_PCT}% cost per side, no look-ahead, same decision and sizing code as "
                   "the live bot. One shared account per setup with equal-weight positions; basket = equal-weight "
                   "buy and hold of the same ETFs.")
        cfg = {c: PCT for c in ["Total %", "CAGR %", "Max DD %", "Win rate %",
                                "Basket total %", "Basket CAGR %", "Basket max DD %"]}
        st.subheader("Setup comparison")
        st.dataframe(setups_frame(setups), hide_index=True, width="stretch", column_config=cfg)
        if not study.empty:
            with st.expander("Filter study on the current setup"):
                st.dataframe(setups_frame(study), hide_index=True, width="stretch", column_config=cfg)

        st.subheader("Equity vs. buy & hold basket")
        names = setups["setup"].tolist()
        current = setups.loc[setups["is_current"].eq(True), "setup"]
        choice = st.selectbox("Setup", names, index=names.index(current.iloc[0]) if len(current) else 0)
        path = chart_path(choice)
        if path:
            st.image(path, width="stretch")
        c1, c2 = st.columns([1, 2])
        if not yearly.empty:
            y = yearly[yearly["setup"] == choice]
            c1.dataframe(pd.DataFrame({"Year": y["year"].astype(str), "Strategy %": y["strategy"] * 100,
                                       "Basket %": y["basket"] * 100}),
                         hide_index=True, width="stretch",
                         column_config={"Strategy %": PCT, "Basket %": PCT})
        if not bt_trades.empty:
            t = bt_trades[bt_trades["setup"] == choice]
            if not t.empty:
                per = t.groupby("symbol").agg(Trades=("pnl", "size"), Wins=("pnl", lambda p: int((p > 0).sum())),
                                              PnL=("pnl", "sum")).reset_index().rename(columns={"symbol": "Symbol"})
                c2.dataframe(per, hide_index=True, width="stretch",
                             column_config={"PnL": st.column_config.NumberColumn("P/L", format="$%.0f")})
                with st.expander(f"Every trade ({len(t)})"):
                    st.dataframe(t[["symbol", "entry_date", "exit_date", "entry_price", "exit_price", "shares",
                                    "pnl", "pnl_pct", "exit_rule"]].assign(pnl_pct=lambda d: d["pnl_pct"] * 100),
                                 hide_index=True, width="stretch", column_config={
                                     "entry_price": st.column_config.NumberColumn("Entry", format="$%.2f"),
                                     "exit_price": st.column_config.NumberColumn("Exit", format="$%.2f"),
                                     "pnl": st.column_config.NumberColumn("P/L", format="$%.0f"),
                                     "pnl_pct": st.column_config.NumberColumn("P/L %", format="%.2f%%"),
                                     "symbol": "Symbol", "entry_date": "Entry date", "exit_date": "Exit date",
                                     "exit_rule": "Exit rule"})

st.divider()
st.caption("Educational project. Paper trading only. Not financial advice. "
           f"[GitHub]({REPO_URL})")
