"""Live allocation mode (bot.run_allocation) on a fake Alpaca client - no network, no real files.

These tests exercise the live code path as it is; they never modify bot.py or config.py.
"""
import types

import numpy as np
import pandas as pd
import pytest
from synthetic import calendar

import bot
import config

ASSETS = list(config.ALLOCATION_ASSETS)
TRADABLE = [*ASSETS, config.CASH_SYMBOL]
IDX = calendar("2025-01-01", 300)


def synthetic_closes(trending=("SPY", "EFA", "DBC")) -> tuple[pd.DataFrame, dict]:
    cols = {}
    for i, sym in enumerate(TRADABLE):
        if sym == config.CASH_SYMBOL:
            cols[sym] = np.linspace(90, 91.5, len(IDX))
        elif sym in trending:
            cols[sym] = np.linspace(50 + i, 100 + i, len(IDX))           # rising: above its average
        else:
            cols[sym] = np.linspace(100 + i, 60 + i, len(IDX))           # falling: below its average
    df = pd.DataFrame(cols, index=IDX)
    return df, {s: float(df[s].iloc[-1]) for s in TRADABLE}


class FakeClient:
    def __init__(self, latest, cash=100_000.0, positions=None, nmbp=None, open_orders=()):
        self.latest, self.cash, self.positions = latest, cash, dict(positions or {})
        self.nmbp, self.open_orders, self.submitted = nmbp, list(open_orders), []

    def price(self, s):
        return self.latest.get(s, 100.0)

    def get_account(self):
        eq = self.cash + sum(q * self.price(s) for s, q in self.positions.items())
        nmbp = self.nmbp if self.nmbp is not None else self.cash
        return types.SimpleNamespace(cash=str(self.cash), equity=str(eq), non_marginable_buying_power=str(nmbp))

    def get_all_positions(self):
        return [types.SimpleNamespace(symbol=s, qty=str(q), current_price=str(self.price(s)))
                for s, q in self.positions.items() if q]

    def get_orders(self, filter=None):
        return self.open_orders

    def get_asset(self, s):
        return types.SimpleNamespace(fractionable=True)

    def submit_order(self, req):
        qty = float(req.qty) * (1 if "BUY" in str(req.side) else -1)
        self.submitted.append((req.symbol, qty))
        self.positions[req.symbol] = self.positions.get(req.symbol, 0.0) + qty   # instant fill
        self.cash -= qty * self.price(req.symbol)
        return types.SimpleNamespace(id=f"fake-{len(self.submitted)}")


OPEN = types.SimpleNamespace(is_open=True, next_open=None)
CLOSED = types.SimpleNamespace(is_open=False, next_open="tomorrow 09:30")


@pytest.fixture
def live(monkeypatch, tmp_path):
    """Isolate bot state/log files and replace market data + calendar with synthetic ones."""
    closes, latest = synthetic_closes()
    day = {"n": 3}
    monkeypatch.setattr(bot, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(bot, "ALLOC_CSV_PATH", str(tmp_path / "alloc.csv"))
    monkeypatch.setattr(bot, "fetch_closes", lambda symbols, market_open: (closes, latest))
    monkeypatch.setattr(bot, "trading_day_of_month", lambda client, d: day["n"])
    monkeypatch.setattr(config, "STRATEGY", "trend_allocation")
    return types.SimpleNamespace(latest=latest, day=day, tmp=tmp_path)


def ledger():
    return bot.load_state()["allocation"]["tranches"]


def weights_of(client):
    eq = float(client.get_account().equity)
    return {s: q * client.price(s) / eq for s, q in client.positions.items() if q}


def test_first_run_invests_all_parts_with_bil_fallback_and_buffer(live):
    c = FakeClient(live.latest)
    rows = bot.run_allocation(c, OPEN, dry_run=False)
    w = weights_of(c)
    n = len(ASSETS)
    for s in ("SPY", "EFA", "DBC"):                                      # in uptrend: 1/7 each (minus buffer)
        assert w[s] == pytest.approx((1 / n) * (1 - config.CASH_BUFFER_PCT / 100), abs=2e-4)
    assert w[config.CASH_SYMBOL] == pytest.approx((1 - 3 / n) * (1 - config.CASH_BUFFER_PCT / 100), abs=2e-4)
    assert not set(w) & {"IEF", "TLT", "GLD", "VNQ"}                     # below average: nothing
    assert c.cash == pytest.approx(100_000 * config.CASH_BUFFER_PCT / 100, rel=0.01)
    assert rows[0]["tranches_due"] == "all (initial)"
    # trading day 3: part 1 (day 1) counts as done, parts 2-4 keep their own days this month
    assert [t["last_rebalance"] is not None for t in ledger()] == [True, False, False, False]


def test_schedule_and_catch_up(live):
    c = FakeClient(live.latest)
    bot.run_allocation(c, OPEN, dry_run=False)
    live.day["n"] = 4
    assert bot.run_allocation(c, OPEN, dry_run=False)[0]["tranches_due"] == "none"
    live.day["n"] = 6
    assert bot.run_allocation(c, OPEN, dry_run=False)[0]["tranches_due"] == "2"
    live.day["n"] = 17                                                   # days 11 and 16 were missed
    assert bot.run_allocation(c, OPEN, dry_run=False)[0]["tranches_due"] == "3,4"
    assert all(t["last_rebalance"] is not None for t in ledger())


def test_never_uses_margin(live):
    c = FakeClient(live.latest, nmbp=20_000.0)                          # only $20k spendable
    rows = bot.run_allocation(c, OPEN, dry_run=False)
    bought = sum(q * c.price(s) for s, q in c.submitted if q > 0)
    assert bought <= 20_000 + 1e-6
    assert "buys scaled" in rows[0]["reason"]


def test_sells_first_and_positions_outside_strategy_are_closed(live):
    c = FakeClient(live.latest, cash=50_000.0, positions={"XLK": 100.0, "IEF": 50.0})
    bot.run_allocation(c, OPEN, dry_run=False)
    first_buy = next(i for i, (_, q) in enumerate(c.submitted) if q > 0)
    assert all(q < 0 for _, q in c.submitted[:first_buy])                # all sells before any buy
    assert c.positions.get("XLK", 0) == 0 and c.positions.get("IEF", 0) == 0


def test_dry_run_and_closed_market_leave_state_untouched(live):
    c = FakeClient(live.latest)
    rows = bot.run_allocation(c, OPEN, dry_run=True)
    assert c.submitted == [] and not (live.tmp / "state.json").exists()
    assert rows[0]["action"].startswith("DRY RUN")
    rows = bot.run_allocation(c, CLOSED, dry_run=False)
    assert c.submitted == [] and not (live.tmp / "state.json").exists()
    assert "blocked" in rows[0]["action"]


def test_pending_orders_block_trading(live):
    c = FakeClient(live.latest, open_orders=[types.SimpleNamespace(symbol="SPY")])
    rows = bot.run_allocation(c, OPEN, dry_run=False)
    assert c.submitted == [] and "pending" in rows[0]["action"]


def test_duplicate_bars_are_dropped(monkeypatch):
    idx = pd.DatetimeIndex(["2026-10-01", "2026-10-02", "2026-10-02"], tz="America/New_York")
    df = pd.DataFrame({"Open": [1.0, 2.0, 2.5], "High": [1.0, 2.0, 2.5], "Low": [1.0, 2.0, 2.5],
                       "Close": [1.0, 2.0, 2.5], "Volume": [1.0, 1.0, 1.0]}, index=idx)
    monkeypatch.setattr(bot.yf, "Ticker", lambda s: types.SimpleNamespace(history=lambda **kw: df))
    closes, latest = bot.fetch_closes(["AAA"], market_open=False)
    assert not closes.index.duplicated().any() and latest["AAA"] == 2.5
    assert not bot.fetch_bars("AAA").index.duplicated().any()


def test_missing_data_is_logged_and_goes_to_bil(live, monkeypatch, caplog):
    closes, latest = synthetic_closes()
    closes.loc[closes.index[-1], "SPY"] = np.nan                         # no close for SPY on the signal date
    monkeypatch.setattr(bot, "fetch_closes", lambda symbols, market_open: (closes, latest))
    c = FakeClient(latest)
    with caplog.at_level("WARNING", logger="bot"):
        rows = bot.run_allocation(c, OPEN, dry_run=True)
    assert any("SPY: no usable trend signal" in r.message for r in caplog.records)
    spy = next(r for r in rows if r["symbol"] == "SPY")
    assert spy["target_weight"] == 0 and "SPY: missing data" in rows[0]["reason"]


def test_reconcile_scales_ledger_to_real_positions():
    led = {"tranches": [{"cash": 10.0, "shares": {"SPY": 2.0}}, {"cash": 10.0, "shares": {"SPY": 6.0}}]}
    notes = bot.reconcile_ledger(led, {"SPY": 4.0}, 30.0, ["SPY"])
    assert [t["shares"]["SPY"] for t in led["tranches"]] == [1.0, 3.0]     # scaled by 4/8
    assert sum(t["cash"] for t in led["tranches"]) == pytest.approx(30.0)
    assert notes and "SPY" in notes[0]


def test_round_qty_toward_zero():
    assert bot.round_qty(18.46879, True) == pytest.approx(18.4687)
    assert bot.round_qty(-3.99999, True) == pytest.approx(-3.9999)
    assert bot.round_qty(7.9, False) == 7.0
