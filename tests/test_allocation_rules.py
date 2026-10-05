"""Trend allocation rules (allocation.py), shared by the live bot and the research."""
import numpy as np
import pandas as pd
import pytest
from synthetic import calendar

import allocation


def prices(cols: dict, index=None) -> allocation.Prices:
    index = index if index is not None else calendar("2019-01-01", 400)
    return allocation.Prices(pd.DataFrame(cols, index=index))


def test_monthly_points_month_end_path_uses_calendar_month_ends():
    idx = calendar("2019-01-01", 400)
    px = prices({"A": np.arange(len(idx), dtype=float)}, idx)
    s = px.month_end_idx[14]                       # a month-end inside the data
    pts = px.monthly_points(px.close["A"], s, 10, month_end=True)
    assert len(pts) == 10 and pts.index[-1] == s
    assert list(pts.index[:-1]) == list(px.month_end_idx[5:14])     # the 9 previous month-ends


def test_monthly_points_mid_month_steps_21_trading_days():
    idx = calendar("2019-01-01", 400)
    px = prices({"A": np.arange(len(idx), dtype=float)}, idx)
    s = idx[300]
    pts = px.monthly_points(px.close["A"], s, 10, month_end=False)
    assert list(pts.index) == [idx[300 - 21 * k] for k in range(9, -1, -1)]


def test_trend_signals_and_equal_weights():
    idx = calendar("2019-01-01", 400)
    px = prices({"UP": np.linspace(50, 150, len(idx)), "DOWN": np.linspace(150, 50, len(idx)),
                 "FLAT": np.full(len(idx), 100.0)}, idx)
    s = px.month_end_idx[15]
    sig = allocation.trend_signals(px, s, ["UP", "DOWN", "FLAT"], 10)
    assert sig["UP"][2] and not sig["DOWN"][2] and not sig["FLAT"][2]       # flat: close == average
    assert sig["UP"][1] == pytest.approx(px.monthly_points(px.close["UP"], s, 10).mean())
    w = allocation.weights_from(sig)
    assert w == {"UP": pytest.approx(1 / 3)}                                 # 1/N of the universe, not 1/held


def test_all_assets_below_average_means_full_cash():
    idx = calendar("2019-01-01", 400)
    px = prices({"A": np.linspace(150, 50, len(idx)), "B": np.linspace(140, 60, len(idx))}, idx)
    w = allocation.weights_from(allocation.trend_signals(px, idx[-1], ["A", "B"], 10))
    assert w == {}                                  # the engine / bot put 100% in BIL


def test_missing_history_or_price_is_not_in_trend():
    idx = calendar("2019-01-01", 400)
    young = np.r_[np.full(300, np.nan), np.linspace(100, 120, 100)]      # only ~5 months of data
    gap = np.linspace(50, 150, len(idx))
    gap[-1] = np.nan                                                      # no close on the signal date
    px = prices({"YOUNG": young, "GAP": gap}, idx)
    sig = allocation.trend_signals(px, idx[-1], ["YOUNG", "GAP"], 10)
    assert sig["YOUNG"][2] is False and np.isnan(sig["YOUNG"][1])
    assert sig["GAP"][2] is False
    assert allocation.weights_from(sig) == {}


def test_hold_signals_use_daily_average():
    idx = calendar("2019-01-01", 260)
    px = prices({"A": np.r_[np.full(250, 100.0), np.full(10, 120.0)], "B": np.full(260, 100.0)}, idx)
    sig = allocation.hold_signals(px, idx[-1], ["A", "B"], 200)
    assert sig["A"][2] and not sig["B"][2]
    assert sig["A"][1] == pytest.approx(px.close["A"].iloc[-200:].mean())


def test_tranche_days():
    assert [allocation.tranche_day(k, 5) for k in range(4)] == [1, 6, 11, 16]
    assert allocation.tranche_day(0, 5) == 1
