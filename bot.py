"""Trading bot for Alpaca: monthly trend allocation (default) or the archived EMA crossover.

config.STRATEGY picks the mode:
  "trend_allocation" / "hold_by_default"  monthly allocation with split rebalancing (run_allocation)
  "ema_crossover"                         archived setup J, daily per-symbol signals (run_symbol)

Usage:
    python bot.py              # dry run or live according to config.DRY_RUN
    python bot.py --dry-run    # force dry run (never places orders)
    python bot.py --live       # force order placement (paper account by default)
    python bot.py --scheduled  # for Task Scheduler: runs only once per trading day,
                               # at/after config.SCHEDULED_RUN_TIME_ET while the market is open
    python bot.py --backtest   # backtest study (see backtest.py)
"""
import argparse
import csv
import json
import logging
import math
import os
import sys

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

import allocation
import config
from indicators import add_indicators

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, config.LOG_DIR)
STATE_PATH = os.path.join(HERE, config.STATE_FILE)
CSV_PATH = os.path.join(LOG_DIR, "runs.csv")
ALLOC_CSV_PATH = os.path.join(LOG_DIR, "allocation_runs.csv")
LAST_RUN_PATH = os.path.join(LOG_DIR, "last_scheduled_run.txt")
ALLOCATION_STRATEGIES = ("trend_allocation", "hold_by_default")

ALLOC_FIELDS = [
    "run_time", "signal_date", "strategy", "trading_day", "tranches_due", "symbol", "close", "average",
    "above", "target_weight", "price", "current_qty", "target_qty", "order_qty", "action", "reason",
]

CSV_FIELDS = [
    "run_time", "bar_date", "symbol", "price", "close", "ma_fast", "ma_slow", "sma_trend",
    "rsi", "macd", "macd_signal", "volume", "vol_avg", "adx", "atr", "crossover", "cross_age",
    "trend", "rsi_filter", "macd_filter", "volume_filter", "adx_filter",
    "position_qty", "entry_price", "stop_price", "qty", "market_open", "dry_run",
    "signal", "action", "reason",
]
# Older runs.csv files used these names (before EMA support)
CSV_RENAMES = {"sma_fast": "ma_fast", "sma_slow": "ma_slow"}

log = logging.getLogger("bot")


# --------------------------------------------------------------------------- setup
def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    handlers = [logging.FileHandler(os.path.join(LOG_DIR, "bot.log"), encoding="utf-8")]
    if sys.stdout is not None:  # pythonw.exe (used by Task Scheduler) has no console
        handlers.append(logging.StreamHandler(sys.stdout))
    for handler in handlers:
        handler.setFormatter(fmt)
        log.addHandler(handler)


def alpaca_client():
    from alpaca.trading.client import TradingClient

    load_dotenv(os.path.join(HERE, ".env"))
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret or key.startswith("your_"):
        raise RuntimeError("Missing Alpaca credentials: fill in ALPACA_API_KEY / ALPACA_SECRET_KEY in .env")
    return TradingClient(key, secret, paper=config.PAPER_TRADING)


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# --------------------------------------------------------------------------- data
def fetch_bars(symbol):
    df = yf.Ticker(symbol).history(period="2y", interval="1d", auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"yfinance returned no data for {symbol}")
    return df[["Open", "High", "Low", "Close", "Volume"]].tail(config.LOOKBACK_BARS)


def signal_bars(df, market_open):
    """Drop today's still-forming bar when the market is open (if configured)."""
    today = pd.Timestamp.now(tz="America/New_York").date()
    if config.USE_COMPLETED_BARS_ONLY and market_open and df.index[-1].date() == today:
        return df.iloc[:-1]
    return df


# --------------------------------------------------------------------------- strategy
def ma_label(period):
    return f"{config.MA_TYPE.upper()}{period}"


def crossover(prev, last):
    if prev.ma_fast <= prev.ma_slow and last.ma_fast > last.ma_slow:
        return "UP"
    if prev.ma_fast >= prev.ma_slow and last.ma_fast < last.ma_slow:
        return "DOWN"
    return "NONE"


def cross_date(last):
    """Date (YYYY-MM-DD) of the most recent up-crossover, or None."""
    return None if pd.isna(last.last_cross_up) else pd.Timestamp(last.last_cross_up).strftime("%Y-%m-%d")


def entry_window_open(last):
    """True while the fast MA is above the slow MA and the up-crossover is recent enough."""
    return (last.ma_fast > last.ma_slow and not pd.isna(last.cross_age)
            and last.cross_age <= config.ENTRY_WINDOW_DAYS)


def check_filters(last):
    """Return {name: 'PASS' | 'FAIL' | 'OFF'} for every confluence filter."""
    checks = {
        "trend": (config.USE_TREND_FILTER, last.Close > last.sma_trend),
        "rsi_filter": (config.USE_RSI_FILTER, config.RSI_BUY_MIN <= last.rsi <= config.RSI_BUY_MAX),
        "macd_filter": (config.USE_MACD_FILTER, last.macd > last.macd_signal),
        "volume_filter": (config.USE_VOLUME_FILTER, last.Volume > last.vol_avg),
        "adx_filter": (config.USE_ADX_FILTER, last.adx > config.ADX_MIN),
    }
    return {name: ("PASS" if ok else "FAIL") if on else "OFF" for name, (on, ok) in checks.items()}


def exit_rules_hit(cross, last, price, stop):
    """Return [(rule, description)] for every enabled exit rule that triggered."""
    hits = []
    if config.USE_CROSS_EXIT and cross == "DOWN":
        hits.append(("MA cross", f"{ma_label(config.FAST_MA)} crossed below {ma_label(config.SLOW_MA)}"))
    if config.USE_RSI_EXIT and last.rsi > config.RSI_EXIT_ABOVE:
        hits.append(("RSI exit", f"RSI {last.rsi:.1f} > {config.RSI_EXIT_ABOVE}"))
    if config.USE_ATR_STOP and price <= stop:
        hits.append(("ATR stop", f"price {price:.2f} <= ATR stop {stop:.2f}"))
    return hits


def evaluate(prev, last, price, position_qty, stop=None, last_entry_cross=None):
    """Core strategy decision, shared by live runs and the backtest.

    prev/last are the two most recent completed bars (with indicators), price is the
    current price, stop the position's stop price and last_entry_cross the date of the
    crossover the previous entry used (for ONE_ENTRY_PER_CROSSOVER). Returns a dict with
    signal ('BUY' | 'SELL' | 'NONE'), crossover, cross_age, cross_date, filters,
    exit_rules and reason. Position sizing and order gating (market hours, pending
    orders) are left to the caller.
    """
    cross = crossover(prev, last)
    filters = check_filters(last)
    age = None if pd.isna(last.cross_age) else int(last.cross_age)
    result = {"signal": "NONE", "crossover": cross, "cross_age": age, "cross_date": cross_date(last),
              "filters": filters, "exit_rules": [], "reason": ""}

    if position_qty < 0:
        result["reason"] = f"short position of {position_qty} found; bot does not manage shorts"
    elif position_qty > 0:
        hits = exit_rules_hit(cross, last, price, stop)
        if hits:
            result.update(signal="SELL", exit_rules=[rule for rule, _ in hits],
                          reason="; ".join(text for _, text in hits))
        else:
            result["reason"] = f"holding {position_qty:g} shares; no exit rule triggered"
            if cross == "UP":
                result["reason"] += " (up-crossover ignored: already in position)"
    else:
        failed = [name for name, outcome in filters.items() if outcome == "FAIL"]
        when = "today" if age == 0 else f"{age} day(s) ago"
        if not entry_window_open(last):
            result["reason"] = (f"no up-crossover (crossover={cross})" if config.ENTRY_WINDOW_DAYS == 0
                                else f"no up-crossover in the last {config.ENTRY_WINDOW_DAYS} days (crossover={cross})")
            if cross == "DOWN":
                result["reason"] += "; sell signal ignored: no shares owned, no short selling"
        elif config.ONE_ENTRY_PER_CROSSOVER and last_entry_cross == result["cross_date"]:
            result["reason"] = f"already traded the {result['cross_date']} crossover; waiting for the next one"
        elif failed:
            result["reason"] = f"up-crossover {when} but filters failed: " + ", ".join(failed)
        else:
            passed = ("all enabled filters passed" if "PASS" in filters.values()
                      else "no entry filters enabled")
            result.update(signal="BUY", reason=f"up-crossover {when}; {passed}")
    return result


INDICATOR_COLUMNS = ["ma_fast", "ma_slow", "sma_trend", "rsi", "macd", "macd_signal", "vol_avg", "adx", "atr"]


def stop_price_for(entry_price, symbol, state, current_atr):
    """Stop = entry - multiplier * ATR at entry (falls back to current ATR if unknown)."""
    atr_at_entry = state.get(symbol, {}).get("atr_at_entry")
    if atr_at_entry is None:
        log.warning("No saved ATR at entry for %s; using current ATR for the stop", symbol)
        atr_at_entry = current_atr
    return entry_price - config.ATR_STOP_MULTIPLIER * atr_at_entry


def position_size(equity, cash, price, current_atr):
    """Whole shares to buy, shared by live runs and the backtest.

    equal_weight: equity / len(SYMBOLS) per symbol. With at most one position per
    symbol, all open positions together cost at most the account equity.
    Every mode is capped by the cash available, so the bot never uses margin.
    """
    if config.POSITION_SIZING == "equal_weight":
        qty = math.floor(equity / len(config.SYMBOLS) / price)
    elif config.POSITION_SIZING == "risk":
        qty = math.floor(equity * config.RISK_PER_TRADE_PCT / 100 / (config.ATR_STOP_MULTIPLIER * current_atr))
    else:
        qty = config.TRADE_QTY
    return max(0, min(qty, math.floor(cash / price)))


def buy_quantity(account, price, current_atr):
    # non-marginable buying power is reduced by pending buy orders, so two symbols
    # bought on the same morning can't both spend the same cash
    cash = min(float(account.cash), float(account.non_marginable_buying_power))
    return position_size(float(account.equity), cash, price, current_atr)


# --------------------------------------------------------------------------- orders
def submit_market_order(client, symbol, qty, side):
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    order = client.submit_order(MarketOrderRequest(
        symbol=symbol, qty=qty,
        side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    ))
    return order.id


def open_orders_for(client, symbol):
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    return client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))


# --------------------------------------------------------------------------- logging
def write_csv(row):
    new_file = not os.path.exists(CSV_PATH)
    if not new_file:
        with open(CSV_PATH, encoding="utf-8") as f:
            header = f.readline().strip().split(",")
        if header != CSV_FIELDS:  # columns changed: rewrite old rows under the new header
            old = pd.read_csv(CSV_PATH).rename(columns=CSV_RENAMES)
            old.reindex(columns=CSV_FIELDS).to_csv(CSV_PATH, index=False)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def fmt(x, nd=2):
    return "" if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), nd)


# --------------------------------------------------------------------------- main
def run_symbol(client, symbol, clock, dry_run):
    account = client.get_account()
    positions = {p.symbol: p for p in client.get_all_positions()}
    position = positions.get(symbol)
    position_qty = float(position.qty) if position else 0.0
    entry_price = float(position.avg_entry_price) if position else None
    pending = open_orders_for(client, symbol)

    raw = fetch_bars(symbol)
    df = add_indicators(signal_bars(raw, clock.is_open), config)
    price = float(raw["Close"].iloc[-1])  # latest price (may be today's live bar)
    prev, last = df.iloc[-2], df.iloc[-1]

    row = {
        "run_time": pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d %H:%M:%S"),
        "bar_date": str(last.name.date()), "symbol": symbol, "price": fmt(price),
        "close": fmt(last.Close), "ma_fast": fmt(last.ma_fast), "ma_slow": fmt(last.ma_slow),
        "sma_trend": fmt(last.sma_trend), "rsi": fmt(last.rsi), "macd": fmt(last.macd, 4),
        "macd_signal": fmt(last.macd_signal, 4), "volume": int(last.Volume),
        "vol_avg": fmt(last.vol_avg, 0), "adx": fmt(last.adx), "atr": fmt(last.atr),
        "position_qty": position_qty, "entry_price": fmt(entry_price),
        "market_open": clock.is_open, "dry_run": dry_run,
    }

    if df[INDICATOR_COLUMNS].iloc[-2:].isna().any().any():
        row.update(signal="NONE", action="NO TRADE", reason="not enough history for indicators to warm up")
        return finish(row)

    state = load_state()
    stop = None
    if position_qty > 0:
        stop = stop_price_for(entry_price, symbol, state, float(last.atr))
        row["stop_price"] = fmt(stop)

    decision = evaluate(prev, last, price, position_qty, stop, state.get(symbol, {}).get("entry_cross"))
    signal, reason = decision["signal"], decision["reason"]
    row["crossover"] = decision["crossover"]
    row["cross_age"] = "" if decision["cross_age"] is None else decision["cross_age"]
    row.update(decision["filters"])

    qty = 0
    if signal == "SELL":
        qty = position_qty
    elif signal == "BUY":
        qty = buy_quantity(account, price, float(last.atr))
        if qty < 1:
            signal = "NONE"
            reason = "up-crossover and filters passed, but position size is 0 (cash or risk limit)"

    row.update(signal=signal, reason=reason, qty=qty if signal != "NONE" else "")

    if signal == "NONE":
        row["action"] = "NO TRADE"
        return finish(row)

    blockers = []
    if pending:
        blockers.append(f"{len(pending)} open order(s) already pending for {symbol}")
    if not clock.is_open:
        blockers.append(f"market closed (next open {clock.next_open})")
    if blockers:
        row["action"] = f"NO TRADE ({signal} {qty:g} blocked: {'; '.join(blockers)})"
    elif dry_run:
        row["action"] = f"DRY RUN: would {signal} {qty:g} {symbol} at market"
    else:
        order_id = submit_market_order(client, symbol, qty, signal)
        row["action"] = f"{signal} {qty:g} {symbol} submitted (order {order_id})"
        if signal == "BUY":
            state[symbol] = {"atr_at_entry": float(last.atr), "signal_price": price,
                             "date": row["run_time"], "entry_cross": decision["cross_date"]}
        else:  # keep entry_cross so the same crossover isn't traded again
            state[symbol] = {"entry_cross": state.get(symbol, {}).get("entry_cross")}
        save_state(state)
    return finish(row)


def finish(row):
    filters = " ".join(f"{k}={row.get(k, '')}" for k in
                       ("trend", "rsi_filter", "macd_filter", "volume_filter", "adx_filter"))
    log.info(
        "%s %s price=%s | %s=%s %s=%s SMA%d=%s RSI=%s MACD=%s/%s Vol=%s/%s ADX=%s ATR=%s | "
        "cross=%s age=%s %s | pos=%s stop=%s | %s -> %s | %s",
        row["bar_date"], row["symbol"], row["price"], ma_label(config.FAST_MA), row["ma_fast"],
        ma_label(config.SLOW_MA), row["ma_slow"], config.TREND_MA, row["sma_trend"], row["rsi"],
        row["macd"], row["macd_signal"], row["volume"], row["vol_avg"], row["adx"], row["atr"],
        row.get("crossover", ""), row.get("cross_age", ""), filters, row["position_qty"], row.get("stop_price", ""),
        row["signal"], row["action"], row["reason"],
    )
    write_csv(row)
    return row


# --------------------------------------------------------------------------- monthly allocation
def allocation_universe():
    """(assets, signal function) for the active allocation strategy (rules in allocation.py)."""
    if config.STRATEGY == "trend_allocation":
        assets = list(config.ALLOCATION_ASSETS)
        return assets, lambda px, s, month_end: allocation.trend_signals(
            px, s, assets, config.ALLOCATION_MA_MONTHS, month_end)
    if config.STRATEGY == "hold_by_default":
        assets = list(config.HOLD_ASSETS)
        return assets, lambda px, s, month_end: allocation.hold_signals(px, s, assets, config.HOLD_SMA_DAYS)
    raise ValueError(f"unknown allocation strategy {config.STRATEGY!r}")


def fetch_closes(symbols, market_open):
    """Completed daily closes (one column per symbol) and the latest price of each symbol."""
    closes, latest = {}, {}
    for symbol in symbols:
        raw = yf.Ticker(symbol).history(period="2y", interval="1d", auto_adjust=True)
        if raw.empty:
            raise RuntimeError(f"yfinance returned no data for {symbol}")
        latest[symbol] = float(raw["Close"].iloc[-1])  # may be today's live bar
        closes[symbol] = signal_bars(raw, market_open)["Close"]
    df = pd.DataFrame(closes)
    df.index = df.index.tz_localize(None).normalize()
    return df.ffill(limit=5), latest


def trading_day_of_month(client, day):
    """How many trading days of `day`'s month have happened up to and including `day`."""
    from alpaca.trading.requests import GetCalendarRequest

    calendar = client.get_calendar(GetCalendarRequest(start=day.replace(day=1), end=day))
    return sum(1 for c in calendar if c.date <= day)


def new_ledger(positions, cash, universe):
    """Split the current account into TRANCHES equal virtual parts."""
    n = config.TRANCHES
    held = {s: q for s, q in positions.items() if s in universe}
    return {"strategy": config.STRATEGY, "tranches": [
        {"cash": cash / n, "shares": {s: q / n for s, q in held.items()}, "last_rebalance": None}
        for _ in range(n)]}


def reconcile_ledger(ledger, positions, cash, universe):
    """Scale the virtual tranches to the real account (fills, rounding, manual trades)."""
    notes, tranches = [], ledger["tranches"]
    for s in universe:
        booked = sum(t["shares"].get(s, 0.0) for t in tranches)
        actual = positions.get(s, 0.0)
        if abs(booked - actual) > 1e-3:          # ignore rounding to 4 decimals
            notes.append(f"{s} {booked:.4f} -> {actual:.4f}")
            for t in tranches:
                t["shares"][s] = (t["shares"].get(s, 0.0) * actual / booked if booked > 0
                                  else actual / len(tranches))
    booked_cash = sum(t["cash"] for t in tranches)
    if abs(booked_cash - cash) > 0.01:
        for t in tranches:
            t["cash"] += (cash - booked_cash) / len(tranches)
    return notes


def round_qty(qty, fractionable):
    """Round toward zero: 4 decimals for fractional shares, whole shares otherwise."""
    step = 1e-4 if fractionable else 1.0
    return math.copysign(math.floor(abs(qty) / step + 1e-9) * step, qty)


def write_alloc_rows(rows):
    new_file = not os.path.exists(ALLOC_CSV_PATH)
    with open(ALLOC_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ALLOC_FIELDS)
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in ALLOC_FIELDS})


def run_allocation(client, clock, dry_run):
    """Monthly allocation with split rebalancing.

    The account is divided into TRANCHES virtual parts (kept in bot_state.json). Part k
    rebalances on trading day 1 + k * TRANCHE_SPACING_DAYS of each month to the current
    target weights; money not allocated is held in CASH_SYMBOL (BIL). A run on any other
    day only logs the signals. Missed rebalance days are caught up on the next run.
    """
    universe, signal_fn = allocation_universe()
    cash_symbol = config.CASH_SYMBOL
    tradable = universe + [cash_symbol]
    now = pd.Timestamp.now(tz="America/New_York")
    today, month = now.date(), now.strftime("%Y-%m")

    account = client.get_account()
    pos_objs = {p.symbol: p for p in client.get_all_positions()}
    positions = {s: float(p.qty) for s, p in pos_objs.items()}
    cash = float(account.cash)
    pending = [o for o in client.get_orders(filter=_open_orders_request()) if o.symbol in set(tradable) | set(positions)]

    closes, latest = fetch_closes(tradable, clock.is_open)
    px = allocation.Prices(closes[universe])
    s = closes.index[-1]                       # last completed trading day
    month_end = s.month != today.month         # first trading day of a new month -> month-end closes
    signals = signal_fn(px, s, month_end)
    weights = allocation.weights_from(signals)
    weights[cash_symbol] = max(0.0, 1.0 - sum(weights.values()))
    prices = {sym: latest[sym] for sym in tradable}
    prices.update({sym: float(p.current_price) for sym, p in pos_objs.items() if sym not in prices})

    state = load_state()
    ledger = state.get("allocation")
    notes = []
    if not ledger or ledger.get("strategy") != config.STRATEGY or len(ledger["tranches"]) != config.TRANCHES:
        ledger = new_ledger(positions, cash, tradable)
        notes.append("new ledger: all tranches rebalance now")
        initial = True
    else:
        notes += [f"reconciled {n}" for n in reconcile_ledger(ledger, positions, cash, tradable)]
        initial = False

    day = trading_day_of_month(client, today)
    spacing = config.TRANCHE_SPACING_DAYS
    due = [k for k, t in enumerate(ledger["tranches"])
           if initial or (t["last_rebalance"] != month and day >= allocation.tranche_day(k, spacing))]
    for k in due:
        t = ledger["tranches"][k]
        value = t["cash"] + sum(q * prices[sym] for sym, q in t["shares"].items())
        t["shares"] = {sym: w * value / prices[sym] for sym, w in weights.items() if w > 0}
        t["cash"] = 0.0
        if not initial or day >= allocation.tranche_day(k, spacing):
            t["last_rebalance"] = month   # tranches whose day is still ahead also rebalance on it

    # Orders: move the account from its real positions to the sum of all tranches.
    targets = {sym: sum(t["shares"].get(sym, 0.0) for t in ledger["tranches"]) for sym in tradable}
    for sym in positions:
        targets.setdefault(sym, 0.0)           # anything outside the strategy is sold
    fractionable = {sym: bool(getattr(client.get_asset(sym), "fractionable", False)) for sym in targets}
    orders = {}
    for sym, target in targets.items():
        qty = round_qty(target - positions.get(sym, 0.0), fractionable[sym])
        if target == 0 and positions.get(sym, 0.0) > 0:
            qty = -positions[sym]               # close whole positions, including fractions
        if qty and abs(qty) * prices[sym] >= config.MIN_ORDER_VALUE:
            orders[sym] = qty
    buy_value = sum(q * prices[sym] for sym, q in orders.items() if q > 0)
    sell_value = sum(-q * prices[sym] for sym, q in orders.items() if q < 0)
    available = min(cash, float(account.non_marginable_buying_power)) + sell_value * 0.995
    if buy_value > available:                  # never use margin: scale buys down to the cash available
        scale = max(0.0, available) / buy_value
        notes.append(f"buys scaled to {scale:.1%} of target to stay within cash")
        orders = {sym: (round_qty(q * scale, fractionable[sym]) if q > 0 else q) for sym, q in orders.items()}

    blockers = []
    if pending:
        blockers.append(f"{len(pending)} open order(s) pending")
    if not clock.is_open:
        blockers.append(f"market closed (next open {clock.next_open})")
    if not orders:
        action_all = "NO TRADE"
    elif dry_run:
        action_all = "DRY RUN"
    elif blockers:
        action_all = "BLOCKED: " + "; ".join(blockers)
    else:
        action_all = "SUBMIT"

    submitted = {}
    if action_all == "SUBMIT":
        for sym, qty in sorted(orders.items(), key=lambda kv: kv[1]):   # sells first
            submitted[sym] = submit_market_order(client, sym, abs(qty), "BUY" if qty > 0 else "SELL")
    if not dry_run and action_all in ("SUBMIT", "NO TRADE"):   # blocked or dry runs leave the ledger as it was
        state["allocation"] = ledger
        save_state(state)

    due_text = ("all (initial)" if initial else ",".join(str(k + 1) for k in due)) if due else "none"
    next_days = [allocation.tranche_day(k, spacing) for k in range(config.TRANCHES)]
    if dry_run and blockers:
        notes.append("a live run now would be blocked: " + "; ".join(blockers))
    reason = (f"trading day {day} of {now:%b}; tranches rebalance on days {next_days}; due: {due_text}"
              + (f"; {'; '.join(notes)}" if notes else ""))
    run_time = now.strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for sym in sorted(targets, key=lambda x: (x not in universe, x != cash_symbol, x)):
        close, avg, above = signals.get(sym, (closes[sym].iloc[-1] if sym in closes else None, None, None))
        qty = orders.get(sym, 0.0)
        if not qty:
            action = "NO TRADE"
        elif action_all == "SUBMIT":
            action = f"{'BUY' if qty > 0 else 'SELL'} {abs(qty):g} submitted (order {submitted[sym]})"
        elif action_all == "DRY RUN":
            action = f"DRY RUN: would {'BUY' if qty > 0 else 'SELL'} {abs(qty):g}"
        else:
            action = f"NO TRADE ({'BUY' if qty > 0 else 'SELL'} {abs(qty):g} {action_all.lower()})"
        rows.append({
            "run_time": run_time, "signal_date": str(s.date()), "strategy": config.STRATEGY,
            "trading_day": day, "tranches_due": due_text, "symbol": sym, "close": fmt(close),
            "average": fmt(avg), "above": "" if above is None else above,
            "target_weight": round(weights.get(sym, 0.0), 4), "price": fmt(prices.get(sym)),
            "current_qty": round(positions.get(sym, 0.0), 4), "target_qty": round(targets[sym], 4),
            "order_qty": round(qty, 4), "action": action, "reason": reason,
        })
        log.info("%s %s close=%s avg=%s above=%s weight=%.1f%% | hold=%s target=%s | %s",
                 rows[-1]["signal_date"], sym, rows[-1]["close"], rows[-1]["average"], rows[-1]["above"],
                 weights.get(sym, 0.0) * 100, rows[-1]["current_qty"], rows[-1]["target_qty"], action)
    log.info("ALLOCATION %s | %s | equity $%s, buys $%s, sells $%s | %s", config.STRATEGY, reason,
             f"{float(account.equity):,.0f}", f"{buy_value:,.0f}", f"{sell_value:,.0f}", action_all)
    write_alloc_rows(rows)
    return rows


def _open_orders_request():
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    return GetOrdersRequest(status=QueryOrderStatus.OPEN)


def run_units():
    """What a scheduled run completes per day: each symbol (crossover) or one allocation run."""
    return list(config.SYMBOLS) if config.STRATEGY == "ema_crossover" else ["ALLOCATION"]


# --------------------------------------------------------------------------- scheduling
def scheduler_log(message):
    """One line per scheduled wake-up (skips included) in logs/scheduler.log."""
    stamp = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d %H:%M:%S ET")
    with open(os.path.join(LOG_DIR, "scheduler.log"), "a", encoding="utf-8") as f:
        f.write(f"{stamp} {message}\n")


def today_et():
    return pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d")


def symbols_done_today():
    """Symbols the scheduled run already completed today (file: 'YYYY-MM-DD SPY,QQQ')."""
    try:
        with open(LAST_RUN_PATH, encoding="utf-8") as f:
            date, _, symbols = f.read().strip().partition(" ")
    except FileNotFoundError:
        return set()
    return set(filter(None, symbols.split(","))) if date == today_et() else set()


def mark_done_today(symbol):
    done = symbols_done_today() | {symbol}
    with open(LAST_RUN_PATH, "w", encoding="utf-8") as f:
        f.write(f"{today_et()} {','.join(sorted(done))}")


def scheduled_skip_reason(client):
    """Why a scheduled trigger should not run now, or None if it should.

    Task Scheduler fires every few minutes around the open, in the PC's local time;
    this check pins the real run to US Eastern time, so DST changes don't matter.
    """
    now = pd.Timestamp.now(tz="America/New_York")
    if set(run_units()) <= symbols_done_today():
        return "already ran today"
    if now.strftime("%H:%M") < config.SCHEDULED_RUN_TIME_ET:
        return f"too early ({now:%H:%M} ET < {config.SCHEDULED_RUN_TIME_ET} ET)"
    clock = client.get_clock()  # covers weekends, holidays and early closes
    if not clock.is_open:
        return f"market closed (next open {clock.next_open})"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="never place orders")
    mode.add_argument("--live", action="store_true", help="place orders (paper account by default)")
    parser.add_argument("--scheduled", action="store_true",
                        help="run at most once per trading day, after SCHEDULED_RUN_TIME_ET")
    parser.add_argument("--backtest", action="store_true",
                        help="run the backtest study instead of trading (same as python backtest.py)")
    args = parser.parse_args()
    if args.backtest:
        import backtest
        backtest.main([])
        return
    dry_run = True if args.dry_run else False if args.live else config.DRY_RUN

    setup_logging()
    try:
        client = alpaca_client()
        symbols = run_units()
        if args.scheduled:
            skip = scheduled_skip_reason(client)
            if skip:
                scheduler_log(f"skip: {skip}")
                return
            done = symbols_done_today()
            symbols = [s for s in symbols if s not in done]
            scheduler_log(f"running bot for {', '.join(symbols)}")
        clock = client.get_clock()
    except Exception:
        log.exception("Run failed")
        if args.scheduled:
            scheduler_log("run failed; will retry on the next trigger (see bot.log)")
        sys.exit(1)

    # Each symbol runs independently: one failing doesn't stop the others, and on the
    # next scheduled trigger only the failed symbols are retried.
    failed = []
    for symbol in symbols:
        try:
            if symbol == "ALLOCATION":
                run_allocation(client, clock, dry_run)
            else:
                run_symbol(client, symbol, clock, dry_run)
            if args.scheduled:
                mark_done_today(symbol)
        except Exception:
            log.exception("%s run failed", symbol)
            failed.append(symbol)
    if failed:
        if args.scheduled:
            scheduler_log(f"failed for {', '.join(failed)}; will retry on the next trigger (see bot.log)")
        sys.exit(1)


if __name__ == "__main__":
    main()
