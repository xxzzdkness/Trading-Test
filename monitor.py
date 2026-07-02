#!/usr/bin/env python3
"""Trading signal monitor.

This program evaluates a YAML watchlist during regular US market hours,
records every decision to a CSV journal, and sends Telegram notifications.
It is intentionally read-only: it never places trades or connects to a broker.
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yaml
import yfinance as yf
from dotenv import load_dotenv

EASTERN = ZoneInfo("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
DEFAULT_WATCHLIST_FILE = "watchlist.yaml"
DEFAULT_JOURNAL_FILE = "journal.csv"
DEFAULT_DATA_INTERVAL = "5m"
DEFAULT_LOOKBACK_PERIOD = "5d"
DEFAULT_RUN_EVERY_MINUTES = 30
SUPPORTED_INTERVALS = {"1m", "5m"}
JOURNAL_COLUMNS = [
    "timestamp_et",
    "ticker",
    "decision",
    "price",
    "vwap",
    "rsi",
    "ema20",
    "ema50",
    "atr",
    "relative_volume",
    "reason",
    "news_headlines",
]


@dataclass(frozen=True)
class Settings:
    watchlist_file: Path
    journal_file: Path
    data_interval: str
    lookback_period: str
    run_every_minutes: int
    telegram_bot_token: str
    telegram_chat_id: str
    newsapi_key: str

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables after .env has been loaded."""
        interval = os.getenv("DATA_INTERVAL", DEFAULT_DATA_INTERVAL).strip()
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"DATA_INTERVAL must be one of {sorted(SUPPORTED_INTERVALS)}")

        run_every = int(os.getenv("RUN_EVERY_MINUTES", str(DEFAULT_RUN_EVERY_MINUTES)))
        if run_every <= 0:
            raise ValueError("RUN_EVERY_MINUTES must be greater than zero")

        return cls(
            watchlist_file=Path(os.getenv("WATCHLIST_FILE", DEFAULT_WATCHLIST_FILE)),
            journal_file=Path(os.getenv("JOURNAL_FILE", DEFAULT_JOURNAL_FILE)),
            data_interval=interval,
            lookback_period=os.getenv("LOOKBACK_PERIOD", DEFAULT_LOOKBACK_PERIOD).strip(),
            run_every_minutes=run_every,
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            newsapi_key=os.getenv("NEWSAPI_KEY", "").strip(),
        )


def is_market_hours(now: datetime | None = None) -> bool:
    """Return True only during regular US equity market hours on weekdays."""
    current = now.astimezone(EASTERN) if now else datetime.now(EASTERN)
    return current.weekday() < 5 and MARKET_OPEN <= current.time() <= MARKET_CLOSE


def load_watchlist(path: Path) -> list[str]:
    """Load tickers from watchlist.yaml.

    Supported formats:
    - tickers: [AAPL, MSFT]
    - watchlist: [AAPL, MSFT]
    - [AAPL, MSFT]
    """
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    if isinstance(config, dict):
        raw_tickers = config.get("tickers") or config.get("watchlist") or []
    elif isinstance(config, list):
        raw_tickers = config
    else:
        raw_tickers = []

    tickers = sorted({str(ticker).strip().upper() for ticker in raw_tickers if str(ticker).strip()})
    if not tickers:
        raise ValueError(f"No tickers found in {path}")
    return tickers


def fetch_ohlcv(ticker: str, interval: str, period: str) -> pd.DataFrame:
    """Fetch intraday OHLCV candles from Yahoo Finance."""
    data = yf.download(
        ticker,
        interval=interval,
        period=period,
        progress=False,
        auto_adjust=False,
        prepost=False,
        threads=False,
    )
    if data.empty:
        raise ValueError(f"No OHLCV data returned for {ticker}")
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    return data.dropna(subset=["High", "Low", "Close", "Volume"])


def add_indicators(data: pd.DataFrame) -> pd.DataFrame:
    """Add VWAP, RSI, EMA20, EMA50, ATR, and relative volume columns."""
    frame = data.copy()
    typical_price = (frame["High"] + frame["Low"] + frame["Close"]) / 3
    volume = frame["Volume"].replace(0, pd.NA)
    cumulative_volume = frame["Volume"].cumsum().replace(0, pd.NA)
    frame["VWAP"] = (typical_price * frame["Volume"]).cumsum() / cumulative_volume

    delta = frame["Close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    frame["RSI"] = (100 - (100 / (1 + rs))).fillna(50)

    frame["EMA20"] = frame["Close"].ewm(span=20, adjust=False).mean()
    frame["EMA50"] = frame["Close"].ewm(span=50, adjust=False).mean()

    previous_close = frame["Close"].shift(1)
    true_range = pd.concat(
        [
            frame["High"] - frame["Low"],
            (frame["High"] - previous_close).abs(),
            (frame["Low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["ATR"] = true_range.rolling(14, min_periods=1).mean()
    frame["RelativeVolume"] = frame["Volume"] / volume.rolling(20, min_periods=1).mean()
    return frame


def fetch_recent_news(ticker: str, api_key: str, limit: int = 3) -> list[str]:
    """Fetch recent company news headlines when NEWSAPI_KEY is configured."""
    if not api_key:
        return []
    response = requests.get(
        "https://newsapi.org/v2/everything",
        params={
            "q": ticker,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": limit,
            "apiKey": api_key,
        },
        timeout=10,
    )
    response.raise_for_status()
    articles = response.json().get("articles", [])
    return [article.get("title", "").strip() for article in articles if article.get("title")]


def decide_signal(latest: pd.Series) -> tuple[str, str]:
    """Return BUY, HOLD, SELL, or WAIT with a concise reason."""
    required = ["Close", "VWAP", "RSI", "EMA20", "EMA50", "ATR", "RelativeVolume"]
    if any(pd.isna(latest.get(column)) for column in required):
        return "WAIT", "Insufficient indicator history"

    close = float(latest["Close"])
    vwap = float(latest["VWAP"])
    rsi = float(latest["RSI"])
    ema20 = float(latest["EMA20"])
    ema50 = float(latest["EMA50"])
    relvol = float(latest["RelativeVolume"])

    if close > vwap and ema20 > ema50 and 45 <= rsi <= 70 and relvol >= 1.2:
        return "BUY", "Bullish trend above VWAP with confirming relative volume"
    if close < vwap and ema20 < ema50 and rsi < 55 and relvol >= 1.2:
        return "SELL", "Bearish trend below VWAP with confirming relative volume"
    if rsi > 75 or rsi < 25:
        return "WAIT", "RSI is extended; waiting for a cleaner setup"
    return "HOLD", "No high-conviction BUY or SELL setup"


def append_journal(path: Path, row: dict[str, object]) -> None:
    """Append one decision row to journal.csv, creating headers when needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=JOURNAL_COLUMNS)
        if not exists:
            writer.writeheader()
        writer.writerow({column: row.get(column, "") for column in JOURNAL_COLUMNS})


def send_telegram(message: str, settings: Settings) -> None:
    """Send a Telegram notification if bot settings are configured."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        print("Telegram not configured; skipping notification.")
        return
    response = requests.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
        json={"chat_id": settings.telegram_chat_id, "text": message, "disable_web_page_preview": True},
        timeout=10,
    )
    response.raise_for_status()


def format_float(value: object) -> str:
    return "" if pd.isna(value) else f"{float(value):.4f}"


def build_decision_row(
    ticker: str,
    latest: pd.Series,
    decision: str,
    reason: str,
    headlines: list[str],
) -> dict[str, object]:
    """Build one journal-compatible decision row."""
    return {
        "timestamp_et": datetime.now(EASTERN).isoformat(timespec="seconds"),
        "ticker": ticker,
        "decision": decision,
        "price": format_float(latest["Close"]),
        "vwap": format_float(latest["VWAP"]),
        "rsi": format_float(latest["RSI"]),
        "ema20": format_float(latest["EMA20"]),
        "ema50": format_float(latest["EMA50"]),
        "atr": format_float(latest["ATR"]),
        "relative_volume": format_float(latest["RelativeVolume"]),
        "reason": reason,
        "news_headlines": " | ".join(headlines),
    }


def monitor_once(settings: Settings, force: bool = False) -> list[dict[str, object]]:
    """Evaluate all watchlist tickers once and persist decisions."""
    if not force and not is_market_hours():
        print("Outside US market hours; no decisions generated. Use --force to run manually anyway.")
        return []

    decisions: list[dict[str, object]] = []
    for ticker in load_watchlist(settings.watchlist_file):
        try:
            data = add_indicators(fetch_ohlcv(ticker, settings.data_interval, settings.lookback_period))
            latest = data.iloc[-1]
            decision, reason = decide_signal(latest)
            headlines = fetch_recent_news(ticker, settings.newsapi_key)
            row = build_decision_row(ticker, latest, decision, reason, headlines)
            append_journal(settings.journal_file, row)
            send_telegram(
                f"{ticker}: {decision}\nPrice: {row['price']} RSI: {row['rsi']} RelVol: {row['relative_volume']}\n{reason}",
                settings,
            )
            decisions.append(row)
        except Exception as exc:  # keep other symbols running while journaling failures
            row = {
                "timestamp_et": datetime.now(EASTERN).isoformat(timespec="seconds"),
                "ticker": ticker,
                "decision": "WAIT",
                "reason": f"Data/news/notification error: {exc}",
            }
            append_journal(settings.journal_file, row)
            decisions.append(row)
            print(f"{ticker}: {exc}")
    return decisions


def sleep_until_next_run(minutes: int) -> None:
    """Sleep between scheduled checks."""
    time.sleep(max(1, minutes) * 60)


def run_forever(settings: Settings) -> None:
    """Run the monitor every configured interval."""
    while True:
        monitor_once(settings)
        sleep_until_next_run(settings.run_every_minutes)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Python trading signal monitor")
    parser.add_argument("--once", action="store_true", help="run one check and exit instead of looping forever")
    parser.add_argument("--force", action="store_true", help="allow --once to run outside US market hours")
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()
    settings = Settings.from_env()
    if args.once:
        monitor_once(settings, force=args.force)
    else:
        run_forever(settings)


if __name__ == "__main__":
    main()
