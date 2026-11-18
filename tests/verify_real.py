"""Verify the bot against the real backtest trades on real Binance candles.

Input: backtest_candles_v2.bin.gz (exported from the research backtest: candles + its trade list).

Check A: the bot's strategy code on the full candle history (same as the backtest)  -> every trade must match.
Check B: the bot's live engine, run hour by hour on a rolling 1000-candle window
         (exactly what the running bot sees)                                           -> how many trades match.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import struct
import sys
import time
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.config import Config  # noqa: E402
from bot.engine import Engine  # noqa: E402
from bot.store import Store  # noqa: E402
from bot.strategy import HOUR_MS, Series, analyze, simulate  # noqa: E402
from bot.telegram import Telegram  # noqa: E402

WARM = 900


def unshuffle(buf: bytes, width: int) -> bytes:
    n = len(buf) // width
    out = bytearray(len(buf))
    for b in range(width):
        out[b::width] = buf[b * n:(b + 1) * n]
    return bytes(out)


def load(path: str):
    raw = gzip.decompress(Path(path).read_bytes())
    hl = struct.unpack("<I", raw[:4])[0]
    header = json.loads(raw[4:4 + hl])
    body = raw[4 + hl:]
    data = {}
    for m in header["symbols"]:
        n = m["n"]
        th = array("i"); th.frombytes(unshuffle(body[m["offT"]:m["offT"] + 4 * n], 4))
        xs = array("f"); xs.frombytes(unshuffle(body[m["offX"]:m["offX"] + 16 * n], 4))
        data[m["s"]] = [(th[i] * HOUR_MS, xs[4 * i], xs[4 * i + 1], xs[4 * i + 2], xs[4 * i + 3]) for i in range(n)]
    return header, data


def cfg_for(mode: str) -> Config:
    c = Config()
    if mode == "base":
        c.flush_atr_min, c.max_stop_pct = 0.0, 0.25
    return c


def check_a(header, data, mode: str):
    c = cfg_for(mode)
    ref_all = header["trades_v2" if mode == "v2" else "trades_base"]
    same = diff = 0
    problems = []
    for sym, bars in data.items():
        s = Series.from_bars(bars)
        got = [t for t in simulate(s, c, warm=WARM)]
        ref = ref_all.get(sym, [])
        if len(got) != len(ref):
            problems.append(f"{sym}: bot {len(got)} trades vs backtest {len(ref)}")
        for g, r in zip(got, ref):
            ok = (g.i, g.f, g.exit_k) == (r[0], r[1], r[2]) and abs(g.entry - r[5]) <= 1e-9 * max(1, abs(r[5])) \
                and abs(g.r_net - r[7]) < 1e-9 and int(g.open) == r[8]
            same += ok
            diff += not ok
            if not ok:
                problems.append(f"{sym}: bot {(g.i, g.f, g.exit_k, g.entry, g.r_net)} vs backtest {tuple(r[:3]) + (r[5], r[7])}")
    return same, diff, problems


def check_b(header, data, window: int = 1000):
    """Hour-by-hour live engine with a rolling window, only on candles where something can happen."""
    c = cfg_for("v2")
    ref_all = header["trades_v2"]
    eng = Engine(c, Store(":memory:"), Telegram("", "", dry_run=True), client=None, warm=300)

    async def run():
        for sym, bars in data.items():
            full = Series.from_bars(bars)
            a = analyze(full, c, warm=WARM)
            # candles where a trigger is possible: every reclaim signal, plus any close within 0.3% of the EMA200
            cand = {g.i for g in a.signals}
            for k in range(WARM, full.n):
                if abs(full.C[k] / full.e200[k] - 1) < 3e-3:
                    cand.add(k)
            k = WARM
            while k < full.n:
                st = eng.store.active_for(sym)
                if k in cand or st is not None:
                    w = bars[max(0, k - window + 1): k + 1]
                    await eng.process_closed_bar(sym, Series.from_bars(w))
                k += 1
    asyncio.run(run())
    got = {}
    for st in eng.store.db.execute("SELECT * FROM setups WHERE status='closed'"):
        st = eng.store._row(st)
        got.setdefault(st.symbol, []).append(st)
    same, missing, extra, details = 0, 0, 0, []
    for sym in data:
        ref = [r for r in ref_all.get(sym, []) if not r[8]]
        g = sorted(got.get(sym, []), key=lambda x: x.fill_t)
        gk = {(x.fill_t // HOUR_MS, x.exit_reason): x for x in g}
        for r in ref:
            fill_h = int(round(r[4]))
            match = [x for (fh, _), x in gk.items() if fh == fill_h]
            if match and abs(match[0].entry / r[5] - 1) < 1e-3 and abs(match[0].r_net - r[7]) < 0.05:
                same += 1
            else:
                missing += 1
                details.append(f"MISSING {sym} fill@{fill_h} entry {r[5]:.6g} R {r[7]:+.2f} (bot: {[(x.fill_t // HOUR_MS, x.entry, round(x.r_net, 2)) for x in g if abs(x.fill_t // HOUR_MS - fill_h) < 72]})")
        ref_fills = {int(round(r[4])) for r in ref}
        for x in g:
            if x.fill_t // HOUR_MS not in ref_fills:
                extra += 1
                details.append(f"EXTRA {sym} fill@{x.fill_t // HOUR_MS} entry {x.entry:.6g} {x.exit_reason} R {x.r_net:+.2f}")
    return same, missing, extra, details


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "backtest_candles_v2.bin.gz"
    t0 = time.time()
    header, data = load(path)
    nb = sum(len(v) for v in data.values())
    print(f"Loaded {len(data)} coins, {nb:,} candles in {time.time() - t0:.1f}s")
    ref_v2 = sum(1 for v in header["trades_v2"].values() for r in v if not r[8])
    ref_base = sum(1 for v in header["trades_base"].values() for r in v if not r[8])
    print(f"Backtest reference: {ref_v2} closed v2 trades, {ref_base} closed trades with the original rules\n")

    for mode in ("v2", "base"):
        t0 = time.time()
        same, diff, probs = check_a(header, data, mode)
        print(f"[A] full history, {mode:<4}: {same} identical, {diff} different ({time.time() - t0:.0f}s)")
        for p in probs[:10]:
            print("     ", p)

    t0 = time.time()
    same, missing, extra, details = check_b(header, data)
    print(f"\n[B] live engine, rolling 1000-candle window, v2: {same} of {ref_v2} backtest trades reproduced, "
          f"{missing} missing, {extra} extra ({time.time() - t0:.0f}s)")
    for d in details[:30]:
        print("     ", d)
