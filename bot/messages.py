"""Telegram message text and position-size suggestions."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape

HOUR_MS = 3_600_000


class Fmt:
    def __init__(self, cfg, ticks: dict[str, str] | None = None):
        self.cfg = cfg
        self.ticks = ticks or {}
        self.tz = timezone(timedelta(hours=cfg.tz_offset_hours))

    def px(self, symbol: str, v: float) -> str:
        tick = self.ticks.get(symbol)
        if tick:
            d = max(0, -Decimal(tick).normalize().as_tuple().exponent)
        else:
            a = abs(v)
            d = 2 if a >= 1000 else 4 if a >= 1 else 6 if a >= 0.01 else 8
        return f"{v:.{d}f}"

    def t(self, ms: int, with_date: bool = True) -> str:
        dt = datetime.fromtimestamp(ms / 1000, tz=self.tz)
        return (dt.strftime("%b %d, %H:%M") if with_date else dt.strftime("%H:%M")) + f" {self.cfg.tz_label}"

    @staticmethod
    def coin(symbol: str) -> str:
        return symbol[:-4] if symbol.endswith("USDT") else symbol

    def links(self, symbol: str) -> str:
        return (f'<a href="https://www.binance.com/en/futures/{symbol}">Binance</a> · '
                f'<a href="https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}.P">TradingView</a>')


def sizing(cfg, entry: float, risk_frac: float) -> dict:
    """Position size from a fixed $ risk, and the highest leverage that keeps liquidation beyond the SL."""
    risk_usd = cfg.risk_usd if cfg.risk_usd > 0 else cfg.account_usd * cfg.risk_pct / 100
    position = risk_usd / risk_frac if risk_frac > 0 else 0.0
    safe_lev = math.floor(100 / (risk_frac * 100 + cfg.liq_buffer_pct)) if risk_frac > 0 else 1
    lev = max(1, min(cfg.max_leverage, safe_lev))
    return {"risk_usd": risk_usd, "position": position, "qty": position / entry if entry else 0,
            "lev": lev, "margin": position / lev, "tp_usd": risk_usd * cfg.tp_r}


def _size_lines(f: Fmt, entry: float, risk: float) -> str:
    z = sizing(f.cfg, entry, risk)
    return (f"💵 Risk <b>${z['risk_usd']:.2f}</b> → position <b>${z['position']:.0f}</b> "
            f"({z['qty']:.4g} coins), <b>{z['lev']}x</b> → margin ${z['margin']:.2f}\n"
            f"   SL = −${z['risk_usd']:.2f} · TP = +${z['tp_usd']:.2f}")


def _ctx(f: Fmt, sym: str, pump: float, dd: float, flush_x: float, high: float, low: float) -> str:
    return (f"Pump +{pump*100:.0f}% → top {f.px(sym, high)} · flush −{dd*100:.0f}% to {f.px(sym, low)} · "
            f"flush candle {flush_x:.1f}× ATR")


def watch(f: Fmt, sym: str, a, now_ms: int) -> str:
    return (f"👀 <b>WATCH · {f.coin(sym)}</b>\n"
            f"{_ctx(f, sym, a.pump, a.dd, a.flush_x, a.hi, a.low)}\n"
            f"Waiting for a 1h close above the 1h EMA200 (<code>{f.px(sym, a.ema_level)}</code>) "
            f"within the next {a.candles_left} candle(s).\n{f.links(sym)}")


def early(f: Fmt, sym: str, sig, price: float, ema_prev: float, level: float, sl: float, tp: float, risk: float,
          close_ms: int, now_ms: int) -> str:
    mins = max(0, round((close_ms - now_ms) / 60000))
    return (f"⏳ <b>EARLY · {f.coin(sym)}</b> · candle closes in ~{mins} min ({f.t(close_ms, False)})\n"
            f"Price <code>{f.px(sym, price)}</code> is {((price/ema_prev)-1)*100:+.2f}% above the 1h EMA200 "
            f"(<code>{f.px(sym, ema_prev)}</code>). If the candle closes here, the setup triggers.\n"
            f"Provisional plan: buy limit ≈ <code>{f.px(sym, level)}</code> · SL ≈ <code>{f.px(sym, sl)}</code> "
            f"(−{risk*100:.1f}%) · TP ≈ <code>{f.px(sym, tp)}</code>\n"
            f"{_ctx(f, sym, sig.pump, sig.dd, sig.flush_x, sig.hi, sig.low)}\n"
            f"Don't enter yet. Wait for the close confirmation.\n{f.links(sym)}")


def early_cancel(f: Fmt, sym: str, close: float, ema_v: float) -> str:
    return (f"✖️ <b>EARLY CANCELLED · {f.coin(sym)}</b>\n"
            f"The candle closed at <code>{f.px(sym, close)}</code>, not above the 1h EMA200 "
            f"(<code>{f.px(sym, ema_v)}</code>). No trigger.")


def trigger(f: Fmt, sym: str, st, sig, est_sl: float, est_tp: float, est_risk: float, cap_note: str = "") -> str:
    head = "⏸ <b>TRIGGER (paused) · " if st.paused else "🚨 <b>TRIGGER · "
    body = (f"{head}{f.coin(sym)}</b> · confirmed {f.t(st.trigger_t + HOUR_MS)}\n"
            f"{_ctx(f, sym, sig.pump, sig.dd, sig.flush_x, sig.hi, sig.low)}\n\n"
            f"📥 <b>Buy limit:</b> <code>{f.px(sym, st.level)}</code> (1h EMA200; I'll tell you when it moves)\n"
            f"🛑 <b>SL (est.):</b> <code>{f.px(sym, est_sl)}</code> (−{est_risk*100:.1f}%, 1.5×ATR)\n"
            f"🎯 <b>TP (est.):</b> <code>{f.px(sym, est_tp)}</code> (+{est_risk*f.cfg.tp_r*100:.1f}%, {f.cfg.tp_r}R)\n"
            f"⌛ Order valid until {f.t(st.expires_t + HOUR_MS)}\n")
    if st.paused:
        body += (f"⚠️ The stop would be {est_risk*100:.1f}%, wider than {f.cfg.max_stop_pct*100:.0f}%. "
                 f"Don't place the order yet; I'll tell you if it becomes valid.\n")
    else:
        body += _size_lines(f, st.level, est_risk) + "\n"
    body += "Exact SL/TP are sent when the order fills.\n" + cap_note + f.links(sym)
    return body


def level_update(f: Fmt, st, est_sl: float, est_tp: float, est_risk: float, was_paused: bool) -> str:
    sym = st.symbol
    if st.paused:
        return (f"⏸ <b>PAUSE · {f.coin(sym)}</b> · stop would be {est_risk*100:.1f}% (> {f.cfg.max_stop_pct*100:.0f}%). "
                f"Remove the buy limit for this hour.")
    tag = "▶️ <b>RESUME" if was_paused else "🔁 <b>LEVEL"
    return (f"{tag} · {f.coin(sym)}</b> · move the buy limit to <code>{f.px(sym, st.level)}</code> "
            f"(SL ≈ <code>{f.px(sym, est_sl)}</code>, TP ≈ <code>{f.px(sym, est_tp)}</code>, −{est_risk*100:.1f}%) · "
            f"valid until {f.t(st.expires_t + HOUR_MS)}")


def filled(f: Fmt, st, fill_ms: int) -> str:
    sym = st.symbol
    return (f"✅ <b>FILLED · {f.coin(sym)}</b> · {f.t(fill_ms)}\n"
            f"Entry <code>{f.px(sym, st.entry)}</code>\n"
            f"🛑 <b>SL</b> <code>{f.px(sym, st.sl)}</code> (−{st.risk*100:.2f}%)\n"
            f"🎯 <b>TP</b> <code>{f.px(sym, st.tp)}</code> (+{st.risk*f.cfg.tp_r*100:.2f}%)\n"
            f"⏱ Close at market if neither is hit by {f.t(st.fill_t + (f.cfg.max_hold_h + 1) * HOUR_MS)}\n"
            + _size_lines(f, st.entry, st.risk) + f"\n{f.links(sym)}")


def closed(f: Fmt, st) -> str:
    sym = st.symbol
    icon = {"TP": "🎯", "SL": "🛑", "TIME": "⏱"}.get(st.exit_reason, "•")
    word = {"TP": "TP HIT", "SL": "SL HIT", "TIME": "7-DAY TIME EXIT"}.get(st.exit_reason, st.exit_reason)
    z = sizing(f.cfg, st.entry, st.risk)
    pnl = st.r_net * z["risk_usd"]
    return (f"{icon} <b>{word} · {f.coin(sym)}</b>\n"
            f"Entry <code>{f.px(sym, st.entry)}</code> → exit <code>{f.px(sym, st.exit_price)}</code> · "
            f"<b>{st.r_net:+.2f}R</b> (≈ {'+' if pnl >= 0 else '−'}${abs(pnl):.2f} at your risk size, after costs)")


def expired(f: Fmt, st) -> str:
    return f"⌛ <b>EXPIRED · {f.coin(st.symbol)}</b> · no fill within {f.cfg.order_valid_h}h. Cancel the buy limit."


def skipped(f: Fmt, st, risk: float) -> str:
    return (f"⏭ <b>SKIPPED · {f.coin(st.symbol)}</b> · price reached the level while the stop was "
            f"{risk*100:.1f}% (> {f.cfg.max_stop_pct*100:.0f}%). Setup closed; no trade.")
