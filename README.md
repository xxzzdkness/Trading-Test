# Python Trading Signal Monitor

A turnkey, read-only Python monitor that evaluates a YAML watchlist every 30 minutes during regular US equity market hours. It calculates common intraday indicators, writes every decision to `journal.csv`, and sends Telegram notifications. It **never places trades** and does not connect to a broker.

## What it does

- Runs only Monday-Friday from 9:30 AM to 4:00 PM America/New_York time.
- Reads symbols from `watchlist.yaml`.
- Pulls 1-minute or 5-minute OHLCV candles with `yfinance`.
- Calculates VWAP, RSI, EMA20, EMA50, ATR, and relative volume.
- Optionally pulls recent headlines from NewsAPI when `NEWSAPI_KEY` is configured.
- Emits `BUY`, `HOLD`, `SELL`, or `WAIT` decisions.
- Appends every decision, including recoverable errors, to `journal.csv`.
- Sends Telegram notifications when bot settings are configured.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and fill in your Telegram bot token and chat ID. News is optional.

## Watchlist

Edit `watchlist.yaml` with the tickers you want monitored:

```yaml
tickers:
  - AAPL
  - MSFT
  - NVDA
```

The loader also accepts `watchlist:` or a plain YAML list.

## Running one manual check

Use this first to verify your setup:

```bash
python monitor.py --once --force
```

`--force` lets you test outside market hours. Without `--force`, the monitor will skip decisions unless the US stock market is in its regular weekday session.

## Running continuously

```bash
python monitor.py
```

The process wakes up every 30 minutes by default. Outside market hours it skips decision generation and waits for the next cycle.

## Configuration

All settings can be edited in `.env`:

| Variable | Default | Description |
| --- | --- | --- |
| `WATCHLIST_FILE` | `watchlist.yaml` | YAML file containing tickers. |
| `JOURNAL_FILE` | `journal.csv` | CSV file where every decision is appended. |
| `DATA_INTERVAL` | `5m` | Candle interval. Supported values: `1m` or `5m`. |
| `LOOKBACK_PERIOD` | `5d` | Intraday history requested from yfinance. |
| `RUN_EVERY_MINUTES` | `30` | Delay between monitor runs. |
| `TELEGRAM_BOT_TOKEN` | empty | Telegram bot token. Notifications are skipped if absent. |
| `TELEGRAM_CHAT_ID` | empty | Telegram chat ID. Notifications are skipped if absent. |
| `NEWSAPI_KEY` | empty | Optional NewsAPI key for recent headlines. |

## Signal logic

- `BUY`: price is above VWAP, EMA20 is above EMA50, RSI is between 45 and 70, and relative volume is at least 1.2.
- `SELL`: price is below VWAP, EMA20 is below EMA50, RSI is below 55, and relative volume is at least 1.2.
- `WAIT`: indicator history is insufficient or RSI is extended above 75 or below 25.
- `HOLD`: no high-conviction setup is present.

These rules are intentionally simple and should be reviewed before relying on the output. The monitor is informational only and never submits orders.

## Testing

```bash
python -m unittest discover -s tests
python -m py_compile monitor.py
```
