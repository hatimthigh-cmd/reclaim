"""The bot must reproduce the research backtest exactly.

1. Signals and trades from bot.strategy match the original JavaScript backtest engine.
2. Feeding candles one by one through the live Engine (the alert lifecycle) produces the
   same trades as the batch simulation.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bot.config import Config  # noqa: E402
from bot.engine import Engine  # noqa: E402
from bot.store import Store  # noqa: E402
from bot.strategy import Series, analyze, simulate  # noqa: E402
from bot.telegram import Telegram  # noqa: E402
from tests.synth import make_series  # noqa: E402

SEEDS = list(range(1, 61))
MODES = {"v2": (2.0, 0.12), "loose": (0.0, 0.25)}
WARM = 900  # the JS reference starts at candle 900


def cfg(mode: str = "v2") -> Config:
    c = Config()
    c.flush_atr_min, c.max_stop_pct = MODES[mode]
    c.validate()
    return c


@pytest.fixture(scope="module")
def datasets():
    return {f"S{s}": make_series(s, 3200) for s in SEEDS}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("mode", list(MODES))
def test_matches_js_reference(datasets, mode):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(datasets, fh)
        path = fh.name
    fx, ms = MODES[mode]
    ref = json.loads(subprocess.check_output(["node", os.path.join(os.path.dirname(__file__), "js_reference.js"), path,
                                              json.dumps({"flushXMin": fx, "maxStop": ms})]))
    c = cfg(mode)
    n_sig = n_tr = 0
    for name, bars in datasets.items():
        s = Series.from_bars(bars)
        a = analyze(s, c, warm=WARM)
        js_sig = ref[name]["signals"]
        assert [(g.i, g.li) for g in a.signals] == [(x[0], x[1]) for x in js_sig], name
        for g, x in zip(a.signals, js_sig):
            assert g.flush_x == pytest.approx(x[2], rel=1e-9)
        py_tr = simulate(s, c, warm=WARM, analysis=a)
        js_tr = ref[name]["trades"]
        assert len(py_tr) == len(js_tr), name
        for p, j in zip(py_tr, js_tr):
            assert (p.i, p.f, p.exit_k, p.open) == (j["i"], j["f"], j["k"], j["open"]), name
            assert p.entry == pytest.approx(j["en"], rel=1e-9)
            assert p.r_net == pytest.approx(j["R"], rel=1e-9, abs=1e-9)
        n_sig += len(a.signals)
        n_tr += len(py_tr)
    print(f"\nparity [{mode}]: {n_sig} signals and {n_tr} trades identical across {len(datasets)} series")
    assert n_tr > (5 if mode == "v2" else 30)  # the synthetic data must actually exercise the rules


@pytest.mark.parametrize("mode", list(MODES))
def test_live_engine_replay_matches_backtest(datasets, mode):
    c = cfg(mode)
    c.alert_skip = True
    total = 0
    for name, bars in list(datasets.items())[:25]:
        s_full = Series.from_bars(bars)
        expected = [t for t in simulate(s_full, c, warm=WARM) if not t.open]
        eng = Engine(c, Store(":memory:"), Telegram("", "", dry_run=True), client=None, warm=WARM)

        async def replay():
            for k in range(WARM - 5, len(bars)):
                await eng.process_closed_bar("TESTUSDT", Series.from_bars(bars[: k + 1]))
        asyncio.run(replay())
        got = eng.store.closed()
        assert len(got) == len(expected), (name, len(got), len(expected))
        for g, e in zip(got, expected):
            assert g.fill_t == e.t_entry, name
            assert g.entry == pytest.approx(e.entry, rel=1e-9)
            assert g.sl == pytest.approx(e.sl, rel=1e-9)
            assert g.tp == pytest.approx(e.tp, rel=1e-9)
            assert g.exit_reason == e.reason, (name, g.exit_reason, e.reason)
            assert g.r_net == pytest.approx(e.r_net, rel=1e-9, abs=1e-9)
            assert g.exit_t == e.t_exit
        total += len(got)
    print(f"\nreplay [{mode}]: {total} trades identical between the live engine and the backtest")
    assert total > (2 if mode == "v2" else 15)
