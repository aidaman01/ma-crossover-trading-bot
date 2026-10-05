"""Research data layer: download with a fixed end date, local cache, checksum manifest, Market.

    python research.py              # download (end date END), cache to data/cache/, write manifest
    python research.py --offline    # reuse data/cache/ and verify it against the manifest

Source: Yahoo Finance through yfinance with auto_adjust=True (OHLC adjusted for splits and
dividends, a total-return proxy) and the 13-week T-bill discount rate (^IRX).

Reproducibility: docs/research/data_manifest.json records the source, download time,
yfinance version, date range, row count and a SHA-256 hash of every series in a canonical
CSV form (dates as YYYY-MM-DD, values rounded to 6 decimals). Two researchers with equal
hashes used identical input data. Yahoo revises adjusted history after every dividend, so a
download made on a different day can legitimately produce different hashes; the manifest
makes that visible instead of silently changing results. Raw data is never committed.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from datetime import UTC, datetime

import pandas as pd

import allocation

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "data", "cache")
MANIFEST_PATH = os.path.join(HERE, "docs", "research", "data_manifest.json")

END = pd.Timestamp("2026-10-02")          # last close used by the research (fixed)
HISTORY_START = "1990-01-01"
SOURCE = "Yahoo Finance via yfinance, auto_adjust=True (split- and dividend-adjusted OHLC)"
RATE_TICKER = "^IRX"                       # 13-week T-bill discount rate, percent
CASH = "CASH"                              # research cash asset: BIL, spliced with T-bills before it
OHLCV = ["Open", "High", "Low", "Close", "Volume"]


# --------------------------------------------------------------------------- download / cache
def download(tickers: list[str], end: pd.Timestamp = END) -> dict[str, pd.DataFrame]:
    """Daily bars for every ticker (plus ^IRX) from HISTORY_START through ``end`` inclusive."""
    import yfinance as yf

    stop = (end + pd.Timedelta(days=1)).strftime("%Y-%m-%d")    # yfinance's end is exclusive
    raw = {}
    for t in list(tickers) + [RATE_TICKER]:
        df = yf.Ticker(t).history(start=HISTORY_START, end=stop, interval="1d", auto_adjust=True)
        if df.empty:
            raise RuntimeError(f"yfinance returned no data for {t}")
        raw[t] = clean(df)
    return raw


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Keep OHLCV, make the index tz-naive dates, drop duplicate dates (keep the last) and sort."""
    df = df[[c for c in OHLCV if c in df.columns]].copy()
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def canonical_csv(df: pd.DataFrame) -> bytes:
    """Deterministic CSV bytes used for hashing (and for the cache)."""
    out = df.copy()
    out.index = pd.DatetimeIndex(out.index).strftime("%Y-%m-%d")
    out.index.name = "Date"
    buf = io.StringIO()
    out.round(6).to_csv(buf, lineterminator="\n", float_format="%.6f")
    return buf.getvalue().encode()


def sha256(df: pd.DataFrame) -> str:
    return hashlib.sha256(canonical_csv(df)).hexdigest()


def save_cache(raw: dict[str, pd.DataFrame], cache_dir: str = CACHE_DIR) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    for t, df in raw.items():
        with open(os.path.join(cache_dir, f"{t.replace('^', '_')}.csv"), "wb") as f:
            f.write(canonical_csv(df))


def load_cache(tickers: list[str], cache_dir: str = CACHE_DIR) -> dict[str, pd.DataFrame]:
    raw = {}
    for t in list(tickers) + [RATE_TICKER]:
        path = os.path.join(cache_dir, f"{t.replace('^', '_')}.csv")
        if not os.path.exists(path):
            raise FileNotFoundError(f"{path} missing: run `python research.py` once online")
        raw[t] = pd.read_csv(path, index_col="Date", parse_dates=True)
    return raw


def build_manifest(raw: dict[str, pd.DataFrame], downloaded_at: str, end: pd.Timestamp = END) -> dict:
    try:
        import yfinance
        yf_version = yfinance.__version__
    except ImportError:          # pragma: no cover - only when hashing offline without yfinance
        yf_version = "unknown"
    series = {t: {"first": df.index[0].strftime("%Y-%m-%d"), "last": df.index[-1].strftime("%Y-%m-%d"),
                  "rows": len(df), "sha256": sha256(df)} for t, df in sorted(raw.items())}
    combined = hashlib.sha256("".join(v["sha256"] for v in series.values()).encode()).hexdigest()
    return {
        "source": SOURCE,
        "rate_series": f"{RATE_TICKER} (13-week T-bill discount rate, %); cash = BIL total return from its "
                       "first trading day, T-bill rate accrued per calendar day / 360 before that",
        "downloaded_at_utc": downloaded_at,
        "yfinance_version": yf_version,
        "end_date": end.strftime("%Y-%m-%d"),
        "hash_method": "SHA-256 of canonical CSV (Date as YYYY-MM-DD, OHLCV rounded to 6 decimals)",
        "combined_sha256": combined,
        "series": series,
    }


def write_manifest(manifest: dict, path: str = MANIFEST_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


def verify(raw: dict[str, pd.DataFrame], manifest: dict) -> list[str]:
    """Series whose hash differs from the manifest (empty list = identical inputs)."""
    expected = manifest.get("series", {})
    return [t for t, df in raw.items() if expected.get(t, {}).get("sha256") != sha256(df)]


def get_data(tickers: list[str], offline: bool = False, end: pd.Timestamp = END) -> tuple[dict, dict]:
    """(raw data, manifest). Online: download, cache and write a new manifest. Offline: load
    the cache and check it against the existing manifest (mismatches are reported, not hidden)."""
    if offline:
        raw = load_cache(tickers)
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            manifest = json.load(f)
        bad = verify(raw, manifest)
        manifest["verification"] = "all hashes match" if not bad else f"MISMATCH: {', '.join(bad)}"
        return raw, manifest
    raw = download(tickers, end)
    raw = {t: pd.read_csv(io.BytesIO(canonical_csv(df)), index_col="Date", parse_dates=True)
           for t, df in raw.items()}          # identical to what the cache will contain
    save_cache(raw)
    manifest = build_manifest(raw, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), end)
    write_manifest(manifest)
    return raw, manifest


# --------------------------------------------------------------------------- market
def cash_returns(calendar: pd.DatetimeIndex, rate: pd.Series, bil_close: pd.Series | None) -> pd.Series:
    """Daily cash return: BIL's close-to-close return once BIL trades, otherwise the T-bill
    discount rate of the PREVIOUS day accrued over the calendar days since then (/360)."""
    rate = rate.clip(lower=0).reindex(calendar).ffill()
    days = calendar.to_series().diff().dt.days
    tbill = rate.shift(1) / 100 * days / 360
    if bil_close is None:
        return tbill.fillna(0.0)
    bil = bil_close.reindex(calendar).pct_change(fill_method=None)
    return bil.where(bil.notna(), tbill).fillna(0.0)


class Market(allocation.Prices):
    """Aligned daily prices on the NYSE calendar (SPY's trading days) plus a tradable cash asset.

    Built only from data passed in, so tests can construct it offline. ``close``/``open``
    contain every ticker plus CASH: CASH closes at the cash index; its open equals the
    previous close moved by BIL's overnight return (no overnight move before BIL existed).
    Month-end / moving-average helpers come from allocation.Prices, shared with the live bot.
    """

    def __init__(self, raw: dict[str, pd.DataFrame], calendar_symbol: str = "SPY", ffill_limit: int = 5):
        assets = {t: d for t, d in raw.items() if t != RATE_TICKER}
        self.raw = assets
        calendar = pd.DatetimeIndex(assets[calendar_symbol].index)
        close = pd.DataFrame({t: d["Close"] for t, d in assets.items()}).reindex(calendar).ffill(limit=ffill_limit)
        opens = pd.DataFrame({t: d["Open"] for t, d in assets.items()}).reindex(calendar).ffill(limit=ffill_limit)
        rate = raw[RATE_TICKER]["Close"] if RATE_TICKER in raw else pd.Series(0.0, index=calendar)
        bil = assets["BIL"]["Close"] if "BIL" in assets else None
        self.cash_ret = cash_returns(calendar, rate, bil)
        self.cash_idx = 100 * (1 + self.cash_ret).cumprod()
        overnight = (opens["BIL"] / close["BIL"].shift(1)) if "BIL" in assets else pd.Series(1.0, index=calendar)
        close[CASH] = self.cash_idx
        opens[CASH] = (self.cash_idx.shift(1) * overnight.fillna(1.0)).fillna(self.cash_idx)
        super().__init__(close)
        self.open = opens
        self.rets = self.close.pct_change(fill_method=None)
        self.bil_start = assets["BIL"].index[0] if "BIL" in assets else None
