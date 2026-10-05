"""Shared test setup: project imports and a hard network block.

Every test runs with network access disabled: any socket connection or yfinance call
raises immediately, so a test that accidentally reaches Yahoo Finance or Alpaca fails
instead of silently using live data. All data in the tests is synthetic.
"""
from __future__ import annotations

import os
import socket
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)


class NetworkBlocked(RuntimeError):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("network access is disabled in tests (use synthetic data)")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    import yfinance

    monkeypatch.setattr(yfinance, "Ticker", _blocked)
    monkeypatch.setattr(yfinance, "download", _blocked)
    yield
