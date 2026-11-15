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
import time
from typing import Any, Callable

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
        workers = [asyncio.create_task(self._worker(i))
                   for i in range(self.concurrent_items)]
        await asyncio.gather(*workers)

    async def _worker(self, index: int) -> None:
        while not self._stopped:
            item = self.item_factory()
            self._thread_begin(index, item)
            try:
                await self.pipeline.run_item(
                    item, self.ctx,
                    on_task=lambda it, name, i=index: self._thread_step(i, it, name))
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
    def _thread_begin(self, index: int, item: Any) -> None:
        self.threads[index] = {"slot": index, "item_id": item.item_id,
                               "item_name": item.description(), "task": "",
                               "started": time.time(), "bytes_up": 0, "bytes_down": 0}
        self._emit_threads()

    def _thread_step(self, index: int, item: Any, task_name: str) -> None:
        t = self.threads.get(index)
        if t is not None:
            t["task"] = task_name
            t["item_name"] = item.description()
            t["bytes_up"] = int(item.get("carousel_bytes", 0))
        self._emit_threads()

    def _thread_end(self, index: int) -> None:
        self.threads.pop(index, None)
        self._emit_threads()

    def _emit_threads(self) -> None:
        if self.on_threads:
            self.on_threads(sorted(self.threads.values(), key=lambda t: t["slot"]))

    def _notify(self, item: Any) -> None:
        if self.on_item_end:
            self.on_item_end(item)
