"""Reclaim-Retest v2: pattern detection and backtest simulation.

This is a line-by-line port of the backtest engine used for the research report,
so live alerts follow the same rules that produced the backtest numbers.

A candle (bar) is a tuple: (open_time_ms, open, high, low, close).
All bars passed in must be CLOSED candles, oldest first, 1h apart.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .indicators import atr as atr_fn, ema as ema_fn

HOUR_MS = 3_600_000


@dataclass
class Series:
    T: list[int]
    O: list[float]
    H: list[float]
    L: list[float]
    C: list[float]
    e200: list[float]
    atr: list[float]

    @property
    def n(self) -> int:
        return len(self.T)

    @classmethod
    def from_bars(cls, bars: list[tuple], ema_len: int = 200, atr_len: int = 14) -> "Series":
        T = [int(b[0]) for b in bars]
        O = [float(b[1]) for b in bars]
        H = [float(b[2]) for b in bars]
        L = [float(b[3]) for b in bars]
        C = [float(b[4]) for b in bars]
        return cls(T, O, H, L, C, ema_fn(C, ema_len), atr_fn(H, L, C, atr_len))


@dataclass
class Signal:
    """A 1h close back above the EMA200 after a pump and flush (the trigger candle is index i)."""
    i: int
    li: int            # index of the flush-low candle
    ph: int            # index of the pump high
    hi: float          # pump high
    low: float         # flush low
    pump: float        # high / 7-day low - 1
    dd: float          # 1 - flush low / high
    flush_x: float     # flush candle range / ATR of the candle before it
    valid: bool        # passes the capitulation filter (flush_x >= FLUSH_ATR_MIN)


@dataclass
class Armed:
    """State after the last closed candle: a trigger is possible on the next candle."""
    li: int
    ph: int
    hi: float
    low: float
    pump: float
    dd: float
    flush_x: float
    ema_level: float   # EMA200 at the last closed candle (the next candle must close above it)
    candles_left: int  # how many more candles can still trigger
    last_close: float


@dataclass
class Analysis:
    signals: list[Signal] = field(default_factory=list)
    armed: Optional[Armed] = None

    @property
    def last_signal(self) -> Optional[Signal]:
        return self.signals[-1] if self.signals else None


def analyze(s: Series, cfg, warm: int = 300) -> Analysis:
    """Walk the candles in time order with no look-ahead and collect every reclaim trigger."""
    n = s.n
    T, H, L, C, e200, atr = s.T, s.H, s.L, s.C, s.e200, s.atr
    out = Analysis()
    dq: deque[int] = deque()
    cur_ph, base, LL, li, rc_done = -1, None, float("inf"), -1, -1
    win = cfg.reclaim_window_h
    ph = -1
    for i in range(1, n):
        j = i - 1
        while dq and H[dq[-1]] <= H[j]:
            dq.pop()
        dq.append(j)
        while dq[0] < i - cfg.pump_lookback_h:
            dq.popleft()
        ph = dq[0]
        if ph != cur_ph:
            cur_ph, base, LL, li, rc_done = ph, None, float("inf"), -1, -1
            for k in range(ph + 1, i):
                if L[k] < LL:
                    LL, li = L[k], k
        if L[i] < LL:
            LL, li = L[i], i
        if i < warm or i - ph < 2 or li < 0:
            continue
        hi = H[ph]
        dd = 1 - LL / hi
        if dd < cfg.dump_min:
            continue
        if base is None:
            base = min(L[max(0, ph - cfg.base_lookback_h):ph]) if ph > 0 else float("inf")
        pump = hi / base - 1 if base and base != float("inf") else 0.0
        if pump < cfg.pump_min:
            continue
        if i - win <= li < i and rc_done != li and C[i] > e200[i] and C[i - 1] <= e200[i - 1]:
            fx = flush_x(s, li)
            out.signals.append(Signal(i, li, ph, hi, LL, pump, dd, fx, fx >= cfg.flush_atr_min))
            rc_done = li

    # Armed state for the NEXT (still forming) candle
    i = n - 1
    if n > warm and li >= 0 and ph >= 0 and i - ph >= 1:
        hi = H[ph]
        dd = 1 - LL / hi
        if base is None and ph > 0:
            base = min(L[max(0, ph - cfg.base_lookback_h):ph])
        pump = hi / base - 1 if base else 0.0
        left = li + win - (i + 1) + 1  # candles i+1 .. li+win can still trigger
        if (dd >= cfg.dump_min and pump >= cfg.pump_min and left >= 1 and rc_done != li
                and C[i] <= e200[i]):
            fx = flush_x(s, li)
            if fx >= cfg.flush_atr_min:
                out.armed = Armed(li, ph, hi, LL, pump, dd, fx, e200[i], left, C[i])
    return out


def diagnose(s: Series, cfg, a: Analysis, warm: int = 300) -> str:
    """One-line, log-friendly explanation of which setup conditions the last closed candle meets.

    Read-only: it re-derives the same numbers analyze() uses and changes no trading logic.
    """
    n = s.n
    i = n - 1
    if n <= warm:
        return f"history too short ({n}/{warm} candles) -> no setup"
    H, L, C = s.H, s.L, s.C
    lo = max(0, i - cfg.pump_lookback_h)
    ph = lo
    for k in range(lo, i):                      # latest of equal highs wins (same as analyze)
        if H[k] >= H[ph]:
            ph = k
    hi = H[ph]
    base = min(L[max(0, ph - cfg.base_lookback_h):ph]) if ph > 0 else 0.0
    pump = hi / base - 1 if base else 0.0
    LL, li = float("inf"), -1
    for k in range(ph + 1, i + 1):
        if L[k] < LL:
            LL, li = L[k], k
    dd = 1 - LL / hi if hi else 0.0
    fx = flush_x(s, li) if li >= 1 else 0.0
    left = li + cfg.reclaim_window_h - i
    reclaimed = any(sg.li == li for sg in a.signals)
    below = C[i] <= s.e200[i]

    def ok(v: bool) -> str:
        return "OK" if v else "NO"

    checks = [
        ("pump", pump >= cfg.pump_min, f"pump +{pump * 100:.0f}% (need {cfg.pump_min * 100:.0f}%)"),
        ("dump", dd >= cfg.dump_min, f"dump -{dd * 100:.0f}% (need {cfg.dump_min * 100:.0f}%)"),
        ("flush", fx >= cfg.flush_atr_min, f"flush {fx:.1f}xATR (need {cfg.flush_atr_min:.1f})"),
        ("window", left >= 1, f"{max(left, 0)} candle(s) left to reclaim"),
        ("not-yet-reclaimed", not reclaimed, "reclaim already fired" if reclaimed else "no reclaim yet"),
        ("below-EMA", below, "close below EMA" if below else "close above EMA"),
    ]
    detail = " · ".join(f"{text} {ok(passed)}" for _, passed, text in checks)
    sig = a.last_signal
    if a.armed:
        return f"{detail} -> ARMED (waiting for close above EMA)"
    if sig and sig.i == i and sig.valid:
        return f"{detail} -> TRIGGER"
    failed = ", ".join(name for name, passed, _ in checks if not passed)
    return f"{detail} -> no setup (fails: {failed})"


def flush_x(s: Series, li: int) -> float:
    a = s.atr[li - 1] if li >= 1 and s.atr[li - 1] > 0 else s.atr[li]
    return (s.H[li] - s.L[li]) / a if a > 0 else 0.0


def levels_for(entry: float, atr_val: float, cfg) -> tuple[float, float, float]:
    """SL, TP and stop distance (fraction) for a fill at `entry` using the ATR of the last closed candle."""
    sl = entry - cfg.atr_mult * atr_val
    risk = (entry - sl) / entry if entry > 0 else 1.0
    tp = entry + cfg.tp_r * (entry - sl)
    return sl, tp, risk


@dataclass
class Trade:
    i: int
    f: int
    exit_k: int
    entry: float
    sl: float
    tp: float
    risk: float
    r_gross: float
    r_net: float
    reason: str
    open: bool
    t_entry: int
    t_exit: int


def simulate(s: Series, cfg, warm: int = 300, analysis: Optional[Analysis] = None) -> list[Trade]:
    """Backtest the v2 rules on one coin (same logic as the research backtest)."""
    a = analysis or analyze(s, cfg, warm)
    trades: list[Trade] = []
    busy = 0
    for sg in a.signals:
        if s.T[sg.i] < busy or not sg.valid:
            continue
        f, entry = -1, 0.0
        for k in range(sg.i + 1, min(s.n, sg.i + cfg.order_valid_h + 1)):
            px = s.e200[k - 1]
            if s.L[k] <= px:
                f, entry = k, min(s.O[k], px)
                break
        if f < 0:
            continue
        sl, tp, risk = levels_for(entry, s.atr[f - 1], cfg)
        if not (sl < entry) or risk > cfg.max_stop_pct or risk < cfg.min_stop_pct:
            continue
        R = entry - sl
        end = min(s.n - 1, f + cfg.max_hold_h)
        out, reason, k = None, "", f
        if s.L[f] <= sl:
            out, reason = -1.0, "SL"
        else:
            for k in range(f + 1, end + 1):
                if s.L[k] <= sl:
                    out, reason = (min(s.O[k], sl) - entry) / R, "SL"
                    break
                if s.H[k] >= tp:
                    out, reason = cfg.tp_r, "TP"
                    break
        is_open = out is None and f + cfg.max_hold_h > s.n - 1
        if out is None:
            k = end
            out, reason = (s.C[end] - entry) / R, ("OPEN" if is_open else "TIME")
        trades.append(Trade(sg.i, f, k, entry, sl, tp, risk, out, out - cfg.cost_pct / risk, reason, is_open,
                            s.T[f], s.T[k] + HOUR_MS))
        busy = s.T[k] + HOUR_MS
    return trades
