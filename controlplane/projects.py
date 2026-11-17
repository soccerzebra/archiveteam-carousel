"""Install a project's code so a worker can run it.

Two source kinds, both ending in a plain directory containing
``pipeline.py`` that a worker process runs — never a nested container, keeping the
process-per-project model:

  * GIT    : git clone / git pull the repository.
  * DOCKER : `crane export` the image and untar its ``/grab`` dir. Some projects publish no
    git repo; their code (pipeline.py, .lua, warrior-install.sh, bundled data) lives only in
    the image. A docker project's /grab is structurally identical to a git checkout, so once
    extracted it runs through the exact same path (install hook + shared wget-at).

Each install is copied to a version-pinned dir (``<name>-<tag>``: a git short hash, or a
docker image digest prefix) so an in-flight item keeps its exact code even if the project
updates. Version dirs are reference-counted across all active workers: a dir is removed only
when no worker still references it.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .models import ProjectInfo, ProjectSource

_DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})")


async def _run(*args: str, cwd: str | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed ({proc.returncode}):\n{text}")
    return text.strip()


@dataclass
class ProjectInstaller:
    projects_dir: Path  # working checkouts
    versions_dir: Path  # immutable version-pinned copies workers run from
    _refcounts: dict[Path, int] = field(default_factory=dict)

    def __post_init__(self):
        self.projects_dir = Path(self.projects_dir)
        self.versions_dir = Path(self.versions_dir)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.versions_dir.mkdir(parents=True, exist_ok=True)

    async def prepare(self, info: ProjectInfo) -> Path:
        """Install/update the project, pin the current version, acquire it; return the dir."""
        checkout, tag = await self.install(info)
        version_dir = self.pin_version(info, checkout, tag)
        self.acquire(version_dir)
        return version_dir

    async def install(self, info: ProjectInfo) -> tuple[Path, str]:
        """Fetch the project's code into its checkout dir; return (checkout, version_tag)."""
        if info.source is ProjectSource.DOCKER:
            return await self._install_docker(info)
        return await self._install_git(info)

    async def _install_git(self, info: ProjectInfo) -> tuple[Path, str]:
        if not info.repository:
            raise ValueError(f"project {info.name} has no git repository")
        checkout = self.projects_dir / info.name
        if (checkout / ".git").exists():
            await _run("git", "fetch", "--depth", "1", "origin", cwd=str(checkout))
            await _run("git", "reset", "--hard", "origin/HEAD", cwd=str(checkout))
        else:
            if checkout.exists():
                shutil.rmtree(checkout)
            # --recurse-submodules: some projects (e.g. urlteam2's terroroftinytown) keep
            # their client code in a submodule the pipeline imports; a plain clone leaves it
            # empty. --shallow-submodules keeps the extra fetch cheap.
            await _run(
                "git",
                "clone",
                "--depth",
                "1",
                "--recurse-submodules",
                "--shallow-submodules",
                info.repository,
                str(checkout),
            )
        # Make sure submodules are present and current on both clone and fetch paths.
        if (checkout / ".gitmodules").exists():
            await _run(
                "git",
                "submodule",
                "update",
                "--init",
                "--recursive",
                "--depth",
                "1",
                cwd=str(checkout),
            )
        git_hash = await _run("git", "rev-parse", "--short", "HEAD", cwd=str(checkout))
        return checkout, git_hash

    async def _install_docker(self, info: ProjectInfo) -> tuple[Path, str]:
        """Extract a docker-only project's /grab dir with crane (no daemon, no nested container).

        ``crane export`` streams the image's flattened filesystem as a tar; we keep only the
        ``grab/`` subtree (the project's pipeline + assets), stripping the prefix so the dir
        looks exactly like a git checkout. Pinned by image digest.
        """
        if not info.docker:
            raise ValueError(f"project {info.name} has no docker image")
        tag = await self._image_tag(info.docker)
        checkout = self.projects_dir / info.name
        # Do the heavy export + extraction in a container-local temp dir: it's fast, and tar can
        # create symlinks there (e.g. grab/wget-at) — on a macOS bind-mounted volume it cannot.
        # Then copy into the checkout with copytree(symlinks=True), which recreates symlinks via
        # os.symlink (that DOES work on the mount).
        with tempfile.TemporaryDirectory(prefix=f"carousel-{info.name}-") as tmp:
            tar_path = Path(tmp) / "image.tar"
            grab = Path(tmp) / "grab"
            grab.mkdir()
            await _run("crane", "export", info.docker, str(tar_path))
            # keep only grab/*, dropping the leading "grab/" so files land at the root
            await _run(
                "tar", "-x", "-C", str(grab), "--strip-components=1", "-f", str(tar_path), "grab"
            )
            if checkout.exists():
                shutil.rmtree(checkout)
            shutil.copytree(grab, checkout, symlinks=True)
        return checkout, tag

    async def _image_tag(self, image: str) -> str:
        """Short, stable version tag for a docker image: the first 12 hex of its digest."""
        out = await _run("crane", "digest", image)
        m = _DIGEST_RE.search(out)
        if not m:
            raise RuntimeError(f"crane digest {image} returned no sha256 digest:\n{out}")
        return m.group(1)[:12]

    def pin_version(self, info: ProjectInfo, checkout: Path, tag: str) -> Path:
        version_dir = self.versions_dir / f"{info.name}-{tag}"
        if not version_dir.exists():
            shutil.copytree(checkout, version_dir)
        return version_dir

    async def has_update(self, info: ProjectInfo, current_version_dir: Path) -> bool:
        """True if the project's upstream version differs from the running pinned one."""
        if info.source is ProjectSource.DOCKER:
            if not info.docker:
                return False
            tag = await self._image_tag(info.docker)
            return current_version_dir.name != f"{info.name}-{tag}"
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
