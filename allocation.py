"""Monthly allocation rules shared by the live bot (bot.py) and the research (research.py).

Both strategies decide on target weights from daily closes up to a signal date s (the last
completed trading day before the rebalance). Weights not allocated are held in cash
(the live bot holds cash as BIL, a 0-3 month T-bill ETF).

Split rebalancing: the portfolio is divided into TRANCHES equal parts. Part k rebalances
on trading day 1 + k * TRANCHE_SPACING of each month, so no single day's prices decide
the whole portfolio (less "timing luck").
"""
import pandas as pd

CASH = "BIL"
TRADING_DAYS_PER_MONTH = 21


class Prices:
    """Daily closes (one column per symbol) with month-end and moving-average helpers."""

    def __init__(self, close: pd.DataFrame):
        self.close = close
        self.calendar = close.index
        cal = self.calendar.to_series()
        self.month_end_idx = pd.DatetimeIndex(sorted(cal.groupby(cal.dt.to_period("M")).max()))
        self.month_ends = set(self.month_end_idx)
        self.pos = {d: i for i, d in enumerate(self.calendar)}
        self._sma: dict[int, pd.DataFrame] = {}

    def sma(self, days):
        if days not in self._sma:
            self._sma[days] = self.close.rolling(days, min_periods=days).mean()
        return self._sma[days]

    def monthly_points(self, series, s, n, month_end=None):
        """The last n monthly values of `series` up to signal date s.

        month_end=True (s is the last trading day of its month, i.e. the rebalance is on
        the first trading day of the next month): calendar month-end closes, the standard
        definition. Otherwise (later tranches): step back 21 trading days at a time.
        None = treat s as a month-end if it is the last date of its month in the data.
        """
        if month_end is None:
            month_end = s in self.month_ends
        if month_end:
            before = self.month_end_idx[self.month_end_idx.to_period("M") < s.to_period("M")]
            idx = list(before[max(0, len(before) - (n - 1)):]) + [s] if n > 1 else [s]
        else:
            p = self.pos[s]
            idx = [self.calendar[p - TRADING_DAYS_PER_MONTH * k] for k in range(n)
                   if p - TRADING_DAYS_PER_MONTH * k >= 0][::-1]
        return series.reindex(idx)


def trend_signals(px, s, assets, months=10, month_end=None):
    """{asset: (close, N-month average, above)} - Faber-style trend allocation."""
    out = {}
    for a in assets:
        pts = px.monthly_points(px.close[a], s, months, month_end)
        ok = len(pts) == months and pts.notna().all()
        avg = pts.mean() if ok else float("nan")
        close = px.close.at[s, a]
        out[a] = (close, avg, bool(ok and close > avg))
    return out


def hold_signals(px, s, assets, days=200):
    """{asset: (close, N-day average, above)} - hold by default, cash below the average."""
    sma = px.sma(days)
    out = {}
    for a in assets:
        avg, close = sma.at[s, a], px.close.at[s, a]
        out[a] = (close, avg, bool(not pd.isna(avg) and close > avg))
    return out


def weights_from(signals):
    """Equal weight for every asset above its average; the rest is cash."""
    n = len(signals)
    return {a: 1 / n for a, (_, _, above) in signals.items() if above}


def tranche_day(k, spacing):
    """1-based trading day of the month on which tranche k rebalances."""
    return 1 + k * spacing
