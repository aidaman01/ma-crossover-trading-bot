"""Small synthetic market data for offline tests."""
from __future__ import annotations

import numpy as np
import pandas as pd

import research_data as rd


def calendar(start: str = "2018-01-01", periods: int = 600) -> pd.DatetimeIndex:
    """Weekday 'trading' calendar."""
    return pd.bdate_range(start, periods=periods)


def random_walk(index: pd.DatetimeIndex, drift: float = 0.0003, vol: float = 0.01, seed: int = 0,
                start: float = 100.0) -> pd.Series:
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, len(index))
    return pd.Series(start * np.exp(np.cumsum(r)), index=index)


def ohlcv(close: pd.Series, gap: float = 0.001) -> pd.DataFrame:
    """Bars around a close series: open = previous close x (1 + gap), high/low bracket both."""
    open_ = close.shift(1).fillna(close.iloc[0]) * (1 + gap)
    high = np.maximum(open_, close) * 1.004
    low = np.minimum(open_, close) * 0.996
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close,
                         "Volume": 1_000_000.0}, index=close.index)


def raw_market(closes: dict[str, pd.Series], rate_pct: float = 3.6, with_bil: bool = True) -> dict:
    """{ticker: OHLCV} + ^IRX at a constant rate; BIL (if requested) grows at that rate."""
    index = next(iter(closes.values())).index
    raw = {t: ohlcv(c) for t, c in closes.items()}
    if with_bil and "BIL" not in raw:
        days = index.to_series().diff().dt.days.fillna(0)
        bil = 90 * (1 + rate_pct / 100 * days / 360).cumprod()
        raw["BIL"] = ohlcv(bil, gap=0.0)
    raw[rd.RATE_TICKER] = pd.DataFrame({"Open": rate_pct, "High": rate_pct, "Low": rate_pct,
                                        "Close": rate_pct, "Volume": 0.0}, index=index)
    return raw


def market(closes: dict[str, pd.Series], **kw) -> rd.Market:
    return rd.Market(raw_market(closes, **kw), calendar_symbol=next(iter(closes)))
