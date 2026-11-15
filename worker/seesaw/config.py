"""Lazy value resolution + user-configurable settings.

Public surface projects use: ``realize``, ``NumberConfigValue``, ``StringConfigValue``.

The model: pipelines are built at import time out of *lazy* objects (ItemValue,
ItemInterpolation, ConfigValue). ``realize(v, item)`` turns such a tree into concrete
values against a specific work item — this is the single resolution primitive the whole
runtime (and every task's arguments) flows through.
"""

from __future__ import annotations

import re
from typing import Any


def realize(value: Any, item: Any = None) -> Any:
    """Recursively resolve lazy objects to concrete values for ``item``.

    Containers are walked; any object exposing ``.realize(item)`` is asked to resolve
    itself; everything else is returned unchanged.
    """
    if isinstance(value, dict):
        return {k: realize(v, item) for k, v in value.items()}
    if isinstance(value, list):
        return [realize(v, item) for v in value]
    if hasattr(value, "realize"):
        return value.realize(item)
    return value


class ConfigValue:
    """A validated, user-editable setting that resolves to its current value.

    Instances created while a collector is active register themselves, so the control
    plane can surface a project's declared settings in the UI.
    """

    _collector: list["ConfigValue"] | None = None

    @classmethod
    def start_collecting(cls) -> None:
        ConfigValue._collector = []

    @classmethod
    def stop_collecting(cls) -> list["ConfigValue"]:
        collected = ConfigValue._collector or []
        ConfigValue._collector = None
        return collected

    def __init__(self, name, title="", description="", default=None,
                 editable=True, advanced=True):
        self.name = name
        self.title = title
        self.description = description
        self.editable = editable
        self.advanced = advanced
        self.error: str | None = None
        self.value = self.convert_value(default)
        if ConfigValue._collector is not None:
            ConfigValue._collector.append(self)

    def realize(self, item: Any = None) -> Any:
        return self.value

    def set_value(self, value) -> bool:
        self.error = self.check_value(value)
        if self.error is None:
            self.value = self.convert_value(value)
            return True
        return False

    # Overridden by subclasses.
    def check_value(self, value) -> str | None:
        return None

    def convert_value(self, value):
        return value

    def is_valid(self) -> bool:
        return self.value is not None

    def __str__(self) -> str:
        return f"<{self.name}:{self.value}>"


class StringConfigValue(ConfigValue):
    def __init__(self, *args, regex: str | None = None, **kwargs):
        self.regex = regex
        super().__init__(*args, **kwargs)

    def check_value(self, value) -> str | None:
        if self.regex and not re.search(self.regex, value.strip()):
            return f"Invalid value for {self.title.lower()}."
        return None


class NumberConfigValue(ConfigValue):
    def __init__(self, *args, min: int | None = None, max: int | None = None, **kwargs):
        self.min = min
        self.max = max
        super().__init__(*args, **kwargs)

    def check_value(self, value) -> str | None:
        value = value.strip()
        if not re.match(r"^[0-9]+$", value):
            return "Invalid number."
        n = int(value)
        if self.min is not None and n < self.min:
            return f"Number must be {self.min} or greater."
        if self.max is not None and n > self.max:
            return f"Number must be {self.max} or smaller."
        return None

    def convert_value(self, value):
        return None if value is None else int(value)


class ConfigInterpolation:
    """``format_string % config`` resolved lazily (both sides realized first)."""

    def __init__(self, format_string, config):
        self.format_string = format_string
        self.config = config

    def realize(self, item: Any) -> Any:
        return realize(self.format_string, item) % realize(self.config, item)

    def __str__(self) -> str:
        return f"<'{self.format_string}'>"
