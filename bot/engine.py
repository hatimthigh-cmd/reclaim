"""Live engine: keeps candles fresh, runs the strategy each hour, sends alerts and tracks every setup.

The closed-candle logic (process_closed_bar) is deterministic and mirrors strategy.simulate(),
so the alert lifecycle follows the backtest rules. The price monitor only makes alerts faster.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from . import messages as M
from .binance import Binance, BinanceError
from .store import Setup, Store
from .strategy import HOUR_MS, Series, analyze, levels_for
from .telegram import Telegram

log = logging.getLogger("engine")


class Engine:
    def __init__(self, cfg, store: Store, tg: Telegram, client: Optional[Binance] = None, warm: int = 300):
        self.cfg = cfg
        self.store = store
        self.tg = tg
        self.client = client
        self.warm = warm
        self.bars: dict[str, list[tuple]] = {}
        self.symbols: dict[str, dict] = {}
        self.volumes: dict[str, float] = {}
        self.armed: dict[str, tuple] = {}        # symbol -> (Armed, last closed bar time)
        self.early_sent: dict[str, int] = {}     # symbol -> forming bar open time an EARLY alert was sent for
        self.fmt = M.Fmt(cfg)
        self.last_cycle_ms = 0
        self.started = time.time()

    # ------------------------------------------------------------------ helpers
    async def say(self, text: str, key: Optional[str] = None) -> None:
        if key is not None and not self.store.once(key):
            return
        await self.tg.send(text)

    def eligible(self, sym: str) -> bool:
        c = self.cfg
        if c.include_symbols and sym not in c.include_symbols:
            return False
        if sym in c.exclude_symbols:
            return False
        if c.min_quote_volume_24h > 0 and self.volumes and self.volumes.get(sym, 0) < c.min_quote_volume_24h:
            return False
        return True

    def busy_until(self, sym: str) -> int:
        """Exit time of the coin's last closed trade (a trigger before this is ignored, like the backtest)."""
        r = self.store.db.execute(
            "SELECT MAX(exit_t) FROM setups WHERE symbol=? AND status='closed'", (sym,)).fetchone()
        return int(r[0]) if r and r[0] else 0

    def est(self, level: float, atr_val: float):
        return levels_for(level, atr_val, self.cfg)

    # ------------------------------------------------------------------ core per-candle logic
    async def process_closed_bar(self, sym: str, s: Series, notify_new: bool = True) -> None:
        """Handle the newest CLOSED candle of `s` for one coin: fills, exits, expiry, triggers, watch."""
        c, f = self.cfg, self.fmt
        k = s.n - 1
        Tk = s.T[k]

        # 1) pending order: did this candle fill it?
        st = self.store.active_for(sym)
        if st and st.status == "pending" and Tk > st.trigger_t:
            if s.L[k] <= st.level:
                if st.paused:
                    st.status = "skipped"
                    self.store.save(st)
                    if c.alert_skip:
                        await self.say(M.skipped(f, st, c.atr_mult * st.level_atr / st.level), f"skip:{st.id}")
                else:
                    await self._fill(st, min(s.O[k], st.level), Tk)
                    st = self.store.active_for(sym)
            if st and st.status == "pending":
                if Tk >= st.expires_t:
                    st.status = "expired"
                    self.store.save(st)
                    if c.alert_expire:
                        await self.say(M.expired(f, st), f"exp:{st.id}")
                else:
                    was_paused = bool(st.paused)
                    st.level, st.level_atr = s.e200[k], s.atr[k]
                    sl, tp, risk = self.est(st.level, st.level_atr)
                    st.paused = int(risk > c.max_stop_pct or risk < c.min_stop_pct)
                    self.store.save(st)
                    nk = f"nl:{st.id}"
                    last_lvl = float(self.store.get_kv(nk, "0") or 0)
                    moved = last_lvl <= 0 or abs(st.level / last_lvl - 1) >= c.level_update_min_pct
                    if c.alert_level_update and notify_new and (moved or was_paused != bool(st.paused)):
                        self.store.set_kv(nk, str(st.level))
                        await self.say(M.level_update(f, st, sl, tp, risk, was_paused), f"lvl:{st.id}:{Tk}")

        # 2) open trade: SL / TP / time exit on this candle
        st = self.store.active_for(sym)
        if st and st.status == "open" and Tk >= st.fill_t:
            if s.L[k] <= st.sl:
                px = st.sl if Tk == st.fill_t else min(s.O[k], st.sl)
                await self._close(st, px, "SL", Tk + HOUR_MS)
            elif Tk > st.fill_t and s.H[k] >= st.tp:
                await self._close(st, st.tp, "TP", Tk + HOUR_MS)
            elif Tk >= st.fill_t + c.max_hold_h * HOUR_MS:
                await self._close(st, s.C[k], "TIME", Tk + HOUR_MS)

        # 3) trigger on this candle?
        a = analyze(s, c, self.warm)
        sig = a.last_signal
        early_for = self.early_sent.pop(sym, None)
        if sig and sig.i == k and sig.valid:
            await self._on_trigger(sym, s, sig, notify_new)
        elif early_for == Tk and c.alert_early:
            await self.say(M.early_cancel(f, sym, s.C[k], s.e200[k]), f"ecan:{sym}:{Tk}")

        # 4) armed for the next candle?
        active = self.store.active_for(sym)
        if a.armed and not (active and active.status == "open") and self.eligible(sym):
            self.armed[sym] = (a.armed, Tk)
            if c.alert_watch and notify_new:
                await self.say(M.watch(f, sym, a.armed, Tk), f"watch:{sym}:{s.T[a.armed.li]}")
        else:
            self.armed.pop(sym, None)

    async def _on_trigger(self, sym: str, s: Series, sig, notify: bool) -> None:
        c, f = self.cfg, self.fmt
        k = sig.i
        if s.T[k] < self.busy_until(sym):
            return
        st = self.store.active_for(sym)
        if st and st.status == "open":
            return  # one trade per coin
        if st and st.status == "pending":
            st.expires_t = s.T[k] + c.order_valid_h * HOUR_MS   # a fresh trigger extends the order window
            self.store.save(st)
            return
        if not self.eligible(sym):
            return
        cap_note = ""
        if c.max_open_trades > 0 and len(self.store.by_status("pending", "open")) >= c.max_open_trades:
            cap_note = f"⚠️ MAX_OPEN_TRADES ({c.max_open_trades}) reached: not tracked.\n"
            if notify and c.alert_trigger:
                await self.say(f"ℹ️ Trigger on {f.coin(sym)} ignored: MAX_OPEN_TRADES reached.", f"cap:{sym}:{s.T[k]}")
            return
        level, atr_v = s.e200[k], s.atr[k]
        sl, tp, risk = self.est(level, atr_v)
        st = Setup(symbol=sym, status="pending", trigger_t=s.T[k], li_t=s.T[sig.li], high=sig.hi, flush_low=sig.low,
                   pump=sig.pump, dd=sig.dd, flush_x=sig.flush_x, level=level, level_atr=atr_v,
                   paused=int(risk > c.max_stop_pct or risk < c.min_stop_pct),
                   expires_t=s.T[k] + c.order_valid_h * HOUR_MS)
        self.store.save(st)
        self.store.set_kv(f"nl:{st.id}", str(level))
        if notify and c.alert_trigger:
            await self.say(M.trigger(f, sym, st, sig, sl, tp, risk, cap_note), f"trig:{sym}:{s.T[k]}")

    async def _fill(self, st: Setup, entry: float, fill_bar_t: int) -> None:
        c = self.cfg
        sl, tp, risk = levels_for(entry, st.level_atr, c)
        if not (sl < entry) or risk > c.max_stop_pct or risk < c.min_stop_pct:
            st.status = "skipped"
            self.store.save(st)
            if c.alert_skip:
                await self.say(M.skipped(self.fmt, st, risk), f"skip:{st.id}")
            return
        st.status, st.entry, st.sl, st.tp, st.risk, st.fill_t = "open", entry, sl, tp, risk, fill_bar_t
        self.store.save(st)
        if c.alert_fill:
            await self.say(M.filled(self.fmt, st, fill_bar_t), f"fill:{st.id}")

    async def _close(self, st: Setup, price: float, reason: str, exit_ms: int) -> None:
        R = st.entry - st.sl
        gross = self.cfg.tp_r if reason == "TP" else (-1.0 if reason == "SL" and price >= st.sl else (price - st.entry) / R)
        st.status, st.exit_price, st.exit_reason, st.exit_t = "closed", price, reason, exit_ms
        st.r_net = gross - self.cfg.cost_pct / st.risk
        self.store.save(st)
        if self.cfg.alert_exit:
            await self.say(M.closed(self.fmt, st), f"close:{st.id}")

    # ------------------------------------------------------------------ live data
    async def load_universe(self) -> None:
        self.symbols = await self.client.perpetual_symbols(self.cfg.quote_asset)
        self.fmt.ticks = {s: v["tick"] for s, v in self.symbols.items()}
        try:
            self.volumes = await self.client.quote_volumes_24h()
        except BinanceError as e:
            log.warning("24h volume fetch failed: %s", e)
        for s in list(self.bars):
            if s not in self.symbols and not self.store.active_for(s):
                self.bars.pop(s, None)
        log.info("Universe: %d %s perpetuals", len(self.symbols), self.cfg.quote_asset)

    def _closed_only(self, bars: list[tuple]) -> list[tuple]:
        now = self.client.now_ms()
        return [b for b in bars if b[0] + HOUR_MS <= now]

    async def _fetch_history(self, sym: str) -> None:
        try:
            bars = await self.client.klines(sym, limit=self.cfg.history_bars)
            self.bars[sym] = self._closed_only(bars)
        except BinanceError as e:
            log.warning("history %s: %s", sym, e)

    async def load_history(self) -> None:
        todo = [s for s in self.symbols if s not in self.bars]
        log.info("Loading %d candles for %d coins (first start takes a few minutes)...", self.cfg.history_bars, len(todo))
        for i in range(0, len(todo), 20):
            await asyncio.gather(*(self._fetch_history(s) for s in todo[i:i + 20]))
        log.info("History loaded")

    async def _update_one(self, sym: str) -> None:
        cur = self.bars.get(sym)
        if not cur or self.client.now_ms() - cur[-1][0] > 4 * HOUR_MS:
            await self._fetch_history(sym)
            return
        try:
            new = await self.client.klines(sym, limit=5)
        except BinanceError as e:
            log.warning("update %s: %s", sym, e)
            return
        m = {b[0]: b for b in cur}
        for b in self._closed_only(new):
            m[b[0]] = b
        merged = [m[t] for t in sorted(m)]
        self.bars[sym] = merged[-self.cfg.history_bars:]

    async def update_bars(self) -> None:
        syms = list(self.symbols)
        for i in range(0, len(syms), 25):
            await asyncio.gather(*(self._update_one(s) for s in syms[i:i + 25]))

    async def hourly_cycle(self) -> None:
        t0 = time.time()
        await self.update_bars()
        n_done = 0
        for sym, bars in list(self.bars.items()):
            if len(bars) < self.cfg.min_history_h:
                continue
            last_key = f"last:{sym}"
            last_done = int(self.store.get_kv(last_key, "0") or 0)
            new_idx = [i for i, b in enumerate(bars) if b[0] > last_done]
            if not new_idx:
                continue
            if last_done == 0:
                new_idx = new_idx[-1:]           # first run: only the newest candle
            for j, idx in enumerate(new_idx[-6:]):  # catch up at most 6 missed candles
                s = Series.from_bars(bars[: idx + 1], self.cfg.ema_len, self.cfg.atr_len)
                await self.process_closed_bar(sym, s, notify_new=(j == len(new_idx[-6:]) - 1))
            self.store.set_kv(last_key, str(bars[new_idx[-1]][0]))
            n_done += 1
            state = "ARMED" if sym in self.armed else "-"
            log.info("Scanned %-14s close=%-12g EMA%d=%-12g %s [%d/%d]", sym, s.C[-1], self.cfg.ema_len,
                     s.e200[-1], state, n_done, len(self.bars))
        self.last_cycle_ms = self.client.now_ms()
        log.info("Hourly cycle: %d coins processed in %.1fs · watching %d · pending %d · open %d",
                 n_done, time.time() - t0, len(self.armed), len(self.store.by_status("pending")),
                 len(self.store.by_status("open")))
        nxt = (self.last_cycle_ms // HOUR_MS + 1) * HOUR_MS
        log.info("Next hourly scan at %s UTC (waiting for the candle to close)",
                 time.strftime("%H:%M", time.gmtime(nxt / 1000)))

    async def early_scan(self) -> None:
        """Called repeatedly in the last minutes of each hour for coins that are armed."""
        c, f = self.cfg, self.fmt
        now = self.client.now_ms()
        forming_t = (now // HOUR_MS) * HOUR_MS
        for sym, (armed, last_t) in list(self.armed.items()):
            if last_t != forming_t - HOUR_MS or self.early_sent.get(sym) == forming_t:
                continue
            try:
                kl = await self.client.klines(sym, limit=2)
            except BinanceError:
                continue
            cur = [b for b in kl if b[0] == forming_t]
            if not cur:
                continue
            fb = cur[0]
            bars = self.bars.get(sym, [])
            if not bars or bars[-1][0] != forming_t - HOUR_MS:
                continue
            s = Series.from_bars(bars + [fb], c.ema_len, c.atr_len)
            a = analyze(s, c, self.warm)
            sig = a.last_signal
            ema_prev = s.e200[-2]
            price = fb[4]
            if sig and sig.i == s.n - 1 and sig.valid and price >= ema_prev * (1 + c.early_buffer_pct):
                st = self.store.active_for(sym)
                if st and st.status == "open":
                    continue
                level = s.e200[-1]
                sl, tp, risk = self.est(level, s.atr[-1])
                self.early_sent[sym] = forming_t
                if c.alert_early:
                    await self.say(M.early(f, sym, sig, price, ema_prev, level, sl, tp, risk, forming_t + HOUR_MS, now),
                                   f"early:{sym}:{forming_t}")

    async def monitor_prices(self) -> None:
        """Fast checks between candle closes: fills and SL/TP hits."""
        pend = [x for x in self.store.by_status("pending")]
        opn = self.store.by_status("open")
        if not pend and not opn:
            return
        prices = await self.client.prices()
        now = self.client.now_ms()
        bar_t = (now // HOUR_MS) * HOUR_MS
        for st in pend:
            p = prices.get(st.symbol)
            if p is None or bar_t <= st.trigger_t or p > st.level:
                continue
            if st.paused:
                st.status = "skipped"
                self.store.save(st)
                if self.cfg.alert_skip:
                    await self.say(M.skipped(self.fmt, st, self.cfg.atr_mult * st.level_atr / st.level), f"skip:{st.id}")
                continue
            await self._fill(st, min(p, st.level), bar_t)
        for st in self.store.by_status("open"):
            p = prices.get(st.symbol)
            if p is None:
                continue
            if p <= st.sl:
                await self._close(st, st.sl, "SL", now)
            elif p >= st.tp:
                await self._close(st, st.tp, "TP", now)

    # ------------------------------------------------------------------ commands
    async def command(self, cmd: str, args: list[str]) -> str:
        f = self.fmt
        if cmd in ("/start", "/help"):
            return ("<b>Reclaim-Retest v2 bot</b>\n/status · bot health\n/watch · coins waiting for a reclaim\n"
                    "/pending · live buy limits\n/open · open trades\n/stats [days] · paper results\n/rules · strategy rules")
        if cmd == "/status":
            up = (time.time() - self.started) / 3600
            last = f.t(self.last_cycle_ms) if self.last_cycle_ms else "not yet"
            return (f"🟢 Running {up:.1f}h · {len(self.bars)} coins tracked · last scan {last}\n"
                    f"Watching {len(self.armed)} · pending {len(self.store.by_status('pending'))} · "
                    f"open {len(self.store.by_status('open'))}")
        if cmd == "/watch":
            if not self.armed:
                return "No coins are armed right now."
            return "👀 <b>Armed coins</b>\n" + "\n".join(
                f"• {f.coin(s)}: needs a 1h close above <code>{f.px(s, a.ema_level)}</code> "
                f"({a.candles_left} candle(s) left)" for s, (a, _) in self.armed.items())
        if cmd == "/pending":
            rows = self.store.by_status("pending")
            if not rows:
                return "No pending orders."
            out = []
            for st in rows:
                sl, tp, risk = self.est(st.level, st.level_atr)
                out.append(f"• {f.coin(st.symbol)}: limit <code>{f.px(st.symbol, st.level)}</code> SL≈"
                           f"<code>{f.px(st.symbol, sl)}</code> TP≈<code>{f.px(st.symbol, tp)}</code>"
                           f"{' ⏸ paused' if st.paused else ''} · until {f.t(st.expires_t + HOUR_MS)}")
            return "📥 <b>Pending</b>\n" + "\n".join(out)
        if cmd == "/open":
            rows = self.store.by_status("open")
            if not rows:
                return "No open trades."
            prices = await self.client.prices() if self.client else {}
            out = []
            for st in rows:
                p = prices.get(st.symbol)
                ur = f" · now {(p - st.entry) / (st.entry - st.sl):+.2f}R" if p else ""
                out.append(f"• {f.coin(st.symbol)}: entry <code>{f.px(st.symbol, st.entry)}</code> SL "
                           f"<code>{f.px(st.symbol, st.sl)}</code> TP <code>{f.px(st.symbol, st.tp)}</code>{ur}")
            return "📈 <b>Open</b>\n" + "\n".join(out)
        if cmd == "/stats":
            days = int(args[0]) if args and args[0].isdigit() else 0
            since = self.client.now_ms() - days * 86400_000 if (days and self.client) else 0
            rows = self.store.closed(since)
            if not rows:
                return "No closed trades yet."
            rs = [r.r_net for r in rows]
            wins = sum(1 for r in rs if r > 0)
            z = M.sizing(self.cfg, 1, 0.05)
            return (f"📊 <b>Paper results{f' (last {days}d)' if days else ''}</b>\n"
                    f"Trades {len(rs)} · win rate {wins / len(rs) * 100:.0f}% · avg {sum(rs) / len(rs):+.2f}R · "
                    f"total {sum(rs):+.1f}R ≈ ${sum(rs) * z['risk_usd']:+.2f} at ${z['risk_usd']:.2f} risk/trade\n"
                    f"TP {sum(1 for r in rows if r.exit_reason == 'TP')} · SL {sum(1 for r in rows if r.exit_reason == 'SL')} · "
                    f"time {sum(1 for r in rows if r.exit_reason == 'TIME')}")
        if cmd == "/rules":
            c = self.cfg
            return (f"<b>Rules</b>\n1. Pump ≥{c.pump_min*100:.0f}% from the 7-day low\n2. Flush ≥{c.dump_min*100:.0f}% from the high\n"
                    f"3. Flush candle ≥{c.flush_atr_min}×ATR({c.atr_len})\n4. Within {c.reclaim_window_h} candles, a 1h close above the 1h EMA{c.ema_len}\n"
                    f"5. Buy limit at the EMA{c.ema_len}, moved hourly, valid {c.order_valid_h}h\n6. SL = entry − {c.atr_mult}×ATR (skip if >{c.max_stop_pct*100:.0f}%)\n"
                    f"7. TP = {c.tp_r}R · time exit after {c.max_hold_h}h\n8. One trade per coin")
        return "Unknown command. /help"
