"""Configuration loaded from a .env file (and/or real environment variables).

Every value has a default that matches the backtested "Reclaim-Retest v2" rules,
so only the Telegram settings are strictly required.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Minimal .env reader (no external dependency). Real env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if val.startswith("#"):
            val = ""
        elif " #" in val or "\t#" in val:  # inline comment
            val = val.replace("\t#", " #").split(" #", 1)[0].strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ.setdefault(key, val)


def _f(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


def _i(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(float(v)) if v not in (None, "") else default


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v in (None, ""):
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


def _list(name: str) -> list[str]:
    v = os.getenv(name, "")
    return [s.strip().upper() for s in v.split(",") if s.strip()]


@dataclass
class Config:
    # --- Telegram ---
    telegram_token: str = ""
    telegram_chat_id: str = ""
    telegram_commands: bool = True

    # --- Strategy (defaults = backtested v2 rules) ---
    pump_min: float = 0.40            # high >= 40% above prior 7-day low
    pump_lookback_h: int = 120        # the high is the max of the last 120 1h candles
    base_lookback_h: int = 168        # the low is the min of the 168 candles before that high
    dump_min: float = 0.25            # flush low >= 25% below the high
    flush_atr_min: float = 2.0        # flush candle range >= 2 x ATR(14)
    reclaim_window_h: int = 8         # reclaim must come within 8 candles of the flush low
    ema_len: int = 200                # 1h EMA used for the reclaim and the entry
    atr_len: int = 14
    atr_mult: float = 1.5             # SL = entry - 1.5 x ATR
    tp_r: float = 1.5                 # TP = entry + 1.5R
    max_stop_pct: float = 0.12        # skip if SL is further than 12%
    min_stop_pct: float = 0.004
    order_valid_h: int = 48           # cancel the limit after 48h
    max_hold_h: int = 168             # close at market after 7 days
    cost_pct: float = 0.0010          # round-trip fees+slippage used for R stats

    # --- Early alerts ---
    early_enabled: bool = True
    early_start_min: int = 10         # start checking 10 min before the 1h close
    early_end_min: int = 3            # stop checking 3 min before the close
    early_buffer_pct: float = 0.003   # price must be >= EMA200 x (1 + 0.3%) to alert early
    early_poll_sec: int = 20

    # --- Alerts on/off ---
    alert_watch: bool = True
    alert_early: bool = True
    alert_trigger: bool = True
    alert_level_update: bool = True
    alert_fill: bool = True
    alert_exit: bool = True
    alert_expire: bool = True
    alert_skip: bool = False
    level_update_min_pct: float = 0.005  # only send a LEVEL update when the limit moved >= 0.5%

    # --- Risk / sizing (only used to print suggestions) ---
    account_usd: float = 1000.0
    risk_pct: float = 1.0             # % of account risked per trade
    risk_usd: float = 0.0             # if > 0, overrides risk_pct
    max_leverage: int = 10
    liq_buffer_pct: float = 2.0       # keep liquidation at least SL% + 2% away
    max_open_trades: int = 0          # 0 = no cap (alerts only)

    # --- Universe ---
    quote_asset: str = "USDT"
    min_quote_volume_24h: float = 0.0  # 0 = off (backtest used no volume filter)
    include_symbols: list[str] = field(default_factory=list)
    exclude_symbols: list[str] = field(default_factory=list)
    min_history_h: int = 400
    universe_refresh_h: int = 6       # re-read the coin list (new listings / delistings) every N hours

    # --- Runtime ---
    monitor_poll_sec: int = 15
    history_bars: int = 1000
    close_delay_sec: int = 5          # wait after the hour before reading the closed candle
    tz_offset_hours: float = 5.0      # time shown in messages (5 = PKT)
    tz_label: str = "PKT"
    db_path: str = "reclaim_bot.sqlite3"
    log_level: str = "INFO"
    binance_base: str = "https://fapi.binance.com"
    max_weight_per_min: int = 1800

    @classmethod
    def from_env(cls, env_file: str | os.PathLike = ".env") -> "Config":
        load_dotenv(env_file)
        c = cls()
        c.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        c.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        c.telegram_commands = _b("TELEGRAM_COMMANDS", c.telegram_commands)

        c.pump_min = _f("PUMP_MIN_PCT", c.pump_min * 100) / 100
        c.pump_lookback_h = _i("PUMP_LOOKBACK_H", c.pump_lookback_h)
        c.base_lookback_h = _i("BASE_LOOKBACK_H", c.base_lookback_h)
        c.dump_min = _f("DUMP_MIN_PCT", c.dump_min * 100) / 100
        c.flush_atr_min = _f("FLUSH_ATR_MIN", c.flush_atr_min)
        c.reclaim_window_h = _i("RECLAIM_WINDOW_H", c.reclaim_window_h)
        c.ema_len = _i("EMA_LEN", c.ema_len)
        c.atr_len = _i("ATR_LEN", c.atr_len)
        c.atr_mult = _f("ATR_MULT", c.atr_mult)
        c.tp_r = _f("TP_R", c.tp_r)
        c.max_stop_pct = _f("MAX_STOP_PCT", c.max_stop_pct * 100) / 100
        c.min_stop_pct = _f("MIN_STOP_PCT", c.min_stop_pct * 100) / 100
        c.order_valid_h = _i("ORDER_VALID_H", c.order_valid_h)
        c.max_hold_h = _i("MAX_HOLD_H", c.max_hold_h)
        c.cost_pct = _f("COST_PCT", c.cost_pct * 100) / 100

        c.early_enabled = _b("EARLY_ENABLED", c.early_enabled)
        c.early_start_min = _i("EARLY_START_MIN", c.early_start_min)
        c.early_end_min = _i("EARLY_END_MIN", c.early_end_min)
        c.early_buffer_pct = _f("EARLY_BUFFER_PCT", c.early_buffer_pct * 100) / 100
        c.early_poll_sec = _i("EARLY_POLL_SEC", c.early_poll_sec)

        for name in ("watch", "early", "trigger", "level_update", "fill", "exit", "expire", "skip"):
            setattr(c, f"alert_{name}", _b(f"ALERT_{name.upper()}", getattr(c, f"alert_{name}")))

        c.level_update_min_pct = _f("LEVEL_UPDATE_MIN_PCT", c.level_update_min_pct * 100) / 100
        c.account_usd = _f("ACCOUNT_USD", c.account_usd)
        c.risk_pct = _f("RISK_PCT", c.risk_pct)
        c.risk_usd = _f("RISK_USD", c.risk_usd)
        c.max_leverage = _i("MAX_LEVERAGE", c.max_leverage)
        c.liq_buffer_pct = _f("LIQ_BUFFER_PCT", c.liq_buffer_pct)
        c.max_open_trades = _i("MAX_OPEN_TRADES", c.max_open_trades)

        c.quote_asset = os.getenv("QUOTE_ASSET", c.quote_asset).upper()
        c.min_quote_volume_24h = _f("MIN_QUOTE_VOLUME_24H", c.min_quote_volume_24h)
        c.include_symbols = _list("INCLUDE_SYMBOLS")
        c.exclude_symbols = _list("EXCLUDE_SYMBOLS")
        c.min_history_h = _i("MIN_HISTORY_H", c.min_history_h)
        c.universe_refresh_h = max(1, _i("UNIVERSE_REFRESH_H", c.universe_refresh_h))

        c.monitor_poll_sec = _i("MONITOR_POLL_SEC", c.monitor_poll_sec)
        c.history_bars = min(1500, max(400, _i("HISTORY_BARS", c.history_bars)))
        c.close_delay_sec = _i("CLOSE_DELAY_SEC", c.close_delay_sec)
        c.tz_offset_hours = _f("TZ_OFFSET_HOURS", c.tz_offset_hours)
        c.tz_label = os.getenv("TZ_LABEL", c.tz_label)
        c.db_path = os.getenv("DB_PATH", c.db_path)
        c.log_level = os.getenv("LOG_LEVEL", c.log_level).upper()
        c.binance_base = os.getenv("BINANCE_BASE", c.binance_base).rstrip("/")
        c.max_weight_per_min = _i("MAX_WEIGHT_PER_MIN", c.max_weight_per_min)
        c.validate()
        return c

    def validate(self) -> None:
        if self.early_end_min >= self.early_start_min:
            raise ValueError("EARLY_END_MIN must be smaller than EARLY_START_MIN (e.g. start 10, end 3)")
        if not (0 < self.early_start_min < 60):
            raise ValueError("EARLY_START_MIN must be between 1 and 59")
        if self.history_bars < self.pump_lookback_h + self.base_lookback_h + 50:
            raise ValueError("HISTORY_BARS too small for the pump/base lookbacks")
