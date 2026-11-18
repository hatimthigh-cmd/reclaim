# Reclaim-Retest v2 alert bot

Scans every Binance USDT-M perpetual on the 1h chart for the backtested **Reclaim-Retest v2** long setup and sends Telegram alerts:
- **Early:** 5–10 minutes before the trigger candle closes.
- **Confirmed:** the buy-limit price, SL, TP and position size.
- **Follow-up:** fill, TP/SL hit, time exit and expiry alerts.

It only **reads public market data**. It never logs in to Binance and never places orders.

## The rules it follows
1. **Pump:** price is ≥40% above its 7-day low, at a high made within the last 5 days.
2. **Flush:** price drops ≥25% from that high. The lowest candle is the flush candle.
3. **Capitulation:** the flush candle's range (high − low) is ≥ 2 × ATR(14).
4. **Trigger:** within 8 candles of the flush low, a 1h candle closes back above the 1h EMA200, with no lower low in between.
5. **Entry:** a buy limit at the 1h EMA200. It follows the EMA and is cancelled after 48h.
6. **SL** = entry − 1.5 × ATR(14). The trade is skipped if the stop is more than 12% away.
7. **TP** = entry + 1.5 × (entry − SL), i.e. 1.5R. Close at market after 7 days if neither is hit.
8. One trade per coin.

**Verified on real data:** on the real Binance candles of 185 coins, the bot reproduced all 195 v2 backtest trades, with no missing or extra trades. That holds both on the full history and in live mode (hour by hour, 1,000-candle window). It also reproduced all 690 original-rule trades on those coins.

Backtest, Oct 2025 – Nov 2026, 520 coins: about 3–4 trades a week, a 49% win rate, about +0.21R per trade after costs, and losing runs of up to 10. The bot uses the same code logic as that backtest: its detection and trade handling were checked against it trade for trade on test data. Past results don't guarantee future ones. Paper-trade first.

## Alerts you'll receive

| Alert | Meaning | What to do |
|---|---|---|
| 👀 WATCH | Pump, flush and a capitulation candle are done. Waiting for the 1h close above the EMA200. | Nothing yet. Open the chart if you like. |
| ⏳ EARLY | 3–10 min before the close, price is above the EMA200. If the candle closes there, it triggers. | Get ready. **Don't enter yet.** |
| ✖️ EARLY CANCELLED | The candle closed back below the EMA200. | Nothing. |
| 🚨 TRIGGER | Confirmed. Shows the buy-limit price, estimated SL/TP and a suggested position size and leverage. | Place the buy limit. |
| ⏸ TRIGGER (paused) / PAUSE | The stop would currently be wider than 12%. | Don't hold the order this hour. |
| 🔁 LEVEL / ▶️ RESUME | The EMA200 moved by ≥0.5%, or the order is valid again. | Move the buy limit to the new price. |
| ✅ FILLED | Price reached the limit. Shows the **exact SL and TP**. | Set the SL and TP orders. |
| 🎯 TP HIT / 🛑 SL HIT / ⏱ TIME EXIT | The trade closed. Shows the result in R and $. | For a time exit, close at market. |
| ⌛ EXPIRED | No fill within 48h. | Cancel the buy limit. |

Telegram commands (only answered for your chat ID): `/status`, `/watch`, `/pending`, `/open`, `/stats [days]`, `/rules`, `/help`.

## Setup on Windows (about 10 minutes)
1. **Install Python 3.10 or newer** from python.org. Tick "Add Python to PATH" during setup. No other packages are needed.
2. **Create a Telegram bot.** Open @BotFather in Telegram, send `/newbot`, and copy the token. Send any message to your new bot, then get your chat ID from @userinfobot.
3. **Configure.** In the bot folder, copy `.env.example` to `.env`. Open it in Notepad and fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Optionally set `ACCOUNT_USD` and `RISK_PCT` (or `RISK_USD`) for the position-size suggestions.
4. **Test Telegram.** Open a terminal in the folder and run `python main.py test-telegram`.
5. **Run.** Double-click `run_bot.bat`, or run `python main.py run`. The first start loads 1,000 hourly candles for every coin, which takes about 2–4 minutes. Keep the window open; the PC must stay on and online.

The same steps work on Linux, macOS or a VPS: run `python3 main.py run`, ideally under `systemd`, `screen` or `tmux`.

## Other commands
```
python main.py scan                 # one-off: list coins that are armed now and triggers from the last 48h
python main.py backtest ARIAUSDT    # replay the rules on ~3000 recent candles of one or more coins
python main.py backtest ARIA BTW JCT --bars 4000
python -m pytest tests              # parity and unit tests (needs pytest; node for the JS parity test)
python tests/verify_real.py backtest_candles_v2.bin.gz   # re-run the real-data check against the 195 backtest trades
```

## How it works
- **Hourly** (5 seconds after each hour): pulls the newest closed candles for every coin and runs the rules. Then it updates pending orders, fills, exits and expiries using the closed candle, exactly as the backtest does.
- **Early window** (from `EARLY_START_MIN` to `EARLY_END_MIN` before each close): every `EARLY_POLL_SEC` seconds it checks only the armed coins. It tests the forming candle as if it closed at the current price, and alerts as soon as the trigger condition holds with the `EARLY_BUFFER_PCT` margin.
- **Price monitor** (every `MONITOR_POLL_SEC` seconds): watches live prices for fills and SL/TP hits, so those alerts come within seconds. Closed candles are re-checked every hour, so wicks between checks aren't missed.
- **Memory:** everything is stored in `reclaim_bot.sqlite3`, so a restart keeps pending orders and open trades. After downtime it catches up on up to 6 missed candles. Logs go to `reclaim_bot.log`.
- **Coin list:** re-read every `UNIVERSE_REFRESH_H` hours (default 6), so new listings are added and delisted coins dropped.
- **Rate limits:** stays well under Binance's limits and backs off automatically if limited.

## Things to know
- **Position size is a suggestion** based on a fixed $ risk: position = $ risk ÷ stop %. The suggested leverage keeps liquidation at least `stop % + LIQ_BUFFER_PCT` away. Don't use high fixed leverage: at 20x, most stops would be liquidated first.
- **Level updates:** the backtest moves the limit to the EMA200 every hour. To avoid spam, the bot only tells you when it moves by ≥0.5% (`LEVEL_UPDATE_MIN_PCT`), so your order can be up to 0.5% off the exact level.
- **Live vs backtest exits:** the backtest assumes the worst when one candle touches both SL and TP. The live monitor sees the real order of prices, so results can differ slightly in your favour or against you.
- **`MIN_QUOTE_VOLUME_24H`:** the backtest used no volume filter. Thin coins can slip more than the 0.10% cost assumed.
- **`/stats` is a paper record** of every alerted setup. Compare it with your real trades.
