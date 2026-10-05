"""Indicators against independent implementations, plus causality."""
import types

import numpy as np
import pandas as pd
import pytest
from synthetic import calendar, ohlcv, random_walk

import indicators

CFG = types.SimpleNamespace(MA_TYPE="EMA", FAST_MA=5, SLOW_MA=12, TREND_MA=30, RSI_PERIOD=14, MACD_FAST=12,
                            MACD_SLOW=26, MACD_SIGNAL=9, VOLUME_MA=20, ADX_PERIOD=14, ATR_PERIOD=14)


def test_sma_matches_manual_mean_and_warmup():
    s = pd.Series(np.arange(1.0, 11.0))
    out = indicators.sma(s, 3)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx(2.0) and out.iloc[9] == pytest.approx(9.0)


def test_ema_matches_recursive_definition():
    s = random_walk(calendar(periods=60), seed=3)
    span = 10
    alpha = 2 / (span + 1)
    manual = [s.iloc[0]]
    for x in s.iloc[1:]:
        manual.append(alpha * x + (1 - alpha) * manual[-1])
    out = indicators.ema(s, span)
    assert out.iloc[: span - 1].isna().all()
    np.testing.assert_allclose(out.iloc[span - 1:].to_numpy(), np.array(manual[span - 1:]), rtol=1e-12)


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1.0, 60.0))
    down = up[::-1].reset_index(drop=True)
    assert indicators.rsi(up, 14).dropna().eq(100).all()
    assert indicators.rsi(down, 14).dropna().eq(0).all()
    noisy = indicators.rsi(random_walk(calendar(periods=300), seed=4), 14).dropna()
    assert noisy.between(0, 100).all()


def test_atr_of_constant_range_bars():
    idx = calendar(periods=50)
    df = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1.0}, index=idx)
    assert indicators.atr(df, 14).dropna().round(10).eq(2.0).all()


def test_crossover_age_and_date():
    idx = calendar(periods=80)
    close = pd.Series(np.r_[np.linspace(120, 100, 40), np.linspace(100, 130, 40)], index=idx)
    df = indicators.add_indicators(ohlcv(close), CFG)
    cross = df.index[(df["cross_age"] == 0).to_numpy()]
    assert len(cross) >= 1
    first = cross[0]
    assert df.loc[first, "ma_fast"] > df.loc[first, "ma_slow"]
    prev = df.index[df.index.get_loc(first) - 1]
    assert df.loc[prev, "ma_fast"] <= df.loc[prev, "ma_slow"]
    later = df.index[df.index.get_loc(first) + 3]
    assert df.loc[later, "cross_age"] == 3 and df.loc[later, "last_cross_up"] == first


def test_all_indicators_are_causal():
    """Changing prices after day T must not change any indicator value up to T."""
    close = random_walk(calendar(periods=300), seed=5)
    base = indicators.add_indicators(ohlcv(close), CFG)
    changed = close.copy()
    changed.iloc[200:] *= np.linspace(0.5, 1.8, 100)
    other = indicators.add_indicators(ohlcv(changed), CFG)
    cols = ["ma_fast", "ma_slow", "sma_trend", "rsi", "macd", "macd_signal", "vol_avg", "adx", "atr",
            "cross_age"]
    pd.testing.assert_frame_equal(base[cols].iloc[:200], other[cols].iloc[:200])
