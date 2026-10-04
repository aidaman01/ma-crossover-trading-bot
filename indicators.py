"""Technical indicators computed with pandas (Wilder smoothing where standard)."""
import numpy as np
import pandas as pd


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    avg_gain = _wilder(delta.clip(lower=0), period)
    avg_loss = _wilder(-delta.clip(upper=0), period)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(avg_loss != 0, 100.0).where(avg_gain.notna())


def macd(close: pd.Series, fast: int, slow: int, signal: int):
    ema_fast = close.ewm(span=fast, min_periods=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, min_periods=slow, adjust=False).mean()
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, min_periods=signal, adjust=False).mean()
    return line, sig


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    return pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev_close).abs(), (df["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    return _wilder(true_range(df), period)


def adx(df: pd.DataFrame, period: int) -> pd.Series:
    up = df["High"].diff()
    down = -df["Low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = _wilder(true_range(df), period)
    plus_di = 100 * _wilder(plus_dm, period) / tr
    minus_di = 100 * _wilder(minus_dm, period) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _wilder(dx, period)


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, min_periods=period, adjust=False).mean()


def moving_average(series: pd.Series, period: int, kind: str) -> pd.Series:
    return ema(series, period) if kind.upper() == "EMA" else sma(series, period)


def add_indicators(df: pd.DataFrame, cfg) -> pd.DataFrame:
    df = df.copy()
    df["ma_fast"] = moving_average(df["Close"], cfg.FAST_MA, cfg.MA_TYPE)
    df["ma_slow"] = moving_average(df["Close"], cfg.SLOW_MA, cfg.MA_TYPE)
    df["sma_trend"] = sma(df["Close"], cfg.TREND_MA)

    # Up-crossover bookkeeping (causal: each row only looks backwards).
    # cross_age = bars since the most recent up-crossover (0 = it happened on this bar),
    # last_cross_up = date of that crossover; both NaN before the first crossover.
    cross_up = (df["ma_fast"] > df["ma_slow"]) & (df["ma_fast"].shift(1) <= df["ma_slow"].shift(1))
    pos = pd.Series(np.arange(len(df)), index=df.index, dtype=float)
    df["cross_age"] = pos - pos.where(cross_up).ffill()
    df["last_cross_up"] = df.index.to_series().where(cross_up).ffill()

    df["rsi"] = rsi(df["Close"], cfg.RSI_PERIOD)
    df["macd"], df["macd_signal"] = macd(df["Close"], cfg.MACD_FAST, cfg.MACD_SLOW, cfg.MACD_SIGNAL)
    df["vol_avg"] = sma(df["Volume"], cfg.VOLUME_MA)
    df["adx"] = adx(df, cfg.ADX_PERIOD)
    df["atr"] = atr(df, cfg.ATR_PERIOD)
    return df
