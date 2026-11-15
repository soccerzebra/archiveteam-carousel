"""The pipeline: an ordered list of tasks run against one item.

Public surface: ``Pipeline(*tasks)``.

A Pipeline is an ordinary object the Runner drives. Each worker process loads one;
multi-project comes from running many worker processes, not many pipelines in one.
"""

from __future__ import annotations

import os
from typing import Any


class Pipeline:
    def __init__(self, *tasks: Any):
        self.tasks = list(tasks)
        self.project: Any = None
        # Captured at construction = the project's directory (its .lua / ignore-list live
        # relative to here); tasks that chdir use it.
        self.cwd = os.getcwd()

    async def run_item(self, item: Any, ctx: Any, on_task: Any = None) -> None:
        """Drive one item through every task in order. Raises if a task fails.

        ``on_task(item, task_name)`` is called before each task so the Runner can report the
        current step for the live per-thread view.
        """
        for task in self.tasks:
            if item.canceled:
                return
            if on_task is not None:
                on_task(item, getattr(task, "name", task.__class__.__name__))
            await task.run(item, ctx)

    def __str__(self) -> str:
        return "Pipeline(\n  " + "\n  ".join(str(t) for t in self.tasks) + "\n)"
