"""Metric formulas checked against hand-computed values."""
import math

import numpy as np
import pandas as pd
import pytest

import metrics


def series(values, start="2020-01-01", freq="B"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq=freq), dtype=float)


def test_total_return_and_cagr_exact_two_years():
    idx = pd.DatetimeIndex(["2020-01-01", "2022-01-01"])          # 731 calendar days
    eq = pd.Series([100.0, 200.0], index=idx)
    assert metrics.total_return(eq) == pytest.approx(1.0)
    years = 731 / 365.25
    assert metrics.cagr(eq) == pytest.approx(2 ** (1 / years) - 1)


def test_cagr_with_base_counts_the_first_day():
    eq = series([110.0, 121.0], start="2021-01-04")
    # base 100 is the value the day before the first observation
    years = (eq.index[-1] - (eq.index[0] - pd.Timedelta(days=1))).days / 365.25
    assert metrics.cagr(eq, base=100) == pytest.approx(1.21 ** (1 / years) - 1)
    assert metrics.total_return(eq, base=100) == pytest.approx(0.21)


def test_volatility_of_alternating_returns():
    r = series([0.01, -0.01] * 50)
    expected = r.std(ddof=1) * math.sqrt(252)
    assert metrics.annualized_volatility(r) == pytest.approx(expected)
    assert metrics.annualized_mean_return(r) == pytest.approx(0.0, abs=1e-12)


def test_sharpe_uses_excess_returns_and_sample_std():
    r = series([0.002, 0.001, -0.001, 0.003, 0.0, 0.001])
    rf = pd.Series(0.0001, index=r.index)
    e = r - 0.0001
    assert metrics.sharpe_ratio(r, rf) == pytest.approx(e.mean() / e.std(ddof=1) * math.sqrt(252))
    assert metrics.sharpe_ratio(r) == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(252))


def test_downside_deviation_and_sortino_by_hand():
    r = series([0.02, -0.01, 0.0, -0.03, 0.01])
    downside = np.array([0.0, -0.01, 0.0, -0.03, 0.0])
    dd = math.sqrt((downside ** 2).mean()) * math.sqrt(252)       # averaged over ALL days
    assert metrics.downside_deviation(r) == pytest.approx(dd)
    assert metrics.sortino_ratio(r) == pytest.approx(r.mean() * 252 / dd)


def test_max_drawdown_known_path_and_base():
    eq = series([100, 120, 90, 130, 104])
    assert metrics.max_drawdown(eq) == pytest.approx(90 / 120 - 1)          # -25%
    assert metrics.max_drawdown(eq, base=150) == pytest.approx(90 / 150 - 1)  # peak includes base
    assert metrics.max_drawdown(series([100, 101, 102])) == 0.0


def test_calmar_is_cagr_over_abs_drawdown():
    eq = series([100, 120, 90, 130, 104])
    assert metrics.calmar_ratio(eq) == pytest.approx(metrics.cagr(eq) / 0.25)
    assert math.isnan(metrics.calmar_ratio(series([100, 101, 102])))


def test_monthly_win_rate_and_calendar_returns():
    idx = pd.DatetimeIndex(["2021-01-29", "2021-02-26", "2021-03-31", "2021-04-30"])
    eq = pd.Series([100.0, 110.0, 99.0, 99.0], index=idx)
    m = metrics.calendar_returns(eq, "ME", base=95.0)
    assert list(np.round(m.to_numpy(), 6)) == [round(100 / 95 - 1, 6), 0.1, -0.1, 0.0]
    assert metrics.monthly_win_rate(eq, base=95.0) == pytest.approx(2 / 4)


def test_worst_full_year_ignores_partial_last_year():
    idx = pd.DatetimeIndex(["2020-01-02", "2020-12-31", "2021-12-31", "2022-03-31"])
    eq = pd.Series([100.0, 90.0, 99.0, 50.0], index=idx)        # 2022 is partial: excluded
    worst, year = metrics.worst_full_year(eq, base=100.0)
    assert year == 2020 and worst == pytest.approx(-0.10)


def test_missing_observations_are_dropped_and_missing_rf_is_zero():
    r = series([0.01, np.nan, -0.02, 0.03])
    assert metrics.annualized_volatility(r) == pytest.approx(r.dropna().std(ddof=1) * math.sqrt(252))
    rf = pd.Series([0.001], index=r.index[:1])                    # rf missing on later days
    e = metrics.excess_returns(r.dropna(), rf)
    assert list(np.round(e.to_numpy(), 6)) == [0.009, -0.02, 0.03]


def test_daily_returns_with_base_includes_first_day():
    eq = series([105.0, 110.25])
    r = metrics.daily_returns(eq, base=100.0)
    assert list(np.round(r.to_numpy(), 6)) == [0.05, 0.05]


def test_rolling_statistics_are_trailing_only():
    r = series(np.random.default_rng(1).normal(0, 0.01, 400))
    vol = metrics.rolling_volatility(r, 63)
    changed = r.copy()
    changed.iloc[300:] *= 5                                       # change the future
    vol2 = metrics.rolling_volatility(changed, 63)
    pd.testing.assert_series_equal(vol.iloc[:300], vol2.iloc[:300])
    shp = metrics.rolling_sharpe(r, None, 252)
    assert shp.iloc[:251].isna().all() and shp.iloc[251:].notna().all()


def test_summarize_keys_and_consistency():
    eq = series(100 * np.exp(np.cumsum(np.random.default_rng(2).normal(0.0004, 0.01, 600))))
    out = metrics.summarize(eq, None, base=100.0)
    for key in ("total_return", "cagr", "vol", "sharpe", "sortino", "downside_dev", "max_dd", "calmar",
                "monthly_win_rate", "worst_year", "avg_annual_return"):
        assert key in out
    assert out["calmar"] == pytest.approx(out["cagr"] / abs(out["max_dd"]))
