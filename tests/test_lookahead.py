"""Look-ahead protection: information after time t must not change anything decided at t.

Pattern: build the same synthetic market twice, change prices only after a cut-off (or
only on the trading day itself), and check that signals, orders and equity up to the
cut-off are identical.
"""
import functools

import numpy as np
import pandas as pd
import pytest
from synthetic import calendar, random_walk, raw_market

import allocation
import bot
import regimes
import research
import research_data as rd

IDX = calendar("2017-01-02", 700)
CUT = IDX[450]


def closes(seed=0):
    return {"AAA": random_walk(IDX, 0.0004, 0.011, seed=seed), "BBB": random_walk(IDX, 0.0001, 0.009, seed=seed + 1),
            "CCC": random_walk(IDX, -0.0001, 0.013, seed=seed + 2)}


def shocked(series: dict, after: pd.Timestamp, factor=None) -> dict:
    """Copy with every price strictly after `after` multiplied by a random path."""
    rng = np.random.default_rng(99)
    out = {}
    for t, s in series.items():
        s = s.copy()
        mask = s.index > after
        s[mask] = s[mask] * (factor if factor is not None else np.exp(np.cumsum(rng.normal(0, 0.05, mask.sum()))))
        out[t] = s
    return out


def build(series):
    return rd.Market(raw_market(series), calendar_symbol="AAA")


RULE = functools.partial(research.trend, assets=("AAA", "BBB", "CCC"))


def test_signal_at_t_ignores_prices_after_t():
    base, other = build(closes()), build(shocked(closes(), CUT))
    for s in (IDX[300], IDX[400], CUT):
        a = allocation.trend_signals(base, s, ["AAA", "BBB", "CCC"], 10)
        b = allocation.trend_signals(other, s, ["AAA", "BBB", "CCC"], 10)
        assert a == b
    s_me = base.month_end_idx[base.month_end_idx <= CUT][-1]
    assert allocation.trend_signals(base, s_me, ["AAA"], 10) == allocation.trend_signals(other, s_me, ["AAA"], 10)


@pytest.mark.parametrize("engine", ["monthly", "split"])
def test_engine_history_unchanged_by_future_prices(engine):
    base, other = build(closes()), build(shocked(closes(), CUT))
    kw = {"start": IDX[260], "end": IDX[-1], "buffer": 0.005, "min_order": 25.0}
    run = (lambda mk: research.run_monthly(mk, RULE, **kw)) if engine == "monthly" else \
        (lambda mk: research.run_split(mk, RULE, offsets=(0, 5, 10, 15), **kw))
    a, b = run(base), run(other)
    pd.testing.assert_series_equal(a["equity"].loc[:CUT], b["equity"].loc[:CUT])
    assert [o for o in a["orders"] if o[0] <= CUT] == [o for o in b["orders"] if o[0] <= CUT]
    assert not a["equity"].loc[CUT:].equals(b["equity"].loc[CUT:])        # the shock did change the future


def test_rebalance_decision_ignores_the_same_days_close():
    """Orders on rebalance day d depend on closes up to d-1 and d's open, not d's close."""
    base = build(closes())
    res = research.run_monthly(base, RULE, start=IDX[260], end=IDX[-1])
    d = res["rebalances"][5]
    series = closes()
    for t in series:
        series[t] = series[t].copy()
        series[t].loc[d] *= 1.3                                          # only day d's close moves
    raw = raw_market(series)
    for t in ("AAA", "BBB", "CCC"):
        raw[t].loc[d, "Open"] = raw_market(closes())[t].loc[d, "Open"]  # keep d's open unchanged
    other = rd.Market(raw, calendar_symbol="AAA")
    res2 = research.run_monthly(other, RULE, start=IDX[260], end=IDX[-1])
    assert [o for o in res["orders"] if o[0] == d] == [o for o in res2["orders"] if o[0] == d]


def test_daily_setup_j_engine_is_causal():
    settings = {**research.SETUP_J, "SYMBOLS": ["AAA", "BBB", "CCC"]}
    base, other = build(closes(5)), build(shocked(closes(5), CUT))
    a = research.run_daily(base, settings, start=IDX[260], end=IDX[-1])
    b = research.run_daily(other, settings, start=IDX[260], end=IDX[-1])
    assert len(a["orders"]) > 0
    pd.testing.assert_series_equal(a["equity"].loc[:CUT], b["equity"].loc[:CUT])
    assert [o for o in a["orders"] if o[0] <= CUT] == [o for o in b["orders"] if o[0] <= CUT]


def test_regime_labels_use_only_previous_close():
    """The label for day t must not react to day t's own close (or anything later)."""
    spy = random_walk(IDX, 0.0003, 0.012, seed=7)
    for fn in (regimes.trend_regime, regimes.volatility_regime):
        base = fn(spy)
        for t in IDX[400:680:7]:                                         # 40 different days
            for factor in (3.0, 0.3):                                    # extreme move on day t only
                changed = spy.copy()
                changed.loc[t:] *= factor
                assert fn(changed).loc[t] == base.loc[t], (fn.__name__, t, factor)
        changed = spy.copy()
        changed.loc[IDX[500]:] *= 0.5
        assert not fn(changed).loc[IDX[500]:].equals(base.loc[IDX[500]:])   # but later labels do react


def test_volatility_median_is_expanding_not_full_sample():
    spy = random_walk(IDX, 0.0, 0.01, seed=8)
    labels = regimes.volatility_regime(spy, window=63, min_history=252)
    assert labels.iloc[: 63 + 252 - 1].isna().all()                     # warm-up: vol needs 63, median 252
    assert labels.dropna().isin(["high", "low"]).all()


def test_cash_returns_are_causal():
    raw = raw_market(closes())
    base = rd.cash_returns(IDX, raw[rd.RATE_TICKER]["Close"], raw["BIL"]["Close"])
    rate = raw[rd.RATE_TICKER]["Close"].copy()
    rate.loc[CUT:] = 10.0
    bil = raw["BIL"]["Close"].copy()
    bil.loc[CUT:] *= 1.1
    other = rd.cash_returns(IDX, rate, bil)
    pd.testing.assert_series_equal(base.loc[:IDX[449]], other.loc[:IDX[449]])


def test_live_signal_bars_drop_todays_unfinished_bar():
    today = pd.Timestamp.now(tz="America/New_York").normalize()
    idx = pd.DatetimeIndex([today - pd.Timedelta(days=2), today - pd.Timedelta(days=1), today])
    df = pd.DataFrame({"Close": [1.0, 2.0, 3.0]}, index=idx)
    assert bot.signal_bars(df, market_open=True).index[-1] == idx[-2]      # today's bar still forming
    assert bot.signal_bars(df, market_open=False).index[-1] == idx[-1]     # after the close it is complete
