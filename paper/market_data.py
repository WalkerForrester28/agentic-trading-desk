#!/usr/bin/env python3
"""
market_data.py
==============
Public market data for the paper desk (replaces the Robinhood MCP calls).

  - Daily bars from Yahoo Finance's chart endpoint (no key).
  - 10Y-2Y Treasury spread from FRED's public CSV (series T10Y2Y, no key).

stdlib only. Both hosts must be on the environment's network allowlist.
Every fetch failure raises DataError: the paper desk follows the Mandate's
"data does not reconcile -> do not trade" rule, so callers must not guess.
"""
from __future__ import annotations

import csv
import io
import json
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (agentic-trading-desk paper desk)"}


class DataError(RuntimeError):
    pass


def _get(url: str, retries: int = 3) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read()
        except Exception as e:  # network, HTTP 429/5xx
            last = e
            time.sleep(2 ** attempt)
    raise DataError(f"fetch failed: {url}: {last}")


def daily_bars(symbol: str, range_: str = "2y") -> dict:
    """Returns {symbol, dates:[YYYY-MM-DD], close:[float], last_price, market_state}.

    Bars with a null close (halts, Yahoo gaps) are dropped. During regular hours
    the last bar is today's live bar, so close[-1] is the current price (the paper
    equivalent of the skill's Step 0).
    """
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={range_}&interval=1d&includePrePost=false")
    try:
        res = json.loads(_get(url))["chart"]["result"][0]
        ts = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise DataError(f"{symbol}: unexpected Yahoo payload ({e})") from None
    dates, out = [], []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        dates.append(datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d"))
        out.append(round(float(c), 4))
    if len(out) < 30:
        raise DataError(f"{symbol}: only {len(out)} bars returned")
    meta = res.get("meta", {})
    return {
        "symbol": symbol,
        "dates": dates,
        "close": out,
        "last_price": float(meta.get("regularMarketPrice") or out[-1]),
    }


def yield_spread(n: int = 120) -> list[float]:
    """Last n daily observations of the 10Y-2Y spread (percentage points)."""
    raw = _get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=T10Y2Y").decode()
    vals = []
    for row in csv.reader(io.StringIO(raw)):
        if len(row) == 2:
            try:
                vals.append(float(row[1]))
            except ValueError:
                continue  # header or "." (holiday)
    if not vals:
        raise DataError("FRED T10Y2Y: no observations")
    return vals[-n:]


def market_session(now: datetime | None = None) -> dict:
    """Regular-hours window Mon-Fri 09:30-15:58 ET (same as the Mandate guard).
    Holidays are not known here; the stale-bar check in paper_trade.py covers them."""
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    minutes = now.hour * 60 + now.minute
    open_ = now.weekday() < 5 and 9 * 60 + 30 <= minutes <= 15 * 60 + 58
    return {"now_et": now.strftime("%Y-%m-%d %H:%M %Z"), "regular_hours": open_,
            "date": now.strftime("%Y-%m-%d"), "weekday": now.weekday()}
