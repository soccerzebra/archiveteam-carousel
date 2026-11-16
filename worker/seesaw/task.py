"""Pipeline tasks.

Public surface projects use: ``SimpleTask`` (override ``process``) and
``LimitConcurrent``. ``SetItemKey``/``PrintItem``/``ConditionalTask`` round out the
surface.

A task does one step to one item. A task is simply:

    async def run(self, item, ctx) -> None   # raise to fail the item

The pipeline awaits each task in order (see pipeline.py), entering every task through
``execute``, which gives a project the chance to decide per item whether the task runs at
all by overriding ``enqueue(item)``:

    class MaybeUpload(UploadWithTracker):
        def enqueue(self, item):
            if not item["item_name"]:
                return self.complete_item(item)   # skip: the item moves on
            return super().enqueue(item)          # proceed: the task runs

``self.fail_item(item)`` fails the item instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from seesaw.config import realize


class TaskFailed(Exception):
    """A task's ``enqueue`` failed the item (``fail_item``) or left it undecided."""


_PROCEED, _COMPLETE, _FAIL = "proceed", "complete", "fail"


class Task:
    # What the current enqueue() call decided. A class-level default so subclasses that
    # never call Task.__init__ still work; execute() resets it before every enqueue().
    _decision: str | None = None

    def __init__(self, name: str):
        self.name = name

    async def run(self, item: Any, ctx: Any) -> None:
        raise NotImplementedError

    async def execute(self, item: Any, ctx: Any) -> None:
        """Run this task for ``item``, honoring the task's ``enqueue`` decision.

        ``enqueue`` is synchronous and its decision is read back before the next await, so
        concurrent items sharing this task instance can't see each other's decision.
        """
        self._decision = None
        self.enqueue(item)
        decision = self._decision
        if decision == _PROCEED:
            await self.run(item, ctx)
        elif decision == _FAIL:
            raise TaskFailed(f"{self} failed the item.")
        elif decision is None:
            raise TaskFailed(f"{self}.enqueue() neither ran, completed, nor failed the item.")

    def enqueue(self, item: Any) -> None:
        """Decide what this task does with ``item``. The default is to run it."""
        self._decision = _PROCEED

    def start_item(self, item: Any) -> None:
        """Hook called by project code when it begins work on an item; nothing to do."""

    def complete_item(self, item: Any) -> None:
        """Mark this task done for ``item`` without running it."""
        self._decision = _COMPLETE

    def fail_item(self, item: Any) -> None:
        """Fail ``item`` at this task."""
        self._decision = _FAIL

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
            await self.inner_task.execute(item, ctx)


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
            await self.inner_task.execute(item, ctx)
