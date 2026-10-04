"""Strategy and bot settings. Edit values here; no code changes needed."""

# --- Instrument & core signal -------------------------------------------------
# Each symbol is checked every run with the same strategy, and has its own
# position, stop loss and log rows.
SYMBOLS = ["SPY", "QQQ"]
FAST_MA = 20            # fast simple moving average (days)
SLOW_MA = 50            # slow simple moving average (days)
TRADE_QTY = 1           # shares per trade (used when USE_RISK_SIZING is False)

# --- Data ---------------------------------------------------------------------
LOOKBACK_BARS = 300     # trading days of daily history to download from yfinance
# When the market is open, today's daily bar is still forming. True = signals use
# only completed days (the stop-loss check still uses the latest live price).
USE_COMPLETED_BARS_ONLY = True

# --- Confluence filters (a BUY needs an up-crossover AND every enabled filter) --
USE_TREND_FILTER = True     # 1. close above the long-term moving average
TREND_MA = 200

USE_RSI_FILTER = True       # 2. RSI inside [RSI_BUY_MIN, RSI_BUY_MAX]
RSI_PERIOD = 14
RSI_BUY_MIN = 50
RSI_BUY_MAX = 70

USE_MACD_FILTER = True      # 3. MACD line above its signal line
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

USE_VOLUME_FILTER = True    # 4. today's volume above its average
VOLUME_MA = 20

USE_ADX_FILTER = True       # 5. ADX above ADX_MIN (trending market)
ADX_PERIOD = 14
ADX_MIN = 20

# --- Exit rules (SELL if ANY enabled rule triggers) ---------------------------
USE_CROSS_EXIT = True       # fast MA crosses below slow MA
USE_RSI_EXIT = True         # RSI rises above RSI_EXIT_ABOVE
RSI_EXIT_ABOVE = 75
USE_ATR_STOP = True         # price at or below entry - ATR_STOP_MULTIPLIER * ATR
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 2.0

# --- Risk management ----------------------------------------------------------
# Off: buy TRADE_QTY shares. On: size so that hitting the ATR stop loses at most
# RISK_PER_TRADE_PCT of account equity (capped by available cash).
USE_RISK_SIZING = False
RISK_PER_TRADE_PCT = 1.0

# --- Execution ----------------------------------------------------------------
DRY_RUN = True          # True = print/log what would happen, never place orders
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
BACKTEST_EXTRA_SYMBOLS = ["IWM", "DIA", "XLK", "XLE"]   # robustness check for the best variant

# --- Files --------------------------------------------------------------------
LOG_DIR = "logs"
STATE_FILE = "bot_state.json"   # remembers ATR at entry for the stop loss
