"""Persistent stats history: one row per completed item, bucketed on read.

Backed by SQLite so the history survives control-plane restarts. Writes come from the
supervisor's status channel (one ``item_end`` per finished item); reads come from the
``/api/stats`` endpoint, which the dashboard polls to draw the over-time chart.

The store is deliberately tiny: a single append-only table and a bucketing query. A
``threading.Lock`` guards the shared connection so the event-loop thread and any threadpool
request handler can both touch it safely.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class StatsStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS item_events ("
            "  ts INTEGER NOT NULL,"
            "  project TEXT NOT NULL,"
            "  ok INTEGER NOT NULL,"
            "  bytes INTEGER NOT NULL DEFAULT 0)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_item_events_ts ON item_events(ts)")
        self._conn.commit()

    def record_item(self, project: str, ok: bool, nbytes: int, ts: int | None = None) -> None:
        """Append one finished item (success or failure) with its archived byte count."""
        when = int(ts if ts is not None else time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO item_events (ts, project, ok, bytes) VALUES (?, ?, ?, ?)",
                (when, project, 1 if ok else 0, int(nbytes or 0)))
            self._conn.commit()

    def series(self, hours: float = 24.0, buckets: int = 48, now: int | None = None) -> dict:
        """Return the last ``hours`` of activity as ``buckets`` equal time slices.

        Each bucket carries items done, items failed, and bytes archived in that slice;
        ``totals`` sums the window. Empty buckets are included so the chart has a continuous
        time axis.
        """
        buckets = max(1, int(buckets))
        now = int(now if now is not None else time.time())
        window = max(1, int(hours * 3600))
        start = now - window
        bucket_sec = max(1, window // buckets)
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, ok, bytes FROM item_events WHERE ts >= ?", (start,)).fetchall()
        agg: dict[int, list[int]] = {}
        for ts, ok, nbytes in rows:
            idx = min(buckets - 1, (ts - start) // bucket_sec)
            slot = agg.setdefault(idx, [0, 0, 0])
            slot[0 if ok else 1] += 1
            slot[2] += nbytes
        out = []
        for i in range(buckets):
            done, failed, nbytes = agg.get(i, [0, 0, 0])
            out.append({"t": start + i * bucket_sec, "items_done": done,
                        "items_failed": failed, "bytes": nbytes})
        return {
            "window_hours": hours,
            "bucket_seconds": bucket_sec,
            "buckets": out,
            "totals": {
                "items_done": sum(b["items_done"] for b in out),
                "items_failed": sum(b["items_failed"] for b in out),
                "bytes": sum(b["bytes"] for b in out),
            },
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
