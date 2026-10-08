"""Temporary small SQLite only; never copies or mutates the live Claw database."""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import date

spec = importlib.util.spec_from_file_location("calendar_reader", Path(__file__).with_name("calendar_reader.py"))
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


class CalendarReaderTests(unittest.TestCase):
    def test_read_only_query_and_bounded_projection(self):
        with tempfile.TemporaryDirectory(prefix="claw-calendar-test-") as directory:
            db = Path(directory) / "calendar.sqlite"
            with sqlite3.connect(db) as c:
                c.execute("CREATE TABLE trade_calendar (trade_date TEXT PRIMARY KEY, is_trade_day INTEGER)")
                c.executemany("INSERT INTO trade_calendar VALUES (?,?)", [
                    ("2026-09-30", 1), ("2026-10-01", 0), ("2026-10-08", 1), ("2028-01-01", 0)])
            original = db.read_bytes()
            result = reader.read_calendar(db, date(2026, 10, 1))
            self.assertEqual(result["rows"], [["2026-10-01", False], ["2026-10-08", True]])
            self.assertTrue(result["read_only"])
            self.assertEqual(len(result["sha256"]), 64)
            self.assertEqual(db.read_bytes(), original)

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory(prefix="claw-calendar-test-") as directory:
            db = Path(directory) / "absent.sqlite"
            with self.assertRaises(FileNotFoundError):
                reader.read_calendar(db, date(2026, 10, 1))
            self.assertFalse(db.exists())

    def test_invalid_flag_rejected(self):
        with tempfile.TemporaryDirectory(prefix="claw-calendar-test-") as directory:
            db = Path(directory) / "calendar.sqlite"
            with sqlite3.connect(db) as c:
                c.execute("CREATE TABLE trade_calendar (trade_date TEXT PRIMARY KEY, is_trade_day INTEGER)")
                c.execute("INSERT INTO trade_calendar VALUES ('2026-10-01',2)")
            with self.assertRaises(ValueError):
                reader.read_calendar(db, date(2026, 10, 1))


if __name__ == "__main__":
    unittest.main()
