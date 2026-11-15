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

    async def run_item(self, item: Any, ctx: Any) -> None:
        """Drive one item through every task in order. Raises if a task fails."""
        for task in self.tasks:
            if item.canceled:
                return
            await task.run(item, ctx)

    def __str__(self) -> str:
        return "Pipeline(\n  " + "\n  ".join(str(t) for t in self.tasks) + "\n)"
