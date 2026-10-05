"""Performance metrics shared by the research pipeline, the archived backtest and the dashboard.

Conventions (documented once here and in docs/research.md):

* Input: a daily equity curve (portfolio value at each trading day's close) and, where
  relevant, the daily risk-free return on the same dates. Returns are simple daily returns
  r_t = V_t / V_{t-1} - 1. If a starting value (``base``) is given, it is treated as the
  value at the close before the first observation, so the first day's return is included.
* Trading days per year: 252 (``PERIODS_PER_YEAR``). Calendar-time quantities (CAGR) use
  actual calendar days / 365.25.
* Risk-free rate: the daily total return of the cash asset (BIL, or the 3-month T-bill
  rate before BIL existed). Excess return e_t = r_t - rf_t.
* Missing observations: NaN returns are dropped before any statistic is computed; a
  missing risk-free observation is treated as 0 for that day (``excess_returns``).
* Volatility / Sharpe use the sample standard deviation (ddof = 1).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

PERIODS_PER_YEAR = 252


def daily_returns(equity: pd.Series, base: float | None = None) -> pd.Series:
    """Simple daily returns of an equity curve; ``base`` = value before the first day."""
    curve = equity.astype(float)
    if base is not None:
        start = curve.index[0] - pd.Timedelta(days=1)
        curve = pd.concat([pd.Series([float(base)], index=[start]), curve])
    return curve.pct_change().dropna()


def excess_returns(returns: pd.Series, rf: pd.Series | None) -> pd.Series:
    """r_t - rf_t, with rf aligned on the same dates (missing rf -> 0)."""
    if rf is None:
        return returns
    return returns - rf.reindex(returns.index).fillna(0.0)


def total_return(equity: pd.Series, base: float | None = None) -> float:
    """V_end / V_start - 1 (V_start = ``base`` if given, else the first value)."""
    start = float(base) if base is not None else float(equity.iloc[0])
    return float(equity.iloc[-1]) / start - 1


def cagr(equity: pd.Series, base: float | None = None) -> float:
    """Compound annual growth rate: (V_end / V_start) ** (365.25 / calendar days) - 1."""
    start_value = float(base) if base is not None else float(equity.iloc[0])
    start_date = equity.index[0] - pd.Timedelta(days=1) if base is not None else equity.index[0]
    years = (equity.index[-1] - start_date).days / 365.25
    if years <= 0:
        return float("nan")
    return (float(equity.iloc[-1]) / start_value) ** (1 / years) - 1


def annualized_volatility(returns: pd.Series) -> float:
    """Sample standard deviation of daily returns x sqrt(252)."""
    r = returns.dropna()
    return float(r.std(ddof=1) * math.sqrt(PERIODS_PER_YEAR)) if len(r) > 1 else float("nan")


def annualized_mean_return(returns: pd.Series) -> float:
    """Arithmetic mean daily return x 252 (not compounded; compare with CAGR)."""
    r = returns.dropna()
    return float(r.mean() * PERIODS_PER_YEAR) if len(r) else float("nan")


def sharpe_ratio(returns: pd.Series, rf: pd.Series | None = None) -> float:
    """mean(excess) / std(excess) x sqrt(252), daily excess returns over the cash rate."""
    e = excess_returns(returns.dropna(), rf)
    sd = e.std(ddof=1)
    return float(e.mean() / sd * math.sqrt(PERIODS_PER_YEAR)) if len(e) > 1 and sd > 0 else float("nan")


def downside_deviation(returns: pd.Series, rf: pd.Series | None = None) -> float:
    """Annualized downside deviation of excess returns below a 0 target:

    sqrt(mean(min(e_t, 0)^2)) x sqrt(252), averaged over ALL days (the Sortino & Price
    target semideviation), not only the negative ones.
    """
    e = excess_returns(returns.dropna(), rf)
    if not len(e):
        return float("nan")
    return float(np.sqrt(np.mean(np.minimum(e.to_numpy(), 0.0) ** 2)) * math.sqrt(PERIODS_PER_YEAR))


def sortino_ratio(returns: pd.Series, rf: pd.Series | None = None) -> float:
    """Annualized mean excess return / annualized downside deviation (target 0)."""
    e = excess_returns(returns.dropna(), rf)
    dd = downside_deviation(returns, rf)
    return float(e.mean() * PERIODS_PER_YEAR / dd) if dd and dd > 0 else float("nan")


def drawdown_series(equity: pd.Series, base: float | None = None) -> pd.Series:
    """V_t / max(V_0..V_t) - 1; the running peak includes ``base`` if given."""
    peak = equity.cummax()
    if base is not None:
        peak = peak.clip(lower=float(base))
    return equity / peak - 1


def max_drawdown(equity: pd.Series, base: float | None = None) -> float:
    """Largest peak-to-trough decline of the daily closing equity (a negative number)."""
    return float(min(drawdown_series(equity, base).min(), 0.0))


def calmar_ratio(equity: pd.Series, base: float | None = None) -> float:
    """CAGR / |max drawdown| over the same period."""
    mdd = max_drawdown(equity, base)
    return cagr(equity, base) / abs(mdd) if mdd < 0 else float("nan")


def calendar_returns(equity: pd.Series, freq: str, base: float | None = None) -> pd.Series:
    """Compounded returns per calendar period ('ME' months or 'YE' years) from closes."""
    ends = equity.groupby(pd.DatetimeIndex(equity.index).to_period(freq[0])).last()
    prev = ends.shift(1)
    if base is not None:
        prev.iloc[0] = float(base)
    return (ends / prev - 1).dropna()


def monthly_win_rate(equity: pd.Series, base: float | None = None) -> float:
    """Share of calendar months with a positive return (partial first/last months included)."""
    m = calendar_returns(equity, "ME", base)
    return float((m > 0).mean()) if len(m) else float("nan")


def worst_full_year(equity: pd.Series, base: float | None = None) -> tuple[float, int | None]:
    """(return, year) of the worst complete calendar year; the final partial year is excluded,
    and the first year only counts if the curve starts in its first days."""
    y = calendar_returns(equity, "YE", base)
    last_year = equity.index[-1].year
    first_complete = equity.index[0].year if equity.index[0].dayofyear <= 7 else equity.index[0].year + 1
    full = y[[p.year < last_year and p.year >= first_complete for p in y.index]]
    if not len(full):
        return float("nan"), None
    worst_period = full.index[int(np.argmin(full.to_numpy()))]
    return float(full.min()), int(worst_period.year)


def rolling_volatility(returns: pd.Series, window: int = 63) -> pd.Series:
    """Annualized rolling volatility over ``window`` trading days (trailing, no look-ahead)."""
    return returns.rolling(window, min_periods=window).std(ddof=1) * math.sqrt(PERIODS_PER_YEAR)


def rolling_sharpe(returns: pd.Series, rf: pd.Series | None = None, window: int = 252) -> pd.Series:
    """Trailing ``window``-day Sharpe ratio of daily excess returns (annualized)."""
    e = excess_returns(returns, rf)
    mean = e.rolling(window, min_periods=window).mean()
    sd = e.rolling(window, min_periods=window).std(ddof=1)
    return mean / sd * math.sqrt(PERIODS_PER_YEAR)


def summarize(equity: pd.Series, rf: pd.Series | None = None, base: float | None = None) -> dict:
    """All curve-based metrics for one equity curve (activity metrics are added by callers)."""
    r = daily_returns(equity, base)
    worst, worst_year = worst_full_year(equity, base)
    return {
        "total_return": total_return(equity, base),
        "cagr": cagr(equity, base),
        "avg_annual_return": annualized_mean_return(r),
        "vol": annualized_volatility(r),
        "sharpe": sharpe_ratio(r, rf),
        "sortino": sortino_ratio(r, rf),
        "downside_dev": downside_deviation(r, rf),
        "max_dd": max_drawdown(equity, base),
        "calmar": calmar_ratio(equity, base),
        "monthly_win_rate": monthly_win_rate(equity, base),
        "worst_year": worst,
        "worst_year_label": worst_year,
    }
