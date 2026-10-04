"""Strategy and bot settings. Edit values here; no code changes needed."""

# --- Strategy selector ----------------------------------------------------------
# "trend_allocation": monthly trend allocation across asset classes (ACTIVE, see README)
# "hold_by_default":  monthly; 12 US ETFs, each in cash while below its 200-day average
#                     (researched, not chosen: deeper drawdowns with split rebalancing)
# "ema_crossover":    daily EMA 10/30 crossover on 12 ETFs (setup J, archived below)
STRATEGY = "trend_allocation"

# --- Trend allocation (STRATEGY = "trend_allocation") ---------------------------
# Equal weight (1/7) in every asset whose close is above its 10-month average; the
# rest is held in CASH_SYMBOL. Chosen by the research in research.py (README).
ALLOCATION_ASSETS = ["SPY", "EFA", "IEF", "TLT", "GLD", "DBC", "VNQ"]
ALLOCATION_MA_MONTHS = 10
CASH_SYMBOL = "BIL"            # 0-3 month T-bill ETF used as cash

# --- Hold by default (STRATEGY = "hold_by_default") -------------------------------
HOLD_ASSETS = ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU"]
HOLD_SMA_DAYS = 200

# --- Split rebalancing (both monthly strategies) --------------------------------
# The account is split into TRANCHES virtual parts; part k rebalances on trading day
# 1 + k * TRANCHE_SPACING_DAYS of each month (days 1, 6, 11, 16), which spreads out
# the luck of rebalancing on one particular day.
TRANCHES = 4
TRANCHE_SPACING_DAYS = 5
MIN_ORDER_VALUE = 25           # skip orders smaller than this ($) to avoid dust trades

# --- Archived: setup J (STRATEGY = "ema_crossover") -------------------------------
# EMA 10/30 crossover, no entry filters, 12 liquid ETFs, equal-weight positions.
# Each symbol is checked every run and has its own position, stop loss and log rows.
# Also used by backtest.py for the setup comparison.
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

# --- Data (setup J) -------------------------------------------------------------
LOOKBACK_BARS = 300     # trading days of daily history to download from yfinance
# When the market is open, today's daily bar is still forming. True = signals use
# only completed days (the stop-loss check still uses the latest live price).
USE_COMPLETED_BARS_ONLY = True

# --- Setup J: confluence filters (a BUY needs an up-crossover AND every enabled filter)
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

# --- Setup J: exit rules (SELL if ANY enabled rule triggers) ---------------------
USE_CROSS_EXIT = True       # fast MA crosses below slow MA
USE_RSI_EXIT = True         # RSI rises above RSI_EXIT_ABOVE
RSI_EXIT_ABOVE = 75
USE_ATR_STOP = True         # price at or below entry - ATR_STOP_MULTIPLIER * ATR
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 2.0

# --- Setup J: position sizing ----------------------------------------------------
# "equal_weight": each symbol gets equity / len(SYMBOLS), so all open positions
#                 together can never exceed account equity
# "fixed":        TRADE_QTY shares per trade
# "risk":         size so hitting the ATR stop loses RISK_PER_TRADE_PCT of equity
# Every mode is capped by available cash (never uses margin) and buys whole shares.
POSITION_SIZING = "equal_weight"
TRADE_QTY = 1
RISK_PER_TRADE_PCT = 1.0

# --- Execution ----------------------------------------------------------------
DRY_RUN = True          # True = log only; False = place real orders on the (paper) account
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
STATE_FILE = "bot_state.json"   # tranche ledger (allocation) / ATR at entry and last crossover (setup J)
