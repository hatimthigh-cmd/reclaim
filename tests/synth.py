"""Synthetic 1h candles with repeated pump -> flush -> reclaim episodes (for offline tests)."""
from __future__ import annotations

import math
import random

HOUR_MS = 3_600_000


def make_series(seed: int, n: int = 4000, start_ms: int = 1_760_000_000_000 // HOUR_MS * HOUR_MS) -> list[tuple]:
    rnd = random.Random(seed)
    bars = []
    price = 1.0 * (1 + rnd.random())
    t = start_ms
    mode, left, drift, vol = "calm", 0, 0.0, 0.01
    for _ in range(n):
        if left <= 0:
            r = rnd.random()
            if mode == "calm" and r < 0.2:
                mode, left = "pump", rnd.randint(20, 90)
                drift = math.log(1 + rnd.uniform(0.5, 1.8)) / left
                vol = 0.02
            elif mode == "pump":
                mode, left = "flush", rnd.randint(4, 30)
                drift = math.log(1 - rnd.uniform(0.3, 0.6)) / left
                vol = 0.025
            elif mode == "flush":
                mode, left = "bounce", rnd.randint(4, 12)
                drift = math.log(1 + rnd.uniform(0.15, 0.6)) / left
                vol = 0.02
            elif mode == "bounce":
                mode, left = "pullback", rnd.randint(6, 30)
                drift = math.log(1 - rnd.uniform(0.08, 0.35)) / left
                vol = 0.018
            else:
                mode, left = "calm", rnd.randint(60, 220)
                drift, vol = rnd.uniform(-0.0006, 0.0006), 0.008
        o = price
        ret = drift + rnd.gauss(0, vol)
        if mode == "flush" and left == 1:
            ret += -rnd.uniform(0.06, 0.2)   # capitulation candle
        c = max(1e-6, o * math.exp(ret))
        wick_dn = abs(rnd.gauss(0, vol)) * (3 if mode == "flush" and rnd.random() < 0.3 else 1)
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol * 0.7)))
        lo = min(o, c) * (1 - wick_dn)
        bars.append((t, round(o, 8), round(h, 8), round(lo, 8), round(c, 8)))
        price, t, left = c, t + HOUR_MS, left - 1
    return bars
