"""Indicators, computed exactly the way the backtest computed them."""
from __future__ import annotations


def ema(values: list[float], length: int) -> list[float]:
    """EMA seeded with the first value (same as the backtest)."""
    if not values:
        return []
    m = 2.0 / (length + 1)
    out = []
    e = values[0]
    for v in values:
        e = v * m + e * (1 - m)
        out.append(e)
    return out


def atr(highs: list[float], lows: list[float], closes: list[float], length: int = 14) -> list[float]:
    """ATR: running mean for the first `length` candles, then Wilder smoothing (same as the backtest)."""
    out = []
    a = 0.0
    for i in range(len(closes)):
        if i == 0:
            tr = highs[i] - lows[i]
        else:
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        if i < length:
            a = (a * i + tr) / (i + 1)
        else:
            a = (a * (length - 1) + tr) / length
        out.append(a)
    return out
