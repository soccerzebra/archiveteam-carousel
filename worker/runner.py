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
    ):
        self.pipeline = pipeline
        self.concurrent_items = concurrent_items
        self.ctx = ctx
        self.item_factory = item_factory
        self.fail_delay = fail_delay
        self.on_item_end = on_item_end
        self.on_threads = on_threads
        # Plain flag, not an asyncio.Event: nothing awaits on it, and this keeps the Runner
        # free of any event loop until run() is actually awaited.
        self._stopped = False
        self.completed = 0
        self.failed = 0
        # Live per-slot state for the dashboard's thread view, keyed by worker index.
        self.threads: dict[int, dict] = {}

    def stop(self) -> None:
        """Request graceful stop: workers finish their current item, then exit."""
        self._stopped = True

    async def run(self) -> None:
        workers = [asyncio.create_task(self._worker(i)) for i in range(self.concurrent_items)]
        ticker = asyncio.create_task(self._tick())
        try:
            await asyncio.gather(*workers)
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
        while not self._stopped:
            item = self.item_factory()
            self._thread_begin(index, item)
            try:
                await self.pipeline.run_item(
                    item, self.ctx, on_task=lambda it, name, i=index: self._thread_step(i, it, name)
                )
            except GracefulStop:
                # The pipeline signalled "no more work for me" — leave this item unfinished
                # and stop this worker.
                self._stopped = True
                self._thread_end(index)
                return
            except asyncio.CancelledError:
                item.cancel()
                self._thread_end(index)
                raise
            except Exception as exc:  # a task failed this item; keep the worker alive
                item.state = Item.FAILED
                self.failed += 1
                item.last_error = f"{type(exc).__name__}: {exc}"
                item.log_output(f"Item failed: {exc!r}")
                self._thread_end(index)
                self._notify(item)
                if self.fail_delay:
                    await asyncio.sleep(self.fail_delay)
                continue
            if item.state == Item.RUNNING:
                item.state = Item.COMPLETED
            self.completed += 1
            self._thread_end(index)
            self._notify(item)

    # --- live per-thread tracking -----------------------------------------------------
    LOG_TAIL = 6  # log lines reported per thread
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
            out.append(
                {
                    "slot": i,
                    "item_id": item.item_id,
                    "item_name": item.description(),
                    "task": t["task"],
                    "started": t["started"],
                    "bytes_up": int(item.get("carousel_bytes", 0)),
                    "bytes_down": 0,
                    "log": [ln[: self.LOG_WIDTH] for ln in item.log[-self.LOG_TAIL :]],
                }
            )
        return out

    def _emit_threads(self) -> None:
        if self.on_threads:
            self.on_threads(self._snapshot())

    def _notify(self, item: Any) -> None:
        if self.on_item_end:
            self.on_item_end(item)
