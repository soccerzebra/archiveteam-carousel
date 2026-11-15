"""StatsStore tests: SQLite persistence and the bucketed time-series read."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from controlplane.stats import StatsStore


def test_records_and_totals(tmp_path):
    db = StatsStore(tmp_path / "stats.db")
    now = 1_000_000
    db.record_item("urls", True, 1000, ts=now - 10)
    db.record_item("urls", True, 2000, ts=now - 20)
    db.record_item("reddit", False, 0, ts=now - 30)
    s = db.series(hours=1, buckets=10, now=now)
    assert s["totals"] == {"items_done": 2, "items_failed": 1, "bytes": 3000}
    assert len(s["buckets"]) == 10
    assert sum(b["bytes"] for b in s["buckets"]) == 3000


def test_window_excludes_old_events(tmp_path):
    db = StatsStore(tmp_path / "stats.db")
    now = 2_000_000
    db.record_item("urls", True, 500, ts=now - 10)  # inside 1h window
    db.record_item("urls", True, 9999, ts=now - 7200)  # 2h ago, excluded
    s = db.series(hours=1, buckets=6, now=now)
    assert s["totals"]["bytes"] == 500
    assert s["totals"]["items_done"] == 1


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "stats.db"
    db = StatsStore(path)
    now = 3_000_000
    db.record_item("urls", True, 1234, ts=now - 5)
    db.close()
    reopened = StatsStore(path)
    s = reopened.series(hours=1, buckets=4, now=now)
    assert s["totals"]["bytes"] == 1234


def test_empty_series_is_well_formed(tmp_path):
    db = StatsStore(tmp_path / "stats.db")
    s = db.series(hours=24, buckets=48, now=5_000_000)
    assert len(s["buckets"]) == 48
    assert s["totals"] == {"items_done": 0, "items_failed": 0, "bytes": 0}
    assert all(b["bytes"] == 0 for b in s["buckets"])
