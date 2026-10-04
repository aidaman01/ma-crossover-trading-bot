"""Moving-average crossover bot with confluence filters, trading on Alpaca.

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

import config
from indicators import add_indicators

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, config.LOG_DIR)
STATE_PATH = os.path.join(HERE, config.STATE_FILE)
CSV_PATH = os.path.join(LOG_DIR, "runs.csv")
LAST_RUN_PATH = os.path.join(LOG_DIR, "last_scheduled_run.txt")

CSV_FIELDS = [
    "run_time", "bar_date", "symbol", "price", "close", "sma_fast", "sma_slow", "sma_trend",
    "rsi", "macd", "macd_signal", "volume", "vol_avg", "adx", "atr", "crossover",
    "trend", "rsi_filter", "macd_filter", "volume_filter", "adx_filter",
    "position_qty", "entry_price", "stop_price", "market_open", "dry_run",
    "signal", "action", "reason",
]

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
def crossover(prev, last):
    if prev.sma_fast <= prev.sma_slow and last.sma_fast > last.sma_slow:
        return "UP"
    if prev.sma_fast >= prev.sma_slow and last.sma_fast < last.sma_slow:
        return "DOWN"
    return "NONE"


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
        hits.append(("MA cross", f"{config.FAST_MA}MA crossed below {config.SLOW_MA}MA"))
    if config.USE_RSI_EXIT and last.rsi > config.RSI_EXIT_ABOVE:
        hits.append(("RSI exit", f"RSI {last.rsi:.1f} > {config.RSI_EXIT_ABOVE}"))
    if config.USE_ATR_STOP and price <= stop:
        hits.append(("ATR stop", f"price {price:.2f} <= ATR stop {stop:.2f}"))
    return hits


def evaluate(prev, last, price, position_qty, stop=None):
    """Core strategy decision, shared by live runs and the backtest.

    prev/last are the two most recent completed bars (with indicators), price is the
    current price and stop the position's stop price. Returns a dict with signal
    ('BUY' | 'SELL' | 'NONE'), crossover, filters, exit_rules and reason. Position
    sizing and order gating (market hours, pending orders) are left to the caller.
    """
    cross = crossover(prev, last)
    filters = check_filters(last)
    result = {"signal": "NONE", "crossover": cross, "filters": filters, "exit_rules": [], "reason": ""}

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
        if cross != "UP":
            result["reason"] = f"no up-crossover (crossover={cross})"
            if cross == "DOWN":
                result["reason"] += "; sell signal ignored: no shares owned, no short selling"
        elif failed:
            result["reason"] = "up-crossover but filters failed: " + ", ".join(failed)
        else:
            result.update(signal="BUY", reason="up-crossover and all enabled filters passed")
    return result


INDICATOR_COLUMNS = ["sma_fast", "sma_slow", "sma_trend", "rsi", "macd", "macd_signal", "vol_avg", "adx", "atr"]


def stop_price_for(entry_price, symbol, state, current_atr):
    """Stop = entry - multiplier * ATR at entry (falls back to current ATR if unknown)."""
    atr_at_entry = state.get(symbol, {}).get("atr_at_entry")
    if atr_at_entry is None:
        log.warning("No saved ATR at entry for %s; using current ATR for the stop", symbol)
        atr_at_entry = current_atr
    return entry_price - config.ATR_STOP_MULTIPLIER * atr_at_entry


def buy_quantity(account, price, current_atr):
    # non-marginable buying power is reduced by pending buy orders, so two symbols
    # bought on the same morning can't both spend the same cash
    cash = min(float(account.cash), float(account.non_marginable_buying_power))
    if config.USE_RISK_SIZING:
        risk_dollars = float(account.equity) * config.RISK_PER_TRADE_PCT / 100
        qty = math.floor(risk_dollars / (config.ATR_STOP_MULTIPLIER * current_atr))
    else:
        qty = config.TRADE_QTY
    affordable = math.floor(cash / price)
    return max(0, min(qty, affordable))


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
        "close": fmt(last.Close), "sma_fast": fmt(last.sma_fast), "sma_slow": fmt(last.sma_slow),
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

    decision = evaluate(prev, last, price, position_qty, stop)
    signal, reason = decision["signal"], decision["reason"]
    row["crossover"] = decision["crossover"]
    row.update(decision["filters"])

    qty = 0
    if signal == "SELL":
        qty = position_qty
    elif signal == "BUY":
        qty = buy_quantity(account, price, float(last.atr))
        if qty < 1:
            signal = "NONE"
            reason = "up-crossover and filters passed, but position size is 0 (cash or risk limit)"

    row.update(signal=signal, reason=reason)

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
                             "date": row["run_time"]}
        else:
            state.pop(symbol, None)
        save_state(state)
    return finish(row)


def finish(row):
    filters = " ".join(f"{k}={row.get(k, '')}" for k in
                       ("trend", "rsi_filter", "macd_filter", "volume_filter", "adx_filter"))
    log.info(
        "%s %s price=%s | SMA%d=%s SMA%d=%s SMA%d=%s RSI=%s MACD=%s/%s Vol=%s/%s ADX=%s ATR=%s | "
        "cross=%s %s | pos=%s stop=%s | %s -> %s | %s",
        row["bar_date"], row["symbol"], row["price"], config.FAST_MA, row["sma_fast"],
        config.SLOW_MA, row["sma_slow"], config.TREND_MA, row["sma_trend"], row["rsi"],
        row["macd"], row["macd_signal"], row["volume"], row["vol_avg"], row["adx"], row["atr"],
        row.get("crossover", ""), filters, row["position_qty"], row.get("stop_price", ""),
        row["signal"], row["action"], row["reason"],
    )
    write_csv(row)
    return row


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
    if set(config.SYMBOLS) <= symbols_done_today():
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
        symbols = config.SYMBOLS
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
