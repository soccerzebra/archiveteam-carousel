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
    assert s["totals"] == {"items_done": 2, "items_failed": 1, "bytes": 3000, "bytes_down": 0}
    # `buckets` is only an upper bound: a 1h window is served from the 15-min tier, so it has
    # at most 4 slices however many are requested.
    assert len(s["buckets"]) == 4
    assert sum(b["bytes"] for b in s["buckets"]) == 3000


def test_tracks_uploaded_and_downloaded_separately(tmp_path):
    db = StatsStore(tmp_path / "stats.db")
    now = 1_500_000
    db.record_item("urls", True, 1000, bytes_down=1500, ts=now - 10)
    db.record_item("urls", True, 2000, bytes_down=2600, ts=now - 20)
    s = db.series(hours=1, buckets=10, now=now)
    assert s["totals"]["bytes"] == 3000  # uploaded
    assert s["totals"]["bytes_down"] == 4100  # downloaded
    assert sum(b["bytes_down"] for b in s["buckets"]) == 4100


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


def test_migrates_pre_download_database(tmp_path):
    import sqlite3

    path = tmp_path / "stats.db"
    old = sqlite3.connect(path)  # schema before bytes_down existed
    old.execute(
        "CREATE TABLE item_events (ts INTEGER NOT NULL, project TEXT NOT NULL, "
        "ok INTEGER NOT NULL, bytes INTEGER NOT NULL DEFAULT 0)"
    )
    old.execute(
        "INSERT INTO item_events (ts, project, ok, bytes) VALUES (?,?,?,?)", (100, "urls", 1, 700)
    )
    old.commit()
    old.close()

    db = StatsStore(path)  # should add bytes_down without losing the old row
    db.record_item("urls", True, 300, bytes_down=900, ts=150)
    s = db.series(hours=1, buckets=4, now=200)
    assert s["totals"]["bytes"] == 1000  # 700 (migrated) + 300
    assert s["totals"]["bytes_down"] == 900  # old row defaults to 0


def test_empty_series_is_well_formed(tmp_path):
    db = StatsStore(tmp_path / "stats.db")
    s = db.series(hours=24, buckets=48, now=5_000_000)
    # 24h is served from the hourly tier: 24 slices, capped below the requested 48.
    assert len(s["buckets"]) == 24
    assert s["totals"] == {"items_done": 0, "items_failed": 0, "bytes": 0, "bytes_down": 0}
    assert all(b["bytes"] == 0 for b in s["buckets"])


def test_lifetime_totals_accumulate_per_project(tmp_path):
    db = StatsStore(tmp_path / "stats.db")
    base = 10_000_000
    db.record_item("urls", True, 1000, bytes_down=1500, ts=base)
    db.record_item("urls", False, 0, ts=base + 1)
    db.record_item("reddit", True, 50, bytes_down=70, ts=base + 2)
    life = db.lifetime()
    by = {p["project"]: p for p in life["projects"]}
    assert by["urls"] == {
        "project": "urls",
        "items_done": 1,
        "items_failed": 1,
        "bytes": 1000,
        "bytes_down": 1500,
    }
    assert by["reddit"]["items_done"] == 1 and by["reddit"]["bytes"] == 50
    assert life["totals"] == {"items_done": 2, "items_failed": 1, "bytes": 1050, "bytes_down": 1570}


def test_lifetime_survives_pruning(tmp_path):
    # retention 1 day: a write two days later prunes the raw row but not the lifetime counter.
    db = StatsStore(tmp_path / "stats.db", retention_days=1)
    base = 10_000_000
    db.record_item("urls", True, 100, bytes_down=10, ts=base)
    db.record_item("urls", True, 200, bytes_down=20, ts=base + 2 * 86400)
    # Raw history over a 30d window has lost the pruned row...
    s = db.series(hours=24 * 30, buckets=60, now=base + 2 * 86400)
    assert s["totals"]["bytes"] == 200
    # ...but the all-time counter still reflects both items.
    assert db.lifetime()["totals"] == {
        "items_done": 2,
        "items_failed": 0,
        "bytes": 300,
        "bytes_down": 30,
    }


def test_lifetime_backfilled_from_existing_events_on_upgrade(tmp_path):
    import sqlite3

    path = tmp_path / "stats.db"
    old = sqlite3.connect(path)  # schema with events but no lifetime table
    old.execute(
        "CREATE TABLE item_events (ts INTEGER NOT NULL, project TEXT NOT NULL, "
        "ok INTEGER NOT NULL, bytes INTEGER NOT NULL DEFAULT 0, "
        "bytes_down INTEGER NOT NULL DEFAULT 0)"
    )
    old.executemany(
        "INSERT INTO item_events (ts, project, ok, bytes, bytes_down) VALUES (?,?,?,?,?)",
        [(100, "urls", 1, 700, 100), (101, "urls", 0, 0, 0), (102, "reddit", 1, 50, 5)],
    )
    old.commit()
    old.close()

    db = StatsStore(path)  # should seed lifetime from the pre-existing rows
    life = db.lifetime()
    assert life["totals"] == {"items_done": 2, "items_failed": 1, "bytes": 750, "bytes_down": 105}
    # A fresh write keeps accumulating on top of the backfilled baseline.
    db.record_item("urls", True, 300, bytes_down=20, ts=200)
    by = {p["project"]: p for p in db.lifetime()["projects"]}
    assert by["urls"]["bytes"] == 1000 and by["urls"]["items_done"] == 2


def test_window_picks_resolution_tier(tmp_path):
    # Each window is served from the tier whose resolution fits it: 15-min for 6h, hourly for
    # 24h and 7d, daily for 30d. bucket_seconds reports the tier resolution actually used.
    db = StatsStore(tmp_path / "stats.db")
    now = 20_000_000
    assert db.series(hours=6, buckets=500, now=now)["bucket_seconds"] == 900
    assert db.series(hours=24, buckets=500, now=now)["bucket_seconds"] == 3600
    assert db.series(hours=168, buckets=500, now=now)["bucket_seconds"] == 3600  # 7d
    assert db.series(hours=720, buckets=500, now=now)["bucket_seconds"] == 86400  # 30d


def test_7d_window_reads_hourly_tier(tmp_path):
    # Items spread across a week land in the right hourly slices and sum correctly over 7d.
    db = StatsStore(tmp_path / "stats.db")
    now = 30_000_000
    for day in range(7):
        db.record_item("urls", True, 100, bytes_down=10, ts=now - day * 86400 - 5)
    s = db.series(hours=168, buckets=200, now=now)
    assert s["bucket_seconds"] == 3600
    assert s["totals"] == {"items_done": 7, "items_failed": 0, "bytes": 700, "bytes_down": 70}
    # Seven items, seven distinct days -> seven non-empty hourly slices.
    assert sum(1 for b in s["buckets"] if b["items_done"]) == 7


def test_row_count_is_bounded_by_time_not_volume(tmp_path):
    # Many items inside one 15-minute window collapse into a single bucket per tier, so storage
    # stays flat no matter the throughput — the whole point of pre-bucketing.
    db = StatsStore(tmp_path / "stats.db")
    base = 40_000_000
    for i in range(500):
        db.record_item("urls", True, 1, bytes_down=1, ts=base + i)  # 500 items within ~8 min
    n_buckets = db._conn.execute("SELECT COUNT(*) FROM buckets").fetchone()[0]
    assert n_buckets == len(StatsStore.TIERS)  # exactly one bucket per tier, not 500 rows
    assert db.series(hours=1, buckets=10, now=base + 600)["totals"]["items_done"] == 500


def test_each_item_fans_out_to_every_tier(tmp_path):
    db = StatsStore(tmp_path / "stats.db")
    db.record_item("urls", True, 1000, bytes_down=500, ts=50_000_000)
    seen = {r[0] for r in db._conn.execute("SELECT DISTINCT res FROM buckets")}
    assert seen == set(StatsStore.TIERS)
    # Every tier carries the same accumulated totals for that single item.
    for res in StatsStore.TIERS:
        up, down = db._conn.execute(
            "SELECT bytes_up, bytes_down FROM buckets WHERE res = ?", (res,)
        ).fetchone()
        assert (up, down) == (1000, 500)
