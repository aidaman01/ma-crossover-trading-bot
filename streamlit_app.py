"""Read-only dashboard for the trading bot (Streamlit).

Run locally:   streamlit run streamlit_app.py
Secrets:       ALPACA_API_KEY / ALPACA_SECRET_KEY from Streamlit secrets
               (.streamlit/secrets.toml locally, the app's Secrets box on Streamlit Cloud).

This app never places, changes or cancels orders: it only reads the account, positions,
portfolio history and market calendar, plus files committed to the repo. Signals are
computed with the bot's own code (allocation.py / bot.py).
"""
import datetime as dt
import importlib
import json
import os

import altair as alt
import pandas as pd
import streamlit as st

import allocation
import bot
import config
import indicators
import metrics

# Streamlit Cloud updates the repo files in place without restarting Python, so modules
# imported by an earlier version of the app can be stale. Reload them on every run
# (config first, since the others read it at import time).
for _module in (config, indicators, allocation, bot):
    importlib.reload(_module)

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_CSV = os.path.join(HERE, "logs", "runs.csv")
ALLOC_CSV = os.path.join(HERE, "logs", "allocation_runs.csv")
RESEARCH_DIR = os.path.join(HERE, "docs", "research")
BACKTEST_DIR = os.path.join(HERE, "docs", "backtest")
REPO_URL = "https://github.com/aidaman01/ma-crossover-trading-bot"
IS_ALLOCATION = config.STRATEGY in bot.ALLOCATION_STRATEGIES
TRANCHE_DAYS = [allocation.tranche_day(k, config.TRANCHE_SPACING_DAYS) for k in range(config.TRANCHES)]

if config.STRATEGY == "trend_allocation":
    STRATEGY_NAME = "Trend allocation"
    SETUP_TEXT = (f"{len(config.ALLOCATION_ASSETS)} asset classes ({' '.join(config.ALLOCATION_ASSETS)}) · "
                  f"1/{len(config.ALLOCATION_ASSETS)} each while above its {config.ALLOCATION_MA_MONTHS}-month "
                  f"average, else cash ({config.CASH_SYMBOL}) · {config.TRANCHES}-part split rebalancing on "
                  f"trading days {', '.join(map(str, TRANCHE_DAYS))}")
elif config.STRATEGY == "hold_by_default":
    STRATEGY_NAME = "Hold by default"
    SETUP_TEXT = (f"{len(config.HOLD_ASSETS)} ETFs equal weight, each in cash while below its "
                  f"{config.HOLD_SMA_DAYS}-day average · {config.TRANCHES}-part split rebalancing")
else:
    STRATEGY_NAME = "EMA crossover (setup J)"
    SETUP_TEXT = (f"{bot.ma_label(config.FAST_MA)}/{bot.ma_label(config.SLOW_MA)} crossover · "
                  f"{len(config.SYMBOLS)} ETFs · equal-weight positions")

st.set_page_config(page_title="Trend Allocation Bot", page_icon="📈", layout="wide")
PCT = st.column_config.NumberColumn(format="%.1f%%")


# --------------------------------------------------------------------------- Alpaca (read-only)
def alpaca_keys():
    """(key, secret) from Streamlit secrets, or None if missing.

    Read on every run (not cached) so secrets added or changed in the app settings
    take effect without restarting the app.
    """
    try:
        return str(st.secrets["ALPACA_API_KEY"]).strip(), str(st.secrets["ALPACA_SECRET_KEY"]).strip()
    except Exception:
        return None


def is_placeholder(value):
    return not value or value.lower().startswith("your_") or value.lower().startswith("your ")


@st.cache_resource
def _client_for(key, secret):
    from alpaca.trading.client import TradingClient
    return TradingClient(key, secret, paper=True)


def trading_client():
    """Alpaca client for the current secrets, or None if they are missing or placeholders."""
    keys = alpaca_keys()
    if keys is None or any(is_placeholder(k) for k in keys):
        return None
    return _client_for(*keys)


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


@st.cache_data(ttl=3600, show_spinner=False)
def load_trading_day(day):
    return bot.trading_day_of_month(trading_client(), day)


@st.cache_data(ttl=900, show_spinner=False)
def load_portfolio_history(period):
    from alpaca.trading.requests import GetPortfolioHistoryRequest
    h = trading_client().get_portfolio_history(GetPortfolioHistoryRequest(period=period, timeframe="1D"))
    df = pd.DataFrame({"Date": pd.to_datetime(h.timestamp, unit="s"), "Equity": h.equity})
    return df.dropna().query("Equity > 0")


# --------------------------------------------------------------------------- signals (bot code)
@st.cache_data(ttl=900, show_spinner=False)
def load_allocation_signals(market_open):
    """Latest trend signals and target weights, computed exactly like bot.run_allocation."""
    universe, signal_fn = bot.allocation_universe()
    closes, latest = bot.fetch_closes(universe + [config.CASH_SYMBOL], market_open)
    s = closes.index[-1]
    today = pd.Timestamp.now(tz="America/New_York").date()
    signals = signal_fn(allocation.Prices(closes[universe]), s, s.month != today.month)
    weights = allocation.weights_from(signals)
    weights[config.CASH_SYMBOL] = max(0.0, 1.0 - sum(weights.values()))
    return {"signal_date": s.date(), "signals": signals, "weights": weights, "latest": latest}


@st.cache_data(ttl=900, show_spinner=False)
def load_crossover_signal(symbol, market_open, position_qty, entry_price):
    raw = bot.fetch_bars(symbol)
    df = indicators.add_indicators(bot.signal_bars(raw, market_open), config)
    prev, last = df.iloc[-2], df.iloc[-1]
    price = float(raw["Close"].iloc[-1])
    stop = entry_price - config.ATR_STOP_MULTIPLIER * float(last.atr) if position_qty > 0 and entry_price else None
    decision = bot.evaluate(prev, last, price, position_qty, stop)
    return {"bar_date": last.name.date(), "price": price, "last": last.to_dict(), "stop": stop, **decision}


# --------------------------------------------------------------------------- repo files
@st.cache_data(ttl=600, show_spinner=False)
def load_csv(path, rename=None):
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path).rename(columns=rename or {})
    if "run_time" in df:
        df["run_time"] = pd.to_datetime(df["run_time"])
        df = df.sort_values("run_time", ascending=False)
    return df


def image(path, caption=None):
    if os.path.exists(path):
        st.image(path, width="stretch", caption=caption)


def money(x):
    return f"${x:,.2f}"


# --------------------------------------------------------------------------- layout
st.title("📈 " + {"trend_allocation": "Trend Allocation Bot", "hold_by_default": "Hold-by-Default Bot"}
         .get(config.STRATEGY, "EMA Crossover Bot"))
st.caption(f"{SETUP_TEXT} · [Source on GitHub]({REPO_URL})")
st.warning("**Paper trading — not financial advice.** This dashboard shows a simulated Alpaca "
           "paper account for an educational project. It is read-only and cannot place trades.",
           icon="⚠️")

keys = alpaca_keys()
client = trading_client()
clock, positions, acct = None, pd.DataFrame(), None
if keys is None:
    st.info("Alpaca secrets are not configured, so account data is hidden. "
            "Add `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in the app's Streamlit secrets.")
elif client is None:
    st.warning("The Alpaca secrets still contain placeholder text (e.g. `your_paper_api_key`). "
               "Replace both values with your real Alpaca **paper** API key and secret, "
               "keeping the quotes, then save.")
else:
    try:
        clock, positions, acct = load_clock(), load_positions(), load_account()
    except Exception as exc:
        unauthorized = any(word in str(exc).lower()
                           for word in ("401", "403", "unauthorized", "forbidden", "authorization"))
        st.error("Alpaca rejected the API keys. Check that both secrets are your **paper** trading "
                 "key and secret, with no extra spaces." if unauthorized else f"Could not reach Alpaca: {exc}")
        client = None

# ?tab=signals|runs|research opens that tab directly (handy for sharing links)
TABS = {"account": "Account", "signals": "Today's signals", "runs": "Run history", "research": "Research"}
tab_account, tab_signals, tab_runs, tab_research = st.tabs(
    list(TABS.values()),
    default=TABS.get({"backtest": "research"}.get(st.query_params.get("tab", ""), st.query_params.get("tab", "")),
                     "Account"))

# ---- Account
with tab_account:
    if client is None:
        st.write("Account data unavailable.")
    else:
        day_change = acct["equity"] - acct["last_equity"]
        invested = positions["Market value"].sum() if not positions.empty else 0.0
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Account value", money(acct["equity"]),
                  f"{day_change:+,.2f} today" if abs(day_change) >= 0.01 else None)
        c2.metric("Cash", money(acct["cash"]))
        c3.metric("Open positions", len(positions))
        c3.caption(f"{invested / acct['equity']:.0%} of equity in positions"
                   + (f" (incl. {config.CASH_SYMBOL} as cash)" if IS_ALLOCATION else ""))
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
                        y=alt.Y("Equity:Q", title=None, scale=alt.Scale(zero=False), axis=alt.Axis(format="$,.0f")),
                        tooltip=[alt.Tooltip("Date:T", format="%Y-%m-%d"), alt.Tooltip("Equity:Q", format="$,.2f")]
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
    st.caption(f"Bot mode: **{'Dry run (no orders)' if config.DRY_RUN else 'Live orders on the paper account'}** · "
               "Account data refreshes every 5 minutes.")


# ---- Today's signals
def render_allocation_signals():
    market_open = bool(clock and clock["is_open"])
    try:
        with st.spinner("Loading market data..."):
            sig = load_allocation_signals(market_open)
    except Exception as exc:
        st.warning(f"Market data unavailable: {exc}")
        return
    equity = acct["equity"] if acct else None
    held = dict(zip(positions["Symbol"], positions["Market value"], strict=True)) if not positions.empty else {}
    avg_label = (f"{config.ALLOCATION_MA_MONTHS}-month avg" if config.STRATEGY == "trend_allocation"
                 else f"{config.HOLD_SMA_DAYS}-day avg")
    rows = []
    for sym, (close, avg, above) in sig["signals"].items():
        rows.append({"Asset": sym, "Close": close, avg_label: avg, "Trend": "✅ above" if above else "❌ below",
                     "Target %": sig["weights"].get(sym, 0.0) * 100,
                     "Current %": held.get(sym, 0.0) / equity * 100 if equity else None})
    rows.append({"Asset": f"{config.CASH_SYMBOL} (cash)", "Close": sig["latest"].get(config.CASH_SYMBOL),
                 avg_label: None, "Trend": "–", "Target %": sig["weights"][config.CASH_SYMBOL] * 100,
                 "Current %": (held.get(config.CASH_SYMBOL, 0.0) + (acct["cash"] if acct else 0)) / equity * 100
                 if equity else None})
    risk_target = sum(w for s, w in sig["weights"].items() if s != config.CASH_SYMBOL)
    m1, m2, m3 = st.columns(3)
    m1.metric("Assets in uptrend", f"{sum(1 for _, _, a in sig['signals'].values() if a)} of {len(sig['signals'])}")
    m2.metric("Target invested", f"{risk_target:.0%}", f"{1 - risk_target:.0%} in {config.CASH_SYMBOL}",
              delta_color="off", delta_arrow="off")
    if client is not None:
        today = pd.Timestamp.now(tz="America/New_York").date()
        day = load_trading_day(today)
        upcoming = [d for d in TRANCHE_DAYS if d > day]
        m3.metric("Trading day of month", day,
                  f"next tranche: day {upcoming[0]}" if upcoming else "next tranche: day 1 next month",
                  delta_color="off", delta_arrow="off")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "Close": st.column_config.NumberColumn(format="$%.2f"),
        avg_label: st.column_config.NumberColumn(format="$%.2f"),
        "Target %": PCT, "Current %": PCT})
    st.caption(f"Signals from the close of {sig['signal_date']}. Each of the {config.TRANCHES} parts of the account "
               f"rebalances to these targets on its own trading day ({', '.join(map(str, TRANCHE_DAYS))}), so "
               "current weights move towards the targets over the month. Computed with the bot's own allocation code.")


def render_crossover_signals():
    market_open = bool(clock and clock["is_open"])
    rows = []
    for symbol in config.SYMBOLS:
        pos = positions[positions["Symbol"] == symbol] if not positions.empty else pd.DataFrame()
        qty = float(pos["Qty"].iloc[0]) if not pos.empty else 0.0
        entry = float(pos["Avg entry"].iloc[0]) if not pos.empty else None
        try:
            s = load_crossover_signal(symbol, market_open, qty, entry)
        except Exception as exc:
            st.warning(f"{symbol}: market data unavailable ({exc})")
            continue
        rows.append({"Symbol": symbol, "Signal": s["signal"], "Price": s["price"], "Held": qty,
                     "Days since up-cross": str(s["cross_age"]) if s["cross_age"] is not None
                     and s["last"]["ma_fast"] > s["last"]["ma_slow"] else "–",
                     "Reason": s["reason"]})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"Price": st.column_config.NumberColumn(format="$%.2f")})


with tab_signals:
    if IS_ALLOCATION:
        render_allocation_signals()
    else:
        render_crossover_signals()

# ---- Run history
with tab_runs:
    alloc_runs = load_csv(ALLOC_CSV)
    if IS_ALLOCATION:
        if alloc_runs.empty:
            st.info("No allocation runs in the repo yet (logs/allocation_runs.csv). "
                    "They appear here after the bot runs and the log is pushed to GitHub.")
        else:
            last_time = alloc_runs["run_time"].iloc[0]
            latest = alloc_runs[alloc_runs["run_time"] == last_time]
            st.caption(f"From `logs/allocation_runs.csv` in the repo; last run {last_time:%Y-%m-%d %H:%M} ET. "
                       "Updates when new runs are pushed to GitHub.")
            st.markdown(f"**Last run:** {latest['reason'].iloc[0]}")
            cols = ["symbol", "above", "target_weight", "current_qty", "target_qty", "order_qty", "action"]
            st.dataframe(latest[cols]
                         .assign(target_weight=lambda d: d["target_weight"] * 100),
                         hide_index=True, width="stretch", column_config={"target_weight": PCT})
            orders = alloc_runs[alloc_runs["action"].astype(str).str.contains("submitted", na=False)]
            st.subheader("Orders submitted")
            if orders.empty:
                st.write("No orders submitted yet.")
            else:
                st.dataframe(orders[["run_time", "symbol", "order_qty", "price", "action"]], hide_index=True,
                             width="stretch", column_config={
                                 "run_time": st.column_config.DatetimeColumn("Run (ET)", format="YYYY-MM-DD HH:mm")})
            with st.expander(f"All allocation runs ({alloc_runs['run_time'].nunique()})"):
                st.dataframe(alloc_runs, hide_index=True, width="stretch", column_config={
                    "run_time": st.column_config.DatetimeColumn("Run (ET)", format="YYYY-MM-DD HH:mm")})
    runs = load_csv(RUNS_CSV, bot.CSV_RENAMES)
    if not runs.empty:
        with st.expander("Archived: setup J (EMA crossover) runs" if IS_ALLOCATION else "Setup J runs",
                         expanded=not IS_ALLOCATION):
            cols = [c for c in ["run_time", "symbol", "price", "crossover", "cross_age", "signal", "qty",
                                "action", "reason"] if c in runs.columns]
            st.dataframe(runs[cols], hide_index=True, width="stretch", column_config={
                "run_time": st.column_config.DatetimeColumn("Run (ET)", format="YYYY-MM-DD HH:mm")})

# ---- Research
MAIN_STRATEGIES = ["Trend allocation (4 parts)", "Setup J (archived)", "SPY buy & hold", "60/40 SPY/IEF"]


def metrics_frame(df: pd.DataFrame, name_col: str) -> pd.DataFrame:
    """Research metrics (computed by research.py / metrics.py) formatted for display."""
    return pd.DataFrame({
        "Strategy": df[name_col], "CAGR %": df["cagr"] * 100, "Volatility %": df["vol"] * 100,
        "Sharpe": df["sharpe"].round(2), "Sortino": df["sortino"].round(2), "Max DD %": df["max_dd"] * 100,
        "Calmar": df["calmar"].round(2), "Worst year %": df["worst_year"] * 100,
        "Turnover/yr %": df["turnover_per_year"] * 100})


SERIES_COLORS = {"Trend allocation (4 parts)": "#2a78d6", "Setup J (archived)": "#eb6834",
                 "SPY buy & hold": "#3d3c39", "60/40 SPY/IEF": "#8a8986"}   # same as research_charts.py
EXTRA_COLORS = ["#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


def color_scale(names: list[str]) -> alt.Scale:
    extra = iter(EXTRA_COLORS * 3)
    return alt.Scale(domain=names, range=[SERIES_COLORS.get(n) or next(extra) for n in names])


def line_chart(df: pd.DataFrame, y_title: str, log: bool = False, fmt: str = ",.0f") -> alt.Chart:
    long = df.reset_index(names="Date").melt("Date", var_name="Strategy", value_name="Value").dropna()
    scale = alt.Scale(type="log") if log else alt.Scale(zero=False)
    return alt.Chart(long).mark_line(strokeWidth=1.8).encode(
        x=alt.X("Date:T", title=None),
        y=alt.Y("Value:Q", title=y_title, scale=scale, axis=alt.Axis(format=fmt)),
        color=alt.Color("Strategy:N", scale=color_scale(list(df.columns)),
                        legend=alt.Legend(orient="bottom", columns=2)),
        tooltip=[alt.Tooltip("Date:T", format="%Y-%m-%d"), "Strategy:N", alt.Tooltip("Value:Q", format=fmt)],
    ).properties(height=320)


def dated(df: pd.DataFrame) -> pd.DataFrame:
    """CSV with the date in the first column -> DatetimeIndex."""
    return df.set_index(pd.to_datetime(df.iloc[:, 0])).iloc[:, 1:]


with tab_research:
    summary = load_csv(os.path.join(RESEARCH_DIR, "summary.csv"))
    if summary.empty or "sortino" not in summary:
        st.info("No research results in docs/research/. Run `python research.py` and commit the output.")
    else:
        manifest = {}
        manifest_path = os.path.join(RESEARCH_DIR, "data_manifest.json")
        if os.path.exists(manifest_path):
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        st.caption(
            f"Jan 2007 – {manifest.get('end_date', 'Oct 2026')}, dividend-adjusted, 0.05% cost per side on every "
            "trade (including BIL), no look-ahead. Input data SHA-256 "
            f"`{manifest.get('combined_sha256', '?')[:16]}…` (docs/research/data_manifest.json). "
            "**Approach 1 was chosen by comparing five approaches on this same history, so these results are "
            "in-sample;** the only genuine out-of-sample test is the paper trading that began on 2026-10-05.")
        cfg = {c: PCT for c in ["CAGR %", "Volatility %", "Max DD %", "Worst year %", "Turnover/yr %"]}

        st.subheader("Chosen strategy vs. benchmarks")
        period = st.radio("Period", list(summary["period"].unique()), horizontal=True, label_visibility="collapsed")
        rows = summary[summary["period"] == period]
        show_all = st.toggle("Show all five approaches (in-sample model selection)")
        rows = rows if show_all else rows[rows["strategy"].isin(MAIN_STRATEGIES)]
        st.dataframe(metrics_frame(rows, "strategy"), hide_index=True, width="stretch", column_config=cfg)

        curves = load_csv(os.path.join(RESEARCH_DIR, "equity_weekly.csv"))
        if not curves.empty:
            curves = dated(curves)
            default = [c for c in MAIN_STRATEGIES if c in curves.columns]
            picked = st.multiselect("Strategies", list(curves.columns), default=default)
            if picked:
                st.altair_chart(line_chart(curves[picked], "Value of $100k (log scale)", log=True, fmt="$,.0f"),
                                width="stretch")
                dd = pd.DataFrame({c: metrics.drawdown_series(curves[c]) * 100 for c in picked})
                st.altair_chart(line_chart(dd, "Drawdown % (weekly closes)", fmt=".0f"), width="stretch")
                st.caption("Charts use weekly closes; the tables use daily data, so daily drawdowns can be deeper.")

        rolling_df = load_csv(os.path.join(RESEARCH_DIR, "rolling_weekly.csv"))
        if not rolling_df.empty:
            rolling_df = dated(rolling_df)
            labels = {"vol 63d": "63-day volatility", "sharpe 252d": "252-day Sharpe"}
            which = st.radio("Rolling statistic", list(labels), horizontal=True, format_func=labels.get)
            sub = rolling_df[[c for c in rolling_df.columns if c.endswith(which)]]
            sub.columns = [c.split(" | ")[0] for c in sub.columns]
            is_vol = which == "vol 63d"
            st.altair_chart(line_chart(sub * (100 if is_vol else 1), "Volatility %" if is_vol else "Sharpe",
                                       fmt=".1f"), width="stretch")

        robust = load_csv(os.path.join(RESEARCH_DIR, "robustness.csv"))
        if not robust.empty:
            st.subheader("Robustness of the chosen strategy")
            st.caption("Each variant changes one assumption of the live configuration (marked ★). The goal is to "
                       "see how stable the results are, not to pick the best-looking variant.")
            group = st.selectbox("Assumption", list(dict.fromkeys(robust["group"])))
            g = robust[robust["group"] == group].copy()
            g["Variant"] = g["variant"] + g["standard"].map({True: " ★", False: ""})
            st.dataframe(metrics_frame(g, "Variant"), hide_index=True, width="stretch", column_config=cfg)
            st.altair_chart(alt.Chart(g).mark_circle(size=90).encode(
                x=alt.X("max_dd:Q", title="Max drawdown", axis=alt.Axis(format="%")),
                y=alt.Y("sharpe:Q", title="Sharpe", scale=alt.Scale(zero=False)),
                color=alt.Color("standard:N", title="Live configuration"),
                tooltip=["Variant:N", alt.Tooltip("cagr:Q", format=".1%"), alt.Tooltip("sharpe:Q", format=".2f"),
                         alt.Tooltip("max_dd:Q", format=".1%")]).properties(height=260), width="stretch")
            image(os.path.join(RESEARCH_DIR, "robustness.png"))

        regimes_df = load_csv(os.path.join(RESEARCH_DIR, "regimes.csv"))
        if not regimes_df.empty:
            st.subheader("Market regimes")
            st.caption("Bull/bear: SPY above/below its 200-day average at the previous close. High/low volatility: "
                       "SPY's 63-day realized volatility at the previous close above/below its expanding median.")
            kind = st.radio("Regime type", list(dict.fromkeys(regimes_df["regime_type"])), horizontal=True)
            rg = regimes_df[regimes_df["regime_type"] == kind]
            st.altair_chart(alt.Chart(rg).mark_bar().encode(
                x=alt.X("strategy:N", title=None, sort=MAIN_STRATEGIES, axis=alt.Axis(labelAngle=0, labelLimit=140)),
                y=alt.Y("ann_compounded:Q", title="Annualized compounded return", axis=alt.Axis(format="%")),
                color=alt.Color("strategy:N", legend=None, scale=color_scale(MAIN_STRATEGIES)),
                column=alt.Column("regime:N", title=None),
                tooltip=["strategy:N", alt.Tooltip("ann_compounded:Q", format=".1%"),
                         alt.Tooltip("sharpe:Q", format=".2f"), alt.Tooltip("share_of_days:Q", format=".0%")],
            ).properties(height=240, width=230))

        stress_df = load_csv(os.path.join(RESEARCH_DIR, "stress.csv"))
        if not stress_df.empty:
            with st.expander("Stress years (2008, 2020, 2022)"):
                st.dataframe(stress_df.assign(**{"return": stress_df["return"] * 100,
                                                 "max_dd": stress_df["max_dd"] * 100}),
                             hide_index=True, width="stretch",
                             column_config={"return": PCT,
                                            "max_dd": st.column_config.NumberColumn("max DD", format="%.1f%%")})

    bt = load_csv(os.path.join(BACKTEST_DIR, "summary.csv"))
    if not bt.empty and "study" in bt:
        with st.expander("Archived: setup J backtest study (2021–2026)"):
            setups = bt[bt["study"] == "setups"]
            st.dataframe(pd.DataFrame({
                "Setup": setups["setup"], "Trades/mo": setups["trades_per_month"].round(1),
                "CAGR %": setups["cagr"] * 100, "Max DD %": setups["max_dd"] * 100,
                "Basket CAGR %": setups["basket_cagr"] * 100}), hide_index=True, width="stretch",
                column_config={c: PCT for c in ["CAGR %", "Max DD %", "Basket CAGR %"]})
            image(os.path.join(BACKTEST_DIR, "charts", "j_ema_10_30_crossover_only_12_etfs.png"))

st.divider()
st.caption(f"Educational project. Paper trading only. Not financial advice. [GitHub]({REPO_URL}) · "
           f"rendered {dt.datetime.now():%Y-%m-%d %H:%M}")
