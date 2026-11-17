"""Config store: global settings + per-project settings, persisted to config.json.

A two-level model: one GlobalConfig, plus a ProjectConfig per project under its own
namespace key, so projects never clobber each other's settings on start/stop.

Seed order (first that is present wins):
  1. existing config.json
  2. environment variables (DOWNLOADER, HTTP_USERNAME, ...)
  3. built-in defaults
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .models import GlobalConfig, ProjectConfig

_ENV_MAP = {
    "downloader": "DOWNLOADER",
    "warrior_id": "WARRIOR_ID",
    "http_username": "HTTP_USERNAME",
    "http_password": "HTTP_PASSWORD",
}


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self.global_config = GlobalConfig()
        self.projects: dict[str, ProjectConfig] = {}

    def load(self) -> None:
        """Load config.json if present, else seed from env + defaults."""
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self.global_config = GlobalConfig(**raw.get("global", {}))
            self.projects = {
                name: ProjectConfig(**pc) for name, pc in raw.get("projects", {}).items()
            }
            return
        # Seed from environment variables.
        seed = {k: os.environ[v] for k, v in _ENV_MAP.items() if os.environ.get(v)}
        # Env values are strings; pydantic coerces them to the field types at runtime.
        self.global_config = GlobalConfig(**seed)  # type: ignore[arg-type]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "global": self.global_config.model_dump(),
            "projects": {n: pc.model_dump() for n, pc in self.projects.items()},
        }
        self.path.write_text(json.dumps(payload, indent=2))

    def enabled_projects(self) -> list[str]:
        return [name for name, pc in self.projects.items() if pc.enabled]

    def project(self, name: str) -> ProjectConfig:
        return self.projects.setdefault(name, ProjectConfig(project=name))
