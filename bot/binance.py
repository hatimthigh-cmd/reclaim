"""Small Binance USDT-M futures REST client (public endpoints only, standard library only).

No API key is used and no orders are ever placed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("binance")


class BinanceError(Exception):
    pass


class Binance:
    def __init__(self, base: str = "https://fapi.binance.com", max_weight_per_min: int = 1800, concurrency: int = 8):
        self.base = base
        self.max_weight = max_weight_per_min
        self.used_weight = 0
        self._weight_minute = 0
        self._sem = asyncio.Semaphore(concurrency)
        self.time_offset_ms = 0

    # ---------- low level ----------
    def _get_sync(self, path: str, params: dict | None = None, timeout: float = 20.0):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={"User-Agent": "reclaim-bot/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                w = r.headers.get("X-MBX-USED-WEIGHT-1M")
                body = r.read()
                return json.loads(body), (int(w) if w else None), None
        except urllib.error.HTTPError as e:
            retry_after = e.headers.get("Retry-After") if e.headers else None
            body = e.read().decode("utf-8", "ignore")[:300]
            return None, None, (e.code, retry_after, body)

    async def get(self, path: str, params: dict | None = None, weight: int = 1, retries: int = 4):
        for attempt in range(retries):
            await self._respect_weight(weight)
            async with self._sem:
                data, used, err = await asyncio.to_thread(self._get_sync, path, params)
            if used is not None:
                self.used_weight = used
                self._weight_minute = int(time.time() // 60)
            if err is None:
                return data
            code, retry_after, body = err
            if code in (418, 429):
                wait = int(retry_after) if retry_after and retry_after.isdigit() else 60
                log.warning("Rate limited (%s). Sleeping %ss", code, wait)
                await asyncio.sleep(wait)
                continue
            if code >= 500:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise BinanceError(f"HTTP {code} on {path}: {body}")
        raise BinanceError(f"Giving up on {path} after {retries} attempts")

    async def _respect_weight(self, weight: int) -> None:
        minute = int(time.time() // 60)
        if minute != self._weight_minute:
            self.used_weight, self._weight_minute = 0, minute
        if self.used_weight + weight > self.max_weight:
            wait = 61 - time.time() % 60
            log.info("Weight %s near limit, pausing %.0fs", self.used_weight, wait)
            await asyncio.sleep(wait)
            self.used_weight, self._weight_minute = 0, int(time.time() // 60)

    # ---------- endpoints ----------
    async def sync_time(self) -> None:
        data = await self.get("/fapi/v1/time")
        self.time_offset_ms = int(data["serverTime"]) - int(time.time() * 1000)

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.time_offset_ms

    async def perpetual_symbols(self, quote: str = "USDT") -> dict[str, dict]:
        data = await self.get("/fapi/v1/exchangeInfo", weight=1)
        out = {}
        for s in data["symbols"]:
            if s.get("status") == "TRADING" and s.get("quoteAsset") == quote and s.get("contractType") == "PERPETUAL":
                tick = "0.0001"
                for flt in s.get("filters", []):
                    if flt.get("filterType") == "PRICE_FILTER":
                        tick = flt.get("tickSize", tick)
                out[s["symbol"]] = {"tick": tick, "onboard": s.get("onboardDate", 0)}
        return out

    async def quote_volumes_24h(self) -> dict[str, float]:
        data = await self.get("/fapi/v1/ticker/24hr", weight=40)
        return {d["symbol"]: float(d.get("quoteVolume", 0) or 0) for d in data}

    async def prices(self) -> dict[str, float]:
        data = await self.get("/fapi/v1/ticker/price", weight=2)
        return {d["symbol"]: float(d["price"]) for d in data}

    async def klines(self, symbol: str, limit: int = 500, start_ms: int | None = None, end_ms: int | None = None) -> list[tuple]:
        params = {"symbol": symbol, "interval": "1h", "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        w = 1 if limit < 100 else 2 if limit < 500 else 5 if limit <= 1000 else 10
        data = await self.get("/fapi/v1/klines", params, weight=w)
        return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in data]
