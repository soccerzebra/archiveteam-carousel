"""Work items and the lazy references that read from them.

Public surface projects use: ``ItemValue``, ``ItemInterpolation``.

An item is the unit of work (one tracker assignment). It behaves as a mutable mapping of
properties that tasks set and read (``item_dir``, ``warc_file_base``, ``stats`` …). The
two lazy references below let a pipeline, defined once at import time, point at values an
item won't have until runtime.
"""

from __future__ import annotations

import uuid
from typing import Any


class ItemValue:
    """Resolves to ``item[key]`` at realize time."""

    def __init__(self, key: str):
        self.key = key

    def realize(self, item: "Item") -> Any:
        return item[self.key]

    def fill(self, item: "Item", value: Any) -> None:
        item[self.key] = value

    def __str__(self) -> str:
        return f"<{self.key}>"


class ItemInterpolation:
    """Resolves to ``format_string % item`` at realize time.

    Because it uses percent-mapping formatting, ``item`` must support ``__getitem__``;
    e.g. ``ItemInterpolation('%(item_dir)s/%(warc_file_base)s.warc.zst')``.
    """

    def __init__(self, format_string: str):
        self.format_string = format_string

    def realize(self, item: "Item") -> str:
        return self.format_string % item

    def __str__(self) -> str:
        return f"<'{self.format_string}'>"


class Item(dict):
    """A work item: a property mapping plus runtime bookkeeping.

    It subclasses ``dict`` so ``ItemValue`` (``item[key]``) and ``ItemInterpolation``
    (``fmt % item``) resolve against it directly. Tasks set properties as they go
    (``item_name`` from the tracker, ``item_dir`` from PrepareDirectories, ``stats`` …).
    Bookkeeping lives in attributes, which stay out of the ``%`` mapping.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    def __init__(self, item_id: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.item_id = item_id or uuid.uuid4().hex[:8]
        self.state = self.RUNNING
        self.canceled = False
        self.last_error: str | None = None
        self.log: list[str] = []

    #: Cap the retained log so a long-running task (e.g. a scraper that streams output for
    #: hours) can't grow this list without bound. We keep the most recent lines.
    LOG_CAP = 2000

    def log_output(self, message: Any) -> None:
        self.log.append(str(message))
        if len(self.log) > self.LOG_CAP:
            del self.log[:-self.LOG_CAP]

    def description(self) -> str:
        return self.get("item_name") or f"Item {self.item_id}"

    def cancel(self) -> None:
        self.canceled = True
        self.state = self.CANCELED

    def __str__(self) -> str:
        return f"<Item {self.item_id} {self.description()!r} [{self.state}]>"
