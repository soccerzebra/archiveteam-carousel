"""Pipeline tasks.

Public surface projects use: ``SimpleTask`` (override ``process``) and
``LimitConcurrent``. ``SetItemKey``/``PrintItem``/``ConditionalTask`` round out the
surface.

A task does one step to one item. A task is simply:

    async def run(self, item, ctx) -> None   # raise to fail the item

The pipeline awaits each task in order (see pipeline.py).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from seesaw.config import realize


class Task:
    def __init__(self, name: str):
        self.name = name

    async def run(self, item: Any, ctx: Any) -> None:
        raise NotImplementedError

    def __str__(self) -> str:
        return self.name


class SimpleTask(Task):
    """A synchronous step: override ``process(item)``. Exceptions fail the item."""

    def __init__(self, name: str):
        super().__init__(name)

    def process(self, item: Any) -> None:
        raise NotImplementedError

    async def run(self, item: Any, ctx: Any) -> None:
        item.log_output(f"Running {self.name} for {item.description()}.")
        # process() is sync project code; run it without blocking the event loop.
        await asyncio.to_thread(self.process, item)


class LimitConcurrent(Task):
    """Bound how many items may be inside ``inner_task`` at once, across the whole runner.

    One shared semaphore per instance — e.g. ``LimitConcurrent(2, RsyncUpload(...))`` means
    at most two uploads run simultaneously even if more items are in flight. The limit may
    be an int or a (Number)ConfigValue, realized once on first use.
    """

    def __init__(self, concurrency: Any, inner_task: Task):
        super().__init__(f"LimitConcurrent({inner_task})")
        self.concurrency = concurrency
        self.inner_task = inner_task
        self._semaphore: asyncio.Semaphore | None = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(int(realize(self.concurrency)))
        return self._semaphore

    async def run(self, item: Any, ctx: Any) -> None:
        async with self._get_semaphore():
            await self.inner_task.run(item, ctx)


class SetItemKey(SimpleTask):
    def __init__(self, key: str, value: Any):
        super().__init__("SetItemKey")
        self.key = key
        self.value = value

    def process(self, item: Any) -> None:
        item[self.key] = self.value


class PrintItem(SimpleTask):
    def __init__(self):
        super().__init__("PrintItem")

    def process(self, item: Any) -> None:
        item.log_output(f"Item: {item.description()}")


class ConditionalTask(Task):
    """Run ``inner_task`` only when ``predicate(item)`` is truthy."""

    def __init__(self, predicate: Callable[[Any], bool], inner_task: Task):
        super().__init__(f"Conditional({inner_task})")
        self.predicate = predicate
        self.inner_task = inner_task

    async def run(self, item: Any, ctx: Any) -> None:
        if self.predicate(item):
            await self.inner_task.run(item, ctx)
