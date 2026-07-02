import csv
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import monitor


class MonitorTests(unittest.TestCase):
    def test_market_hours_weekday_open(self):
        now = datetime(2026, 7, 2, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertTrue(monitor.is_market_hours(now))

    def test_market_hours_weekend_closed(self):
        now = datetime(2026, 7, 4, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(monitor.is_market_hours(now))

    def test_load_watchlist_normalizes_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "watchlist.yaml"
            path.write_text("tickers: [aapl, MSFT, AAPL]\n", encoding="utf-8")
            self.assertEqual(monitor.load_watchlist(path), ["AAPL", "MSFT"])

    def test_add_indicators_adds_required_columns(self):
        data = pd.DataFrame(
            {
                "High": [11, 12, 13, 14, 15],
                "Low": [9, 10, 11, 12, 13],
                "Close": [10, 11, 12, 13, 14],
                "Volume": [100, 120, 140, 160, 180],
            }
        )
        result = monitor.add_indicators(data)
        for column in ["VWAP", "RSI", "EMA20", "EMA50", "ATR", "RelativeVolume"]:
            self.assertIn(column, result.columns)
        self.assertFalse(pd.isna(result.iloc[-1]["VWAP"]))

    def test_decide_signal_buy(self):
        latest = pd.Series(
            {"Close": 105, "VWAP": 100, "RSI": 60, "EMA20": 103, "EMA50": 101, "ATR": 2, "RelativeVolume": 1.5}
        )
        decision, _ = monitor.decide_signal(latest)
        self.assertEqual(decision, "BUY")

    def test_append_journal_writes_header_and_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "journal.csv"
            monitor.append_journal(path, {"ticker": "AAPL", "decision": "HOLD"})
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["ticker"], "AAPL")
            self.assertEqual(rows[0]["decision"], "HOLD")


if __name__ == "__main__":
    unittest.main()
