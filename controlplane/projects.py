"""Install a project's code so a worker can run it.

Two source kinds, both ending in a plain directory containing
``pipeline.py`` that a worker process runs — never a nested container, keeping the
process-per-project model:

  * GIT    : git clone / git pull the repository.
  * DOCKER : (not yet) `crane export` the image and untar its ``/grab`` dir.

Each install is copied to a version-pinned dir (``<name>-<githash>``) so an in-flight item
keeps its exact code even if the project updates. Version dirs are reference-counted across
all active workers: a dir is removed only when no worker still references it.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .models import ProjectInfo, ProjectSource


async def _run(*args: str, cwd: str | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed ({proc.returncode}):\n{text}")
    return text.strip()


@dataclass
class ProjectInstaller:
    projects_dir: Path          # working checkouts
    versions_dir: Path          # immutable version-pinned copies workers run from
    _refcounts: dict[Path, int] = field(default_factory=dict)

    def __post_init__(self):
        self.projects_dir = Path(self.projects_dir)
        self.versions_dir = Path(self.versions_dir)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.versions_dir.mkdir(parents=True, exist_ok=True)

    async def prepare(self, info: ProjectInfo) -> Path:
        """Install/update the project, pin the current version, acquire it; return the dir."""
        checkout = await self.install(info)
        version_dir = await self.pin_version(info, checkout)
        self.acquire(version_dir)
        return version_dir

    async def install(self, info: ProjectInfo) -> Path:
        if info.source is ProjectSource.DOCKER:
            raise NotImplementedError(
                "docker-image projects (crane export of /grab) are not implemented yet; "
                "use the git `repository` source.")
        if not info.repository:
            raise ValueError(f"project {info.name} has no git repository")

        checkout = self.projects_dir / info.name
        if (checkout / ".git").exists():
            await _run("git", "fetch", "--depth", "1", "origin", cwd=str(checkout))
            await _run("git", "reset", "--hard", "origin/HEAD", cwd=str(checkout))
        else:
            if checkout.exists():
                shutil.rmtree(checkout)
            await _run("git", "clone", "--depth", "1", info.repository, str(checkout))
        return checkout

    async def pin_version(self, info: ProjectInfo, checkout: Path) -> Path:
        git_hash = await _run("git", "rev-parse", "--short", "HEAD", cwd=str(checkout))
        version_dir = self.versions_dir / f"{info.name}-{git_hash}"
        if not version_dir.exists():
            shutil.copytree(checkout, version_dir)
        return version_dir

    async def has_update(self, info: ProjectInfo, current_version_dir: Path) -> bool:
        """True if the project's upstream HEAD differs from the running pinned version."""
        if info.source is ProjectSource.DOCKER:
            return False  # docker-image update detection is M4
        checkout = self.projects_dir / info.name
        if not (checkout / ".git").exists():
            return False
        await _run("git", "fetch", "--depth", "1", "origin", cwd=str(checkout))
        latest = await _run("git", "rev-parse", "--short", "origin/HEAD", cwd=str(checkout))
        return current_version_dir.name != f"{info.name}-{latest}"

    def acquire(self, version_dir: Path) -> None:
        self._refcounts[version_dir] = self._refcounts.get(version_dir, 0) + 1

    def release(self, version_dir: Path) -> None:
        """Decrement; delete the version dir only when no worker references it."""
        remaining = self._refcounts.get(version_dir, 0) - 1
        if remaining <= 0:
            self._refcounts.pop(version_dir, None)
            shutil.rmtree(version_dir, ignore_errors=True)
        else:
            self._refcounts[version_dir] = remaining
