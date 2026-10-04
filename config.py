"""Strategy and bot settings. Edit values here; no code changes needed."""

# --- Instrument & core signal -------------------------------------------------
# Each symbol is checked every run with the same strategy, and has its own
# position, stop loss and log rows.
# Setup "J" from the backtest study (see README): EMA 10/30 crossover, no entry
# filters, 12 liquid ETFs, equal-weight positions.
SYMBOLS = ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU"]
MA_TYPE = "EMA"         # "SMA" or "EMA" for the fast/slow crossover pair
FAST_MA = 10            # fast moving average (days)
SLOW_MA = 30            # slow moving average (days)
# Days after an up-crossover during which a BUY may still happen, as long as the fast
# MA is still above the slow MA and the filters pass. 0 = only on the crossover day.
# With all filters off a BUY happens on the crossover day; the window still lets the bot
# catch up if a scheduled run was missed.
ENTRY_WINDOW_DAYS = 10
ONE_ENTRY_PER_CROSSOVER = True  # after exiting, wait for the next crossover to re-enter

# --- Data ---------------------------------------------------------------------
LOOKBACK_BARS = 300     # trading days of daily history to download from yfinance
# When the market is open, today's daily bar is still forming. True = signals use
# only completed days (the stop-loss check still uses the latest live price).
USE_COMPLETED_BARS_ONLY = True

# --- Confluence filters (a BUY needs an up-crossover AND every enabled filter) --
# All off in the current setup: in the backtests they cut trades without improving
# CAGR / max drawdown. Settings are kept so they can be switched back on.
USE_TREND_FILTER = False    # 1. close above the long-term moving average
TREND_MA = 200

USE_RSI_FILTER = False      # 2. RSI inside [RSI_BUY_MIN, RSI_BUY_MAX]
RSI_PERIOD = 14
RSI_BUY_MIN = 50
RSI_BUY_MAX = 70

USE_MACD_FILTER = False     # 3. MACD line above its signal line
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

USE_VOLUME_FILTER = False   # 4. today's volume above its average
VOLUME_MA = 20

USE_ADX_FILTER = False      # 5. ADX above ADX_MIN (trending market)
ADX_PERIOD = 14
ADX_MIN = 20

# --- Exit rules (SELL if ANY enabled rule triggers) ---------------------------
USE_CROSS_EXIT = True       # fast MA crosses below slow MA
USE_RSI_EXIT = True         # RSI rises above RSI_EXIT_ABOVE
RSI_EXIT_ABOVE = 75
USE_ATR_STOP = True         # price at or below entry - ATR_STOP_MULTIPLIER * ATR
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 2.0

# --- Position sizing ----------------------------------------------------------
# "equal_weight": each symbol gets equity / len(SYMBOLS), so all open positions
#                 together can never exceed account equity
# "fixed":        TRADE_QTY shares per trade
# "risk":         size so hitting the ATR stop loses RISK_PER_TRADE_PCT of equity
# Every mode is capped by available cash (never uses margin) and buys whole shares.
POSITION_SIZING = "equal_weight"
TRADE_QTY = 1
RISK_PER_TRADE_PCT = 1.0

# --- Execution ----------------------------------------------------------------
DRY_RUN = False         # False = place real orders on the (paper) account; True = log only
PAPER_TRADING = True    # keep True: trade on the Alpaca paper account

# --- Scheduling (python bot.py --scheduled) -----------------------------------
# Earliest US Eastern time the scheduled run may happen (24h "HH:MM"). If the PC
# was off/asleep, the run happens at the first trigger after this while the market
# is still open. Runs at most once per trading day.
SCHEDULED_RUN_TIME_ET = "09:35"

# --- Backtest (python backtest.py  or  python bot.py --backtest) ---------------
BACKTEST_YEARS = 5
BACKTEST_START_CAPITAL = 100_000
BACKTEST_COST_PCT = 0.05     # slippage + fees per side, % of trade value (0.05% = 5 bps)

# --- Files --------------------------------------------------------------------
LOG_DIR = "logs"
STATE_FILE = "bot_state.json"   # ATR at entry (stop loss) and last crossover traded
