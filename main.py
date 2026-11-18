"""Reclaim-Retest v2 alert bot for Binance USDT-M perpetuals.

Usage:
  python main.py run                   start the bot (reads .env)
  python main.py test-telegram         send a test message
  python main.py backtest ARIAUSDT     replay the rules on recent candles of one or more coins
  python main.py scan                  one scan right now: list armed coins and recent triggers
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from bot.binance import Binance
from bot.config import Config
from bot.engine import Engine
from bot.messages import Fmt
from bot.store import Store
from bot.strategy import HOUR_MS, Series, analyze, simulate
from bot.telegram import Telegram

log = logging.getLogger("main")


def setup_logging(level: str) -> None:
    for stream in (sys.stdout, sys.stderr):   # Windows consoles: never crash on emoji
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("reclaim_bot.log", encoding="utf-8")])


async def hourly_loop(eng: Engine) -> None:
    cfg, cl = eng.cfg, eng.client
    while True:
        now = cl.now_ms()
        nxt = (now // HOUR_MS + 1) * HOUR_MS + cfg.close_delay_sec * 1000
        await asyncio.sleep(max(1, (nxt - now) / 1000))
        try:
            if (cl.now_ms() // HOUR_MS) % cfg.universe_refresh_h == 0:
                await eng.load_universe()
                new = [s for s in eng.symbols if s not in eng.bars]
                for s in new:
                    await eng._fetch_history(s)
            await eng.hourly_cycle()
        except Exception:
            log.exception("hourly cycle failed")


async def early_loop(eng: Engine) -> None:
    cfg, cl = eng.cfg, eng.client
    while True:
        try:
            now = cl.now_ms()
            close = (now // HOUR_MS + 1) * HOUR_MS
            start, end = close - cfg.early_start_min * 60_000, close - cfg.early_end_min * 60_000
            if now < start:
                await asyncio.sleep(min(60, (start - now) / 1000))
                continue
            if now <= end and cfg.early_enabled and eng.armed:
                await eng.early_scan()
                await asyncio.sleep(cfg.early_poll_sec)
                continue
            await asyncio.sleep(max(5, (close + 60_000 - now) / 1000))
        except Exception:
            log.exception("early loop error")
            await asyncio.sleep(30)


async def monitor_loop(eng: Engine) -> None:
    while True:
        try:
            await eng.monitor_prices()
        except Exception:
            log.exception("monitor error")
        await asyncio.sleep(eng.cfg.monitor_poll_sec)


async def maintenance_loop(eng: Engine) -> None:
    while True:
        await asyncio.sleep(3600 * 6)
        try:
            await eng.client.sync_time()
            eng.store.prune_alerts()
        except Exception:
            log.exception("maintenance error")


async def run(cfg: Config) -> None:
    client = Binance(cfg.binance_base, cfg.max_weight_per_min)
    tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    eng = Engine(cfg, Store(cfg.db_path), tg, client)
    await client.sync_time()
    await eng.load_universe()
    await eng.load_history()
    await tg.send(f"🤖 <b>Reclaim-Retest v2 bot started</b> · {len(eng.bars)} coins · "
                  f"early alerts {cfg.early_start_min}→{cfg.early_end_min} min before each close · /help")
    await eng.hourly_cycle()   # process the latest closed candle right away
    tasks = [hourly_loop(eng), early_loop(eng), monitor_loop(eng), maintenance_loop(eng)]
    if cfg.telegram_commands:
        tasks.append(tg.poll_commands(eng.command))
    await asyncio.gather(*tasks)


async def backtest(cfg: Config, symbols: list[str], bars_n: int) -> None:
    client = Binance(cfg.binance_base, cfg.max_weight_per_min)
    fmt = Fmt(cfg)
    total = []
    for sym in symbols:
        sym = sym.upper() if sym.upper().endswith("USDT") else sym.upper() + "USDT"
        bars = []
        end = None
        while len(bars) < bars_n:
            chunk = await client.klines(sym, limit=1500, end_ms=end)
            if not chunk:
                break
            bars = chunk + bars
            end = chunk[0][0] - 1
            if len(chunk) < 1500:
                break
        bars = [b for b in bars if b[0] + HOUR_MS <= client.now_ms()]
        s = Series.from_bars(bars, cfg.ema_len, cfg.atr_len)
        trades = simulate(s, cfg, warm=300)
        print(f"\n{sym}: {len(bars)} candles, {fmt.t(bars[0][0])} → {fmt.t(bars[-1][0])}")
        for t in trades:
            print(f"  {fmt.t(t.t_entry)}  entry {fmt.px(sym, t.entry)}  SL {fmt.px(sym, t.sl)} (-{t.risk*100:.1f}%)  "
                  f"TP {fmt.px(sym, t.tp)}  → {t.reason:<4} {t.r_net:+.2f}R  exit {fmt.t(t.t_exit)}")
        total += [t for t in trades if not t.open]
    if total:
        rs = [t.r_net for t in total]
        print(f"\nAll: {len(rs)} closed trades · win {sum(r > 0 for r in rs) / len(rs) * 100:.0f}% · "
              f"avg {sum(rs) / len(rs):+.3f}R · total {sum(rs):+.1f}R")


async def scan_once(cfg: Config) -> None:
    client = Binance(cfg.binance_base, cfg.max_weight_per_min)
    tg = Telegram("", "", dry_run=True)
    eng = Engine(cfg, Store(":memory:"), tg, client)
    await client.sync_time()
    await eng.load_universe()
    await eng.load_history()
    recent = []
    for sym, bars in eng.bars.items():
        if len(bars) < cfg.min_history_h or not eng.eligible(sym):
            continue
        s = Series.from_bars(bars, cfg.ema_len, cfg.atr_len)
        a = analyze(s, cfg)
        if a.armed:
            print(f"ARMED  {sym:<16} needs 1h close > {a.armed.ema_level:.6g} · {a.armed.candles_left} candle(s) left · "
                  f"pump +{a.armed.pump*100:.0f}% flush -{a.armed.dd*100:.0f}% flush candle {a.armed.flush_x:.1f}xATR")
        for sg in a.signals:
            if sg.valid and sg.i >= s.n - 48:
                recent.append((s.T[sg.i], sym, sg))
    for t, sym, sg in sorted(recent):
        print(f"TRIGGER {datetime.fromtimestamp(t / 1000 + 3600, tz=timezone.utc):%Y-%m-%d %H:%M} UTC  {sym:<16} "
              f"flush -{sg.dd*100:.0f}% · {sg.flush_x:.1f}xATR")


def main() -> None:
    ap = argparse.ArgumentParser(description="Reclaim-Retest v2 alert bot")
    ap.add_argument("command", choices=["run", "test-telegram", "backtest", "scan"])
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--bars", type=int, default=3000, help="candles per coin for backtest")
    args = ap.parse_args()
    cfg = Config.from_env(args.env)
    setup_logging(cfg.log_level)
    if args.command == "run":
        if not cfg.telegram_token or not cfg.telegram_chat_id:
            log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing: alerts will only be printed to the log")
        asyncio.run(run(cfg))
    elif args.command == "test-telegram":
        ok = asyncio.run(Telegram(cfg.telegram_token, cfg.telegram_chat_id).send("✅ Reclaim-Retest bot: Telegram works."))
        print("sent" if ok else "failed: check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    elif args.command == "backtest":
        asyncio.run(backtest(cfg, args.symbols or ["ARIAUSDT"], args.bars))
    elif args.command == "scan":
        asyncio.run(scan_once(cfg))


if __name__ == "__main__":
    main()
