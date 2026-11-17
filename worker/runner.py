"""The Runner: drive up to N items concurrently through one pipeline.

Our engine, not part of the ``seesaw`` surface (projects never import it). It keeps
``concurrent_items`` item-coroutines in flight; each creates a fresh Item and runs it
through the pipeline, whose first task claims work from the tracker. When an item finishes,
its coroutine loops and starts another.

This is the whole concurrency model: per-item parallelism is ``concurrent_items`` here;
per-stage parallelism (e.g. rsync threads) is a ``LimitConcurrent`` inside the pipeline.
Both compose cleanly because everything is just ``await``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Any

from seesaw.context import RunContext
from seesaw.item import Item


class GracefulStop(Exception):
    """Raised by a task (or injected) to tell a worker loop to stop after the current item."""


# Generic phrases meaning "this thread is resting, not actively transferring". These are
# common to ArchiveTeam's trackers and retry loops across the whole project ecosystem (the
# trackers emit the same "no items / trying again" text everywhere, and our own retry loop
# logs "retrying"), so this is a small SHARED heuristic — not per-project log parsing. Only
# the most-recent non-empty log line is consulted; first phrase match wins. No match => the
# thread is treated as actively working and the UI shows the live log instead of a status.
REST_PHRASES: tuple[tuple[str, str], ...] = (
    ("trying again in", "waiting"),
    ("no free items", "waiting"),
    ("no items", "waiting"),
    ("waiting for", "waiting"),
    ("sleeping", "sleeping"),
    ("retrying", "retrying"),
    ("rate limit", "rate-limited"),
    ("slowing down", "rate-limited"),
    ("backing off", "waiting"),
)


def derive_phase(log: list[str]) -> str:
    """Classify a thread as resting from its latest log line, or "" when it's working."""
    for line in reversed(log):
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        for phrase, label in REST_PHRASES:
            if phrase in low:
                return label
        return ""  # newest line isn't a rest phrase => actively working
    return ""


class Runner:
    def __init__(
        self,
        pipeline: Any,
        concurrent_items: int,
        ctx: RunContext,
        item_factory: Callable[[], Any] = Item,
        fail_delay: float = 10.0,
        on_item_end: Callable[[Any], None] | None = None,
        on_threads: Callable[[list], None] | None = None,
        scratch: Any | None = None,
    ):
        self.pipeline = pipeline
        self.concurrent_items = concurrent_items
        self.ctx = ctx
        self.item_factory = item_factory
        self.fail_delay = fail_delay
        self.on_item_end = on_item_end
        self.on_threads = on_threads
        # Per-item data directories (worker/scratch.py). Without one, every item shares
        # ctx.data_dir and nothing is cleaned up — only for tests that never touch disk.
        self.scratch = scratch
        # Plain flag, not an asyncio.Event: nothing awaits on it, and this keeps the Runner
        # free of any event loop until run() is actually awaited.
        self._stopped = False
        self.completed = 0
        self.failed = 0
        # Live per-slot state for the dashboard's thread view, keyed by worker index.
        self.threads: dict[int, dict] = {}
        # Dynamic concurrency pool: one task per active slot. The count can change at
        # runtime via set_concurrency() without restarting the process, so in-flight items
        # keep running. _retiring holds slots that should exit after their current item.
        self._slots: dict[int, asyncio.Task] = {}
        self._retiring: set[int] = set()
        self._pool_changed: asyncio.Event | None = None

    def stop(self) -> None:
        """Request graceful stop: workers finish their current item, then exit."""
        self._stopped = True

    def set_concurrency(self, concurrent_items: int) -> None:
        """Change the live slot count without restarting.

        Growing spawns new slot coroutines (reviving pending retirements first); shrinking
        flags surplus slots to exit after their current item finishes. In-flight items are
        never interrupted.
        """
        target = max(1, int(concurrent_items))
        self.concurrent_items = target
        active = [i for i in self._slots if i not in self._retiring]
        if target > len(active):
            need = target - len(active)
            # Cancel pending retirements before spawning brand-new slots.
            for idx in list(self._retiring):
                if need == 0:
                    break
                self._retiring.discard(idx)
                need -= 1
            if need:
                self._spawn_slots(need)
        elif target < len(active):
            # Retire the highest-indexed active slots; they stop after the current item.
            for idx in sorted(active, reverse=True)[: len(active) - target]:
                self._retiring.add(idx)

    def _spawn_slots(self, count: int) -> None:
        for _ in range(count):
            idx = 0
            while idx in self._slots:
                idx += 1
            self._slots[idx] = asyncio.create_task(self._worker(idx))
        if self._pool_changed is not None:
            self._pool_changed.set()  # wake run() so it awaits the new slots too

    async def run(self) -> None:
        self._pool_changed = asyncio.Event()
        self._spawn_slots(self.concurrent_items)
        ticker = asyncio.create_task(self._tick())
        try:
            while self._slots:
                self._pool_changed.clear()
                change = asyncio.create_task(self._pool_changed.wait())
                try:
                    await asyncio.wait(
                        [*self._slots.values(), change],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    change.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await change
                # Reap finished slots (retired or drained); surface an unexpected crash.
                for idx in [i for i, t in self._slots.items() if t.done()]:
                    task = self._slots.pop(idx)
                    self._retiring.discard(idx)
                    exc = None if task.cancelled() else task.exception()
                    if exc is not None:
                        raise exc
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker

    async def _tick(self) -> None:
        """Re-emit the thread snapshot periodically so the live log tail stays fresh even
        while a single long-running task (e.g. a scraper) produces output."""
        while not self._stopped:
            await asyncio.sleep(3)
            self._emit_threads()

    async def _worker(self, index: int) -> None:
        while not self._stopped and index not in self._retiring:
            item = self.item_factory()
            # Every pipeline reads item["data_dir"] (PrepareDirectories builds its working dir
            # under it; MoveFiles leaves the finished WARC there for upload). It's this item's
            # own directory, deleted below once the item is over.
            item["data_dir"] = (
                self.scratch.item_dir(item.item_id) if self.scratch else self.ctx.data_dir
            )
            self._thread_begin(index, item)
            try:
                await self.pipeline.run_item(
                    item, self.ctx, on_task=lambda it, name, i=index: self._thread_step(i, it, name)
                )
            except GracefulStop:
                # The pipeline signalled "no more work for me" — leave this item unfinished
                # and stop this worker.
                self._stopped = True
                self._release(item)
                self._thread_end(index)
                return
            except asyncio.CancelledError:
                item.cancel()
                item.on_finish.fire(item)
                self._release(item)
                self._thread_end(index)
                raise
            except Exception as exc:  # a task failed this item; keep the worker alive
                item.state = Item.FAILED
                self.failed += 1
                item.last_error = f"{type(exc).__name__}: {exc}"
                item.log_output(f"Item failed: {exc!r}")
                item.on_finish.fire(item)  # cleanup hooks run on failure too
                self._release(item)
                self._thread_end(index)
                self._notify(item)
                if self.fail_delay:
                    await asyncio.sleep(self.fail_delay)
                continue
            if item.state == Item.RUNNING:
                item.state = Item.COMPLETED
            self.completed += 1
            item.on_finish.fire(item)
            self._release(item)
            self._thread_end(index)
            self._notify(item)

    def _release(self, item: Any) -> None:
        """Delete the item's directory: uploaded or abandoned, its files are done with."""
        if self.scratch is not None:
            self.scratch.release(item.get("data_dir"))

    # --- live per-thread tracking -----------------------------------------------------
    LOG_TAIL = 3  # log lines reported per thread
    LOG_WIDTH = 240  # max chars per reported line

    def _thread_begin(self, index: int, item: Any) -> None:
        # Keep the live Item so the snapshot can read its current log/bytes each tick.
        self.threads[index] = {"slot": index, "item": item, "task": "", "started": time.time()}
        self._emit_threads()

    def _thread_step(self, index: int, item: Any, task_name: str) -> None:
        t = self.threads.get(index)
        if t is not None:
            t["task"] = task_name
        self._emit_threads()

    def _thread_end(self, index: int) -> None:
        self.threads.pop(index, None)
        self._emit_threads()

    def _snapshot(self) -> list[dict]:
        out = []
        for i in sorted(self.threads):
            t = self.threads[i]
            item = t["item"]
            log = [ln[: self.LOG_WIDTH] for ln in item.log[-self.LOG_TAIL :]]
            out.append(
                {
                    "slot": i,
                    "item_id": item.item_id,
                    "item_name": item.description(),
                    "task": t["task"],
                    "phase": derive_phase(log),
                    "started": t["started"],
                    "bytes_up": int(item.get("carousel_bytes", 0)),
                    "bytes_down": 0,
                    "log": log,
                }
            )
        return out

    def _emit_threads(self) -> None:
        if self.on_threads:
            self.on_threads(self._snapshot())

    def _notify(self, item: Any) -> None:
        if self.on_item_end:
            self.on_item_end(item)
