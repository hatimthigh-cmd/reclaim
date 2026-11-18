"""Live-only parts: early alerts, early cancellation, price monitor fills/exits, messages, config."""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bot import messages as M  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.engine import Engine  # noqa: E402
from bot.store import Setup, Store  # noqa: E402
from bot.strategy import HOUR_MS, Series, analyze  # noqa: E402
from bot.telegram import Telegram  # noqa: E402
from tests.synth import make_series  # noqa: E402

WARM = 900


class FakeClient:
    def __init__(self):
        self.now = 0
        self.price_map: dict[str, float] = {}
        self.forming: dict[str, tuple] = {}

    def now_ms(self):
        return self.now

    async def prices(self):
        return dict(self.price_map)

    async def klines(self, sym, limit=2, **kw):
        return [self.forming[sym]]


def loose_cfg() -> Config:
    c = Config()
    c.flush_atr_min, c.max_stop_pct = 0.0, 0.25
    c.alert_skip = True
    return c


def find_trigger(c: Config):
    """A synthetic coin and index where the candle before is armed and the candle itself triggers."""
    for seed in range(1, 80):
        bars = make_series(seed, 3200)
        s = Series.from_bars(bars)
        for sg in analyze(s, c, WARM).signals:
            if not sg.valid:
                continue
            prev = analyze(Series.from_bars(bars[: sg.i]), c, WARM)
            if prev.armed:
                return bars, sg.i
    raise AssertionError("no armed->trigger case found")


def make_engine(c: Config, client=None) -> Engine:
    return Engine(c, Store(":memory:"), Telegram("", "", dry_run=True), client=client, warm=WARM)


def test_early_alert_then_trigger():
    c = loose_cfg()
    bars, i = find_trigger(c)
    cl = FakeClient()
    eng = make_engine(c, cl)
    eng.bars["TESTUSDT"] = bars[:i]

    async def go():
        await eng.process_closed_bar("TESTUSDT", Series.from_bars(bars[:i]))     # armed after candle i-1
        assert "TESTUSDT" in eng.armed
        assert any("WATCH" in m for m in eng.tg.sent)
        # 6 minutes before candle i closes, the forming candle already looks like the final one
        cl.now = bars[i][0] + HOUR_MS - 6 * 60_000
        o, h, l, cl_ = bars[i][1:]
        cl.forming["TESTUSDT"] = (bars[i][0], o, h, l, max(cl_, Series.from_bars(bars[:i]).e200[-1] * 1.01))
        await eng.early_scan()
        assert any("EARLY" in m and "CANCELLED" not in m for m in eng.tg.sent)
        await eng.process_closed_bar("TESTUSDT", Series.from_bars(bars[: i + 1]))
        assert any("TRIGGER" in m for m in eng.tg.sent)
        assert not any("EARLY CANCELLED" in m for m in eng.tg.sent)
        assert eng.store.active_for("TESTUSDT").status == "pending"
    asyncio.run(go())


def test_early_cancel_when_close_fails():
    c = loose_cfg()
    bars, i = find_trigger(c)
    cl = FakeClient()
    eng = make_engine(c, cl)
    eng.bars["TESTUSDT"] = bars[:i]
    s_prev = Series.from_bars(bars[:i])

    async def go():
        await eng.process_closed_bar("TESTUSDT", s_prev)
        cl.now = bars[i][0] + HOUR_MS - 5 * 60_000
        o, h, l, _ = bars[i][1:]
        cl.forming["TESTUSDT"] = (bars[i][0], o, max(h, s_prev.e200[-1] * 1.02), l, s_prev.e200[-1] * 1.02)
        await eng.early_scan()
        assert any("EARLY" in m for m in eng.tg.sent)
        # the real candle closes back below the EMA200
        bad = list(bars[: i + 1])
        t, o, h, l, _ = bad[-1]
        bad[-1] = (t, o, h, l, s_prev.e200[-1] * 0.98)
        await eng.process_closed_bar("TESTUSDT", Series.from_bars(bad))
        assert any("EARLY CANCELLED" in m for m in eng.tg.sent)
        assert not any("🚨" in m for m in eng.tg.sent)
    asyncio.run(go())


def test_monitor_fill_then_sl_and_tp():
    c = loose_cfg()
    cl = FakeClient()
    eng = make_engine(c, cl)
    t0 = 1_770_000_000_000 // HOUR_MS * HOUR_MS
    st = eng.store.save(Setup(symbol="AAAUSDT", status="pending", trigger_t=t0, level=100.0, level_atr=2.0,
                              expires_t=t0 + 48 * HOUR_MS))
    st2 = eng.store.save(Setup(symbol="BBBUSDT", status="pending", trigger_t=t0, level=10.0, level_atr=0.2,
                               expires_t=t0 + 48 * HOUR_MS))

    async def go():
        cl.now = t0 + HOUR_MS + 60_000
        cl.price_map = {"AAAUSDT": 99.5, "BBBUSDT": 10.5}
        await eng.monitor_prices()
        a = eng.store.active_for("AAAUSDT")
        assert a.status == "open" and a.entry == 99.5
        assert a.sl == pytest.approx(99.5 - 3.0) and a.tp == pytest.approx(99.5 + 4.5)
        assert eng.store.active_for("BBBUSDT").status == "pending"
        cl.price_map = {"AAAUSDT": 96.0, "BBBUSDT": 9.9}
        cl.now += 600_000
        await eng.monitor_prices()
        closed = eng.store.closed()
        assert closed[0].exit_reason == "SL"
        assert closed[0].r_net == pytest.approx(-1 - c.cost_pct / closed[0].risk)
        b = eng.store.active_for("BBBUSDT")
        assert b.status == "open"
        cl.price_map = {"BBBUSDT": b.tp + 0.01}
        cl.now += 3 * HOUR_MS
        await eng.monitor_prices()
        rows = {r.symbol: r for r in eng.store.closed()}
        assert rows["BBBUSDT"].exit_reason == "TP"
        assert rows["BBBUSDT"].r_net == pytest.approx(c.tp_r - c.cost_pct / rows["BBBUSDT"].risk)
        assert any("FILLED" in m for m in eng.tg.sent) and any("TP HIT" in m for m in eng.tg.sent)
        stats = await eng.command("/stats", [])
        assert "Trades 2" in stats
    asyncio.run(go())
    assert st.id and st2.id


def test_paused_order_touch_is_skipped():
    c = loose_cfg()
    cl = FakeClient()
    eng = make_engine(c, cl)
    t0 = 1_770_000_000_000 // HOUR_MS * HOUR_MS
    eng.store.save(Setup(symbol="CCCUSDT", status="pending", trigger_t=t0, level=1.0, level_atr=0.3, paused=1,
                         expires_t=t0 + 48 * HOUR_MS))

    async def go():
        cl.now = t0 + 2 * HOUR_MS
        cl.price_map = {"CCCUSDT": 0.99}
        await eng.monitor_prices()
    asyncio.run(go())
    assert eng.store.db.execute("SELECT status FROM setups").fetchone()[0] == "skipped"


def test_sizing_and_messages():
    c = Config()
    c.account_usd, c.risk_pct, c.max_leverage = 1000, 1, 10
    z = M.sizing(c, 2.0, 0.05)
    assert z["risk_usd"] == 10 and z["position"] == pytest.approx(200) and z["lev"] == 10
    z = M.sizing(c, 2.0, 0.12)   # 12% stop -> max ~7x keeps liquidation beyond the stop
    assert z["lev"] == 7
    f = M.Fmt(c, {"XUSDT": "0.0001"})
    st = Setup(symbol="XUSDT", trigger_t=1_770_000_000_000, level=1.23456, level_atr=0.02, expires_t=1_770_100_000_000,
               entry=1.2, sl=1.15, tp=1.275, risk=0.0417, exit_price=1.275, exit_reason="TP", r_net=1.47, fill_t=1_770_000_000_000)

    class Sig:  # minimal stand-in
        pump, dd, flush_x, hi, low = 1.2, 0.4, 2.6, 2.0, 1.0
    for text in (M.trigger(f, "XUSDT", st, Sig, 1.2, 1.3, 0.04), M.filled(f, st, st.fill_t), M.closed(f, st),
                 M.expired(f, st), M.level_update(f, st, 1.2, 1.3, 0.04, True), M.skipped(f, st, 0.15),
                 M.early(f, "XUSDT", Sig, 1.25, 1.23, 1.235, 1.2, 1.3, 0.04, st.trigger_t + HOUR_MS, st.trigger_t)):
        assert "XUSDT" not in text or "binance.com" in text  # coin names shown without the USDT suffix
        assert "<b>" in text
    assert f.px("XUSDT", 1.234567) == "1.2346"


def test_config_env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=123 # me\nFLUSH_ATR_MIN=2.5\nMAX_STOP_PCT=10\n"
                 "EARLY_START_MIN=8\nEARLY_END_MIN=2\nEXCLUDE_SYMBOLS=btcusdt, ETHUSDT\n", encoding="utf-8")
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "FLUSH_ATR_MIN", "MAX_STOP_PCT", "EARLY_START_MIN",
              "EARLY_END_MIN", "EXCLUDE_SYMBOLS"):
        os.environ.pop(k, None)
    c = Config.from_env(p)
    assert c.telegram_chat_id == "123" and c.flush_atr_min == 2.5 and c.max_stop_pct == pytest.approx(0.10)
    assert c.early_start_min == 8 and c.exclude_symbols == ["BTCUSDT", "ETHUSDT"]
    os.environ["EARLY_END_MIN"] = "9"
    with pytest.raises(ValueError):
        Config.from_env(p)
    os.environ.pop("EARLY_END_MIN")
