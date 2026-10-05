"""Data layer: cleaning, canonical hashing, manifest verification, cache round trip."""
import pandas as pd
import pytest
from synthetic import calendar, ohlcv, random_walk

import research_data as rd


def frame(seed=0):
    return ohlcv(random_walk(calendar("2020-01-01", 50), seed=seed))


def test_clean_drops_duplicates_timezone_and_sorts():
    df = frame()
    idx = df.index.tz_localize("America/New_York") + pd.Timedelta(hours=9, minutes=30)
    messy = pd.concat([df.set_axis(idx), df.iloc[[5]].set_axis(idx[[5]])]).iloc[::-1]
    out = rd.clean(messy)
    assert out.index.tz is None and out.index.is_monotonic_increasing and not out.index.duplicated().any()
    assert (out.index == df.index).all()


def test_hash_is_deterministic_and_detects_changes():
    a, b = frame(), frame()
    assert rd.sha256(a) == rd.sha256(b)
    b.iloc[10, b.columns.get_loc("Close")] += 1e-3
    assert rd.sha256(a) != rd.sha256(b)
    c = frame()
    c.iloc[10, c.columns.get_loc("Close")] += 1e-9                       # below the 6-decimal canonical precision
    assert rd.sha256(a) == rd.sha256(c)


def test_manifest_and_verification():
    raw = {"AAA": frame(1), "BBB": frame(2)}
    man = rd.build_manifest(raw, "2026-10-02T12:00:00Z", pd.Timestamp("2026-10-02"))
    assert man["end_date"] == "2026-10-02" and man["downloaded_at_utc"] == "2026-10-02T12:00:00Z"
    assert set(man["series"]) == {"AAA", "BBB"} and man["series"]["AAA"]["rows"] == 50
    assert rd.verify(raw, man) == []
    changed = {"AAA": frame(1), "BBB": frame(3)}
    assert rd.verify(changed, man) == ["BBB"]


def test_cache_round_trip_preserves_hashes(tmp_path):
    raw = {"AAA": frame(4), rd.RATE_TICKER: frame(5)}
    rd.save_cache(raw, str(tmp_path))
    back = rd.load_cache(["AAA"], str(tmp_path))
    assert {t: rd.sha256(df) for t, df in back.items()} == {t: rd.sha256(df) for t, df in raw.items()}


def test_missing_cache_is_an_explicit_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        rd.load_cache(["ZZZ"], str(tmp_path))
