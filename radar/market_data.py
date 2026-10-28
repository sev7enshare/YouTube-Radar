from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .db import iso
from .providers import parse_timestamp


log = logging.getLogger(__name__)
API = "https://fapi.binance.com/fapi/v1/klines"
OKX_API = "https://www.okx.com/api/v5/market/history-candles"
ASSETS = {"BTC": "BTCUSDT", "比特币": "BTCUSDT", "比特幣": "BTCUSDT", "BITCOIN": "BTCUSDT",
          "ETH": "ETHUSDT", "以太坊": "ETHUSDT", "ETHEREUM": "ETHUSDT",
          "SOL": "SOLUSDT", "SOLANA": "SOLUSDT", "BNB": "BNBUSDT", "XRP": "XRPUSDT"}


def symbol_for(asset: str) -> str | None:
    return ASSETS.get(asset.strip().upper())


def strict_price(value: str) -> Decimal | None:
    """Accept only unambiguous absolute numeric prices; never infer Fib ratios or shorthand."""
    cleaned = value.strip().replace(",", "")
    match = re.fullmatch(r"(\d{3,8}(?:\.\d+)?)\s*(?:美元|USDT|USD)?", cleaned)
    return Decimal(match.group(1)) if match else None


def fetch_hourly_klines(symbol: str, start: datetime, end: datetime) -> list[list[Any]]:
    """Read public USD-M futures candles; no account credentials or trade permission."""
    params = urllib.parse.urlencode({"symbol": symbol, "interval": "1h",
                                     "startTime": int(start.timestamp() * 1000),
                                     "endTime": int(end.timestamp() * 1000), "limit": 1000})
    request = urllib.request.Request(API + "?" + params, headers={"User-Agent": "YouTubeRadar/0.2"})
    with urllib.request.urlopen(request, timeout=45) as response:
        data = json.load(response)
    if not isinstance(data, list):
        raise ValueError(f"Unexpected Binance candle response for {symbol}")
    return data


def fetch_okx_hourly_klines(symbol: str, start: datetime, end: datetime) -> list[list[Any]]:
    """Public OKX USDT perpetual candles, returned in Binance-compatible OHLC positions."""
    instrument = symbol.removesuffix("USDT") + "-USDT-SWAP"
    cursor: str | None = None
    found: dict[int, list[Any]] = {}
    for _ in range(10):
        params = {"instId": instrument, "bar": "1H", "limit": "100"}
        if cursor:
            params["after"] = cursor
        url = OKX_API + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": "YouTubeRadar/0.2"})
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.load(response)
        if result.get("code") != "0" or not isinstance(result.get("data"), list):
            raise ValueError(f"Unexpected OKX candle response for {instrument}")
        rows = result["data"]
        if not rows:
            break
        for row in rows:
            if len(row) >= 9 and row[8] == "1":
                at = int(row[0])
                if start.timestamp() * 1000 <= at < end.timestamp() * 1000:
                    found[at] = row[:5]
        oldest = min(int(row[0]) for row in rows)
        if oldest <= start.timestamp() * 1000 or len(rows) < 100:
            break
        cursor = str(oldest)
        time.sleep(0.12)
    return [found[key] for key in sorted(found)]


def record_market_days(db: sqlite3.Connection, now: datetime, max_days: int = 30) -> int:
    """Store completed 24-hour windows after publication; no correctness verdict."""
    rows = db.execute("""SELECT v.id, v.published_at, v.analysis_json FROM videos v
        JOIN channels c ON c.id=v.channel_ref WHERE c.mode='market' AND v.status='analyzed'
        AND v.analysis_json IS NOT NULL""").fetchall()
    recorded = 0
    binance_blocked = False
    for row in rows:
        view = (json.loads(row["analysis_json"]).get("market_view") or {})
        if view.get("version") != 2:
            continue
        symbols = {symbol_for(str(item.get("asset") or "")) for item in
                   (view.get("positions") or []) + (view.get("levels") or [])}
        symbols.discard(None)
        published = parse_timestamp(row["published_at"])
        completed = min(max_days, int((now - published).total_seconds() // 86400))
        if completed < 1:
            continue
        for symbol in sorted(symbols):
            existing = db.execute("SELECT venue FROM market_days WHERE video_id=? LIMIT 1",
                                  (row["id"],)).fetchone()
            venue = existing["venue"] if existing else ("OKX" if binance_blocked else "Binance")
            missing = [day for day in range(1, completed + 1) if not db.execute(
                "SELECT 1 FROM market_days WHERE video_id=? AND symbol=? AND day_number=?",
                (row["id"], symbol, day)).fetchone()]
            if not missing:
                continue
            start_range = published + timedelta(days=min(missing) - 1)
            end_range = published + timedelta(days=max(missing))
            first_range = datetime.fromtimestamp(
                ((int(start_range.timestamp()) + 3599) // 3600) * 3600, tz=timezone.utc)
            last_range = datetime.fromtimestamp(
                (int(end_range.timestamp()) // 3600) * 3600, tz=timezone.utc)
            try:
                if venue == "Binance":
                    try:
                        candles = fetch_hourly_klines(symbol, first_range, last_range - timedelta(milliseconds=1))
                    except urllib.error.HTTPError as exc:
                        if exc.code != 451 or existing:
                            raise
                        binance_blocked = True
                        venue = "OKX"
                        candles = fetch_okx_hourly_klines(symbol, first_range, last_range)
                else:
                    candles = fetch_okx_hourly_klines(symbol, first_range, last_range)
            except Exception as exc:
                log.warning("Market data deferred for %s %s: %s", row["id"], symbol, exc)
                continue
            for day in missing:
                start = published + timedelta(days=day - 1)
                end = published + timedelta(days=day)
                first_hour = datetime.fromtimestamp(
                    ((int(start.timestamp()) + 3599) // 3600) * 3600, tz=timezone.utc)
                last_hour = datetime.fromtimestamp(
                    (int(end.timestamp()) // 3600) * 3600, tz=timezone.utc)
                if last_hour <= first_hour:
                    continue
                try:
                    bars = [x for x in candles if len(x) >= 5 and
                            first_hour.timestamp() * 1000 <= int(x[0]) < last_hour.timestamp() * 1000]
                    if len(bars) < 20:
                        raise ValueError("too few completed hourly bars")
                    with db:
                        db.execute("""INSERT OR IGNORE INTO market_days
                            (video_id,symbol,day_number,window_start,window_end,open,high,low,close,recorded_at,venue)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (row["id"], symbol, day, iso(start), iso(end), str(bars[0][1]),
                             str(max(Decimal(str(x[2])) for x in bars)),
                             str(min(Decimal(str(x[3])) for x in bars)), str(bars[-1][4]), iso(now), venue))
                    recorded += 1
                except Exception as exc:
                    log.warning("Market data deferred for %s %s day %s: %s", row["id"], symbol, day, exc)
                    break
    return recorded


def days_for(db: sqlite3.Connection, video_id: str) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM market_days WHERE video_id=? ORDER BY symbol,day_number",
                      (video_id,)).fetchall()
