"""Research engine (research.py): accounting, costs, schedule, cash, splitting, benchmarks."""
import functools

import numpy as np
import pandas as pd
import pytest
from synthetic import calendar, market, ohlcv, random_walk, raw_market

import research
import research_data as rd

IDX = calendar("2018-01-01", 520)


@pytest.fixture(scope="module")
def m():
    return market({"AAA": random_walk(IDX, 0.0005, 0.01, seed=11),
                   "BBB": random_walk(IDX, -0.0002, 0.012, seed=12)})


def run(m, rule, **kw):
    kw.setdefault("start", IDX[30])
    kw.setdefault("end", IDX[-1])
    return research.run_monthly(m, rule, **kw)


def test_cash_asset_and_tbill_rate_before_bil():
    raw = raw_market({"AAA": random_walk(IDX, seed=1)}, rate_pct=3.6)
    raw["BIL"] = raw["BIL"].iloc[100:]                     # BIL "starts" later
    mk = rd.Market(raw, calendar_symbol="AAA")
    days = (IDX[50] - IDX[49]).days
    assert mk.cash_ret[IDX[50]] == pytest.approx(0.036 * days / 360)       # previous day's rate, /360
    bil = raw["BIL"]["Close"]
    assert mk.cash_ret[IDX[150]] == pytest.approx(bil[IDX[150]] / bil[IDX[149]] - 1)
    pd.testing.assert_series_equal(mk.close[rd.CASH], mk.cash_idx, check_names=False)


def test_buy_and_hold_cost_and_returns_exact(m):
    res = run(m, research.fixed({"AAA": 1.0}), once=True, cost=0.0005)
    d0 = IDX[30]
    o = m.open.loc[d0, "AAA"]
    est = 0.0005 * 100_000
    shares = (100_000 - est) / o
    cash = 100_000 - shares * o - 0.0005 * shares * o                # 0.025 left after cost
    assert cash == pytest.approx(0.025, abs=1e-9)
    assert res["equity"].loc[d0] == pytest.approx(cash + shares * m.close.loc[d0, "AAA"])
    later = IDX[200]                                                  # tiny cash earns interest only
    assert res["equity"].loc[later] == pytest.approx(shares * m.close.loc[later, "AAA"], abs=0.05)
    assert len(res["orders"]) == 1 and res["orders"][0][1] == "AAA"


def test_cash_goes_into_bil_and_pays_costs(m):
    res = run(m, lambda mk, s: {}, cost=0.0005)                  # nothing in trend -> all cash
    first = res["orders"][0]
    assert first[1] == rd.CASH and first[2] == "BUY"
    # 0.05% paid on the BIL purchase: equity after the first day is ~0.05% below capital
    d0 = IDX[30]
    move = m.close.loc[d0, rd.CASH] / m.open.loc[d0, rd.CASH]
    assert res["equity"].loc[d0] == pytest.approx(100_000 * (1 - 0.0005) * move, rel=1e-6)


def test_frictionless_cash_option_compounds_cash_rate(m):
    res = run(m, lambda mk, s: {}, cash_as_asset=False)
    assert res["orders"] == []
    growth = (1 + m.cash_ret.loc[IDX[31]:IDX[-1]]).prod()
    assert res["equity"].iloc[-1] == pytest.approx(100_000 * growth)


def test_rebalance_day_and_signal_date():
    seen = []

    def recorder(mk, s):
        seen.append(s)
        return {"AAA": 1.0}

    mk = market({"AAA": random_walk(IDX, seed=2)})
    res = research.run_monthly(mk, recorder, start=IDX[0], end=IDX[200], offset=5)
    for d in res["rebalances"]:
        month = IDX[(IDX.year == d.year) & (IDX.month == d.month)]
        assert d == month[5]                                         # 6th trading day of the month
    for d, s in zip(res["rebalances"], seen, strict=False):
        assert s == IDX[IDX.get_loc(d) - 1]                          # signal = previous trading day


def test_month_end_signal_mode_uses_last_month_end():
    seen = []
    mk = market({"AAA": random_walk(IDX, seed=3)})
    res = research.run_monthly(mk, lambda m_, s: seen.append(s) or {"AAA": 1.0}, start=IDX[0], end=IDX[200],
                               offset=10, signal="month_end")
    assert seen and all(s in mk.month_ends for s in seen)
    # first month: no completed month-end yet -> no signal -> only the cash asset is bought
    first_day_orders = [o for o in res["orders"] if o[0] == res["rebalances"][0]]
    assert {o[1] for o in first_day_orders} == {rd.CASH}


def test_buffer_and_min_order(m):
    res = run(m, research.fixed({"AAA": 0.6, "BBB": 0.4}), buffer=0.005, min_order=500.0, cost=0.0)
    assert all(o[3] >= 500.0 for o in res["orders"])
    d0 = IDX[30]
    pv = 100_000
    invested = sum(v for d, a, side, v in res["orders"] if d == d0)
    assert invested == pytest.approx(pv * (1 - 0.005))             # 0.5% stays in cash


def test_split_equals_sum_of_parts_and_parts_follow_their_days(m):
    rule = research.fixed({"AAA": 0.5, "BBB": 0.5})
    split = research.run_split(m, rule, offsets=(0, 5), start=IDX[30], end=IDX[-1])
    parts = [research.run_monthly(m, rule, offset=o, capital=50_000, invest_at_start=True, start=IDX[30],
                                  end=IDX[-1]) for o in (0, 5)]
    pd.testing.assert_series_equal(split["equity"], parts[0]["equity"] + parts[1]["equity"])
    assert IDX[30] in split["rebalances"]                           # both parts invest on day one
    some_month = IDX[(IDX.year == 2019) & (IDX.month == 3)]
    assert some_month[0] in split["rebalances"] and some_month[5] in split["rebalances"]


def test_sixty_forty_matches_independent_calculation(m):
    """Frictionless monthly 60/40 vs a direct month-by-month computation."""
    w = {"AAA": 0.6, "BBB": 0.4}
    res = run(m, research.fixed(w), cost=0.0, buffer=0.0, min_order=0.0, cash_as_asset=False)
    cal = IDX[(IDX[30] <= IDX)]
    firsts = cal.to_series().groupby(cal.to_period("M")).min()
    value, expected = 100_000.0, {}
    reb = set(firsts)
    shares = {}
    for d in cal:
        if d in reb:
            pv = sum(q * m.open.loc[d, a] for a, q in shares.items()) if shares else value
            shares = {a: wt * pv / m.open.loc[d, a] for a, wt in w.items()}
        expected[d] = sum(q * m.close.loc[d, a] for a, q in shares.items())
    np.testing.assert_allclose(res["equity"].to_numpy(), pd.Series(expected).to_numpy(), rtol=1e-10)


def test_evaluate_activity_and_turnover_definition(m):
    res = run(m, research.fixed({"AAA": 1.0}), once=True, cost=0.0)
    out = research.evaluate(res, m)
    years = (IDX[-1] - (IDX[30] - pd.Timedelta(days=1))).days / 365.25
    assert out["orders_per_year"] == pytest.approx(1 / years)
    assert out["turnover_per_year"] == pytest.approx(1.0 / 2 / years)      # one-way: buy only, halved
    assert out["avg_invested"] == pytest.approx(1.0, abs=1e-6)


def test_trend_rule_in_engine_holds_only_trending_asset():
    idx = calendar("2018-01-01", 400)
    mk = market({"UP": pd.Series(np.linspace(50, 150, 400), index=idx),
                 "DOWN": pd.Series(np.linspace(150, 50, 400), index=idx)})
    rule = functools.partial(research.trend, assets=("UP", "DOWN"))
    res = research.run_monthly(mk, rule, start=idx[260], end=idx[-1], cost=0.0)
    held = {o[1] for o in res["orders"] if o[2] == "BUY"}
    assert held == {"UP", rd.CASH}                                  # half in UP, half in BIL


def test_stress_year_return():
    idx = pd.DatetimeIndex(["2019-12-31", "2020-03-31", "2020-12-31"])
    res = {"equity": pd.Series([100.0, 80.0, 110.0], index=idx)}
    out = research.stress(res, 2020)
    assert out["return"] == pytest.approx(0.10) and out["max_dd"] == pytest.approx(-0.20)


def test_ohlcv_helper_is_consistent():
    df = ohlcv(random_walk(IDX[:10], seed=9))
    assert (df["High"] >= df[["Open", "Close"]].max(axis=1)).all()
    assert (df["Low"] <= df[["Open", "Close"]].min(axis=1)).all()
