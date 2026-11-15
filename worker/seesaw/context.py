"""Shared runtime services handed to every task at execution time.

Tasks receive an explicit context rather than reaching for globals, which keeps the engine
testable with fakes and the task surface free of global state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RunContext:
    http: Any = None          # async HTTP client (httpx.AsyncClient-shaped); may be None in tests
    data_dir: str = ""        # per-project data directory
    downloader: str = ""      # the one leaderboard identity, shared across projects
