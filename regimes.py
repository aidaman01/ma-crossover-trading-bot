"""Market-regime labels that use only information available before each day.

A regime label for trading day t is computed from data up to and including the close of
day t-1, so it is known before day t's return happens (no look-ahead):

* Trend:      "bull" if SPY's close(t-1) > its 200-day simple average(t-1), else "bear".
* Volatility: realized volatility rv(t-1) = std of SPY's last 63 daily returns ending at
              t-1 x sqrt(252); "high" if rv(t-1) > the median of all rv values observed up
              to and including t-1 (an expanding median, at least 252 values), else "low".

Days without enough history get no label (NaN) and are left out of the regime statistics.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

import metrics

TREND_WINDOW = 200
VOL_WINDOW = 63
VOL_MEDIAN_MIN_HISTORY = 252


def trend_regime(close: pd.Series, window: int = TREND_WINDOW) -> pd.Series:
    """'bull' / 'bear' for each day t from close and SMA at t-1 (NaN during warm-up)."""
    sma = close.rolling(window, min_periods=window).mean()
    label = pd.Series(np.where(close > sma, "bull", "bear"), index=close.index, dtype=object)
    label[sma.isna() | close.isna()] = None
    return label.shift(1)


def volatility_regime(close: pd.Series, window: int = VOL_WINDOW,
                      min_history: int = VOL_MEDIAN_MIN_HISTORY) -> pd.Series:
    """'high' / 'low' for each day t from realized vol at t-1 vs its expanding median at t-1."""
    rv = close.pct_change(fill_method=None).rolling(window, min_periods=window).std(ddof=1) * math.sqrt(
        metrics.PERIODS_PER_YEAR)
    median = rv.expanding(min_periods=min_history).median()
    label = pd.Series(np.where(rv > median, "high", "low"), index=close.index, dtype=object)
    label[rv.isna() | median.isna()] = None
    return label.shift(1)


def regime_stats(returns: pd.Series, labels: pd.Series, rf: pd.Series | None = None) -> pd.DataFrame:
    """Per-regime statistics of daily returns: share of days, annualized arithmetic mean
    return (mean x 252), annualized compounded return (exp(mean log(1 + r) x 252) - 1, i.e.
    the growth rate if every day of that regime were strung together), annualized
    volatility, Sharpe (excess over rf) and share of positive days."""
    df = pd.DataFrame({"r": returns, "label": labels.reindex(returns.index)}).dropna()
    rows = {}
    for name, grp in df.groupby("label"):
        r = grp["r"]
        rows[name] = {
            "share_of_days": len(r) / len(df),
            "days": len(r),
            "ann_return": metrics.annualized_mean_return(r),
            "ann_compounded": float(np.expm1(np.log1p(r).mean() * metrics.PERIODS_PER_YEAR)),
            "ann_vol": metrics.annualized_volatility(r),
            "sharpe": metrics.sharpe_ratio(r, rf),
            "positive_days": float((r > 0).mean()),
        }
    return pd.DataFrame(rows).T
