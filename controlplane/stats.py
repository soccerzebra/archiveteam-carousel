"""Persistent stats: lifetime counters plus a bounded, tiered activity history.

Backed by SQLite so both survive control-plane restarts. Writes come from the supervisor's
status channel (one ``record_item`` per finished item); reads come from ``/api/stats`` (the
over-time chart) and ``/api/stats/lifetime`` (the all-time totals).

Two concerns, kept separate on purpose:

  * ``buckets`` — pre-aggregated activity, keyed by (resolution, bucket-start, project). Each
    finished item is folded into one bucket per *resolution tier* at write time, so a bucket
    accumulates rather than appending a row per item. Row count is therefore bounded by
    *time x resolution x projects*, never by item volume — a busy project and an idle one cost
    the same. This is a round-robin database in spirit: fixed, rotating storage, expressed as
    prune-by-age rather than literal slot reuse.
  * ``lifetime`` — one running-total row per project (items + bytes up/down), bumped on every
    write and NEVER pruned. This is the all-time figure; because it's a counter, it survives
    the pruning of the buckets it came from.

The resolution tiers and what they serve:

  =========  ==========  =========================  ==================
  tier       bucket      serves window(s)           kept for
  =========  ==========  =========================  ==================
  15-minute  900s        6h                         24h  (~96 rows)
  hourly     3600s       24h, 7d                    10d  (~240 rows)
  daily      86400s      30d                        ~400d (~400 rows)
  =========  ==========  =========================  ==================

Each item is written to *all three* tiers at once (write fan-out), so a coarse bucket is
never reconstructed from fine data as it ages — there is no downsampling job to get wrong.
Worst case is ~740 bucket rows per project, flat forever, regardless of throughput.

A ``threading.Lock`` guards the shared connection so the event-loop thread and any
threadpool request handler can both touch it safely.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import ClassVar


class StatsStore:
    #: Resolution tiers, in seconds: 15-minute, hourly, daily.
    TIERS = (900, 3600, 86400)
    #: How long each tier is retained, in seconds. Generous margin over the longest window the
    #: tier serves, so pruning never eats data the UI can still ask for.
    DEFAULT_HORIZONS: ClassVar[dict[int, int]] = {
        900: 24 * 3600,  # 15-min: serves 6h, kept 24h
        3600: 10 * 86400,  # hourly: serves 24h & 7d, kept 10d
        86400: 400 * 86400,  # daily:  serves 30d, kept ~13 months
    }
    #: Prune at most this often (seconds), throttled against the newest event's timestamp so a
    #: burst of writes triggers at most one DELETE sweep.
    PRUNE_INTERVAL = 3600

    def __init__(self, path: str | Path, retention_days: float | None = None):
        self.path = str(path)
        # retention_days, when given, caps *every* tier to that horizon (used by tests and any
        # deployment that wants a shorter history). Otherwise each tier keeps its own default.
        if retention_days is None:
            self.horizons = dict(self.DEFAULT_HORIZONS)
        else:
            cap = int(retention_days * 86400)
            self.horizons = {res: min(h, cap) for res, h in self.DEFAULT_HORIZONS.items()}
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        # Prune reference clock: the newest event timestamp seen, so pruning tracks the data
        # rather than wall-clock (tests insert historical timestamps).
        self._max_ts = 0
        self._last_prune = 0
        self._init_schema()

    # --- schema / migration ---------------------------------------------------------------
    def _init_schema(self) -> None:
        c = self._conn
        c.execute(
            "CREATE TABLE IF NOT EXISTS buckets ("
            "  res INTEGER NOT NULL,"  # resolution in seconds: 900 | 3600 | 86400
            "  bucket INTEGER NOT NULL,"  # bucket start: ts - (ts % res)
            "  project TEXT NOT NULL,"
            "  items_done INTEGER NOT NULL DEFAULT 0,"
            "  items_failed INTEGER NOT NULL DEFAULT 0,"
            "  bytes_up INTEGER NOT NULL DEFAULT 0,"  # bytes uploaded (archived to target)
            "  bytes_down INTEGER NOT NULL DEFAULT 0,"  # bytes downloaded (tracker's count)
            "  PRIMARY KEY (res, bucket, project))"  # PK prefix (res, bucket) covers reads
        )
        # Per-project lifetime counters — the all-time totals, immune to pruning.
        c.execute(
            "CREATE TABLE IF NOT EXISTS lifetime ("
            "  project TEXT PRIMARY KEY,"
            "  items_done INTEGER NOT NULL DEFAULT 0,"
            "  items_failed INTEGER NOT NULL DEFAULT 0,"
            "  bytes_up INTEGER NOT NULL DEFAULT 0,"
            "  bytes_down INTEGER NOT NULL DEFAULT 0)"
        )
        c.commit()
        self._migrate_item_events()
        row = c.execute("SELECT MAX(bucket) FROM buckets").fetchone()
        self._max_ts = int(row[0] or 0)

    def _migrate_item_events(self) -> None:
        """One-time upgrade from the old append-per-item ``item_events`` table: seed the lifetime
        counters and fold the raw rows into the tiered buckets, then drop it. A fresh database
        has no ``item_events`` and skips all of this."""
        c = self._conn
        if not c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='item_events'"
        ).fetchone():
            return
        # Databases created before upload/download were split out lack bytes_down.
        have = {row[1] for row in c.execute("PRAGMA table_info(item_events)")}
        if "bytes_down" not in have:
            c.execute("ALTER TABLE item_events ADD COLUMN bytes_down INTEGER NOT NULL DEFAULT 0")
        # Seed lifetime from the raw rows (unless already populated — don't double-count).
        if not c.execute("SELECT 1 FROM lifetime LIMIT 1").fetchone():
            c.execute(
                "INSERT INTO lifetime (project, items_done, items_failed, bytes_up, bytes_down) "
                "SELECT project, SUM(ok), SUM(1 - ok), SUM(bytes), SUM(bytes_down) "
                "FROM item_events GROUP BY project"
            )
        # Fold the raw history into every tier (unless buckets already hold data).
        if not c.execute("SELECT 1 FROM buckets LIMIT 1").fetchone():
            for res in self.TIERS:
                c.execute(
                    "INSERT INTO buckets "
                    "  (res, bucket, project, items_done, items_failed, bytes_up, bytes_down) "
                    "SELECT ?, (ts / ?) * ?, project, "
                    "       SUM(ok), SUM(1 - ok), SUM(bytes), SUM(bytes_down) "
                    "FROM item_events GROUP BY project, (ts / ?) * ?",
                    (res, res, res, res, res),
                )
        c.execute("DROP TABLE item_events")
        c.commit()

    # --- writes ---------------------------------------------------------------------------
    def record_item(
        self, project: str, ok: bool, nbytes: int, bytes_down: int = 0, ts: int | None = None
    ) -> None:
        """Fold one finished item into every resolution tier and bump the project's lifetime
        counters, atomically."""
        when = int(ts if ts is not None else time.time())
        nbytes = int(nbytes or 0)
        bytes_down = int(bytes_down or 0)
        done, failed = (1, 0) if ok else (0, 1)
        with self._lock:
            for res in self.TIERS:
                bucket = when - (when % res)
                self._conn.execute(
                    "INSERT INTO buckets "
                    "  (res, bucket, project, items_done, items_failed, bytes_up, bytes_down) "
                    "VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(res, bucket, project) DO UPDATE SET "
                    "  items_done   = items_done   + excluded.items_done, "
                    "  items_failed = items_failed + excluded.items_failed, "
                    "  bytes_up     = bytes_up     + excluded.bytes_up, "
                    "  bytes_down   = bytes_down   + excluded.bytes_down",
                    (res, bucket, project, done, failed, nbytes, bytes_down),
                )
            self._conn.execute(
                "INSERT INTO lifetime (project, items_done, items_failed, bytes_up, bytes_down) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(project) DO UPDATE SET "
                "  items_done   = items_done   + excluded.items_done, "
                "  items_failed = items_failed + excluded.items_failed, "
                "  bytes_up     = bytes_up     + excluded.bytes_up, "
                "  bytes_down   = bytes_down   + excluded.bytes_down",
                (project, done, failed, nbytes, bytes_down),
            )
            self._max_ts = max(self._max_ts, when)
            self._maybe_prune()
            self._conn.commit()

    def _maybe_prune(self) -> None:
        """Delete buckets past each tier's horizon, throttled to PRUNE_INTERVAL. Lifetime
        counters are untouched, so all-time totals are preserved. Caller holds the lock."""
        if self._max_ts - self._last_prune < self.PRUNE_INTERVAL:
            return
        self._last_prune = self._max_ts
        for res, horizon in self.horizons.items():
            self._conn.execute(
                "DELETE FROM buckets WHERE res = ? AND bucket < ?",
                (res, self._max_ts - horizon),
            )

    # --- reads ----------------------------------------------------------------------------
    @classmethod
    def _res_for_window(cls, hours: float) -> int:
        """Pick the storage tier that serves a window of this length: 15-min up to 6h, hourly
        up to 7d, daily beyond."""
        seconds = hours * 3600
        if seconds <= 6 * 3600:
            return 900
        if seconds <= 7 * 86400:
            return 3600
        return 86400

    def series(self, hours: float = 24.0, buckets: int = 48, now: int | None = None) -> dict:
        """Return the last ``hours`` of activity as time slices read from the tier that fits the
        window. ``buckets`` is an upper bound on the number of points — the real resolution is
        the tier's, so the result has at most ``window / tier_resolution`` slices (never a comb
        of gaps from asking finer than the data is stored).

        Each slice carries items done, items failed, and bytes up/down; ``totals`` sums the
        window. Empty slices are included so the chart has a continuous time axis.
        """
        now = int(now if now is not None else time.time())
        res = self._res_for_window(hours)
        window = max(res, int(hours * 3600))
        n_full = max(1, window // res)  # tier buckets spanning the window
        points = max(1, min(int(buckets or 1), n_full))  # honor `buckets` only as a cap
        slot = max(res, window // points)
        start = now - window
        start_floor = (start // res) * res  # include the bucket covering `start`
        with self._lock:
            rows = self._conn.execute(
                "SELECT bucket, SUM(items_done), SUM(items_failed), SUM(bytes_up), SUM(bytes_down) "
                "FROM buckets WHERE res = ? AND bucket >= ? AND bucket <= ? GROUP BY bucket",
                (res, start_floor, now),
            ).fetchall()
        out = [
            {"t": start + i * slot, "items_done": 0, "items_failed": 0, "bytes": 0, "bytes_down": 0}
            for i in range(points)
        ]
        for bucket, done, failed, up, down in rows:
            idx = (bucket - start) // slot
            idx = 0 if idx < 0 else (points - 1 if idx > points - 1 else idx)
            slot_row = out[idx]
            slot_row["items_done"] += int(done or 0)
            slot_row["items_failed"] += int(failed or 0)
            slot_row["bytes"] += int(up or 0)
            slot_row["bytes_down"] += int(down or 0)
        return {
            "window_hours": hours,
            "bucket_seconds": slot,
            "buckets": out,
            "totals": {
                "items_done": sum(b["items_done"] for b in out),
                "items_failed": sum(b["items_failed"] for b in out),
                "bytes": sum(b["bytes"] for b in out),
                "bytes_down": sum(b["bytes_down"] for b in out),
            },
        }

    def lifetime(self) -> dict:
        """All-time counters per project plus a grand total, independent of any window."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT project, items_done, items_failed, bytes_up, bytes_down "
                "FROM lifetime ORDER BY bytes_up DESC, project"
            ).fetchall()
        projects = [
            {
                "project": p,
                "items_done": int(d),
                "items_failed": int(f),
                "bytes": int(up),
                "bytes_down": int(down),
            }
            for p, d, f, up, down in rows
        ]
        return {
            "projects": projects,
            "totals": {
                "items_done": sum(p["items_done"] for p in projects),
                "items_failed": sum(p["items_failed"] for p in projects),
                "bytes": sum(p["bytes"] for p in projects),
                "bytes_down": sum(p["bytes_down"] for p in projects),
            },
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
