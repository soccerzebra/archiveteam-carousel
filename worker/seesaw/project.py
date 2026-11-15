"""Project metadata declared by a pipeline.

Public surface: ``Project`` — a passive data holder (title + an HTML blurb the control
plane shows); the pipeline carries the actual behavior.
"""

from __future__ import annotations

from typing import Any


class Project:
    def __init__(
        self, title: str = "", project_html: str = "", utc_deadline: Any = None, **extra: Any
    ):
        self.title = title
        self.project_html = project_html
        self.utc_deadline = utc_deadline
        self.extra = extra

    def __str__(self) -> str:
        return f"Project({self.title!r})"
