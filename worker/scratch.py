"""Per-item scratch space: every item gets its own data directory, removed when it ends.

Pipelines write everything an item produces under ``item["data_dir"]``: PrepareDirectories
makes its working dir there, MoveFiles moves the finished WARC there, and UploadWithTracker
uploads from there. None of them delete what they leave behind, so the runner owns the
directory's lifetime: created fresh when the item starts, removed when the item completes,
fails, or is cancelled.

Layout, one tree per project::

    <data_dir>/<project>/
        run-<pid>-<rand>.lock      flock held for the life of one worker process
        run-<pid>-<rand>/          that worker's items
            <item_id>/             item["data_dir"]

A worker killed outright (SIGKILL, OOM, container stop) can't clean up after itself, so on
startup each worker removes every run directory whose lock nobody holds. Lock-holding runs
belong to live workers (e.g. an old version still draining next to its replacement) and are
left alone.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import uuid
from pathlib import Path


class ScratchSpace:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.run_dir: Path | None = None
        self._lock_fd: int | None = None
        self._lock_path: Path | None = None

    def open(self) -> None:
        """Claim a run directory for this process, then sweep runs left by dead workers."""
        self.root.mkdir(parents=True, exist_ok=True)
        while True:
            name = f"run-{os.getpid()}-{uuid.uuid4().hex[:6]}"
            lock_path = self.root / f"{name}.lock"
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX)
            # A sweeper may have unlinked the lock file between our create and our flock;
            # only a lock that is still reachable at its path protects the run directory.
            try:
                if os.stat(lock_path).st_ino == os.fstat(fd).st_ino:
                    break
            except FileNotFoundError:
                pass
            os.close(fd)
        self._lock_fd, self._lock_path = fd, lock_path
        self.run_dir = self.root / name
        self.run_dir.mkdir()
        self.sweep()

    def sweep(self) -> list[str]:
        """Remove run directories (and lock files) not held by a live worker."""
        removed = []
        own = self.run_dir.name if self.run_dir else None
        entries: list[os.DirEntry] = []
        with contextlib.suppress(FileNotFoundError):
            entries = list(os.scandir(self.root))
        for entry in entries:
            if not entry.name.startswith("run-") or entry.name == own:
                continue
            if entry.name.endswith(".lock"):
                if entry.name[: -len(".lock")] == own:
                    continue
                run = self.root / entry.name[: -len(".lock")]
                if not run.exists() and self._try_reap(Path(entry.path), None):
                    removed.append(entry.name)
                continue
            if entry.is_dir(follow_symlinks=False):
                lock = self.root / f"{entry.name}.lock"
                if not lock.exists():
                    # The lock file is always created before its directory, so a directory
                    # without one is an orphan.
                    shutil.rmtree(entry.path, ignore_errors=True)
                    removed.append(entry.name)
                elif self._try_reap(lock, Path(entry.path)):
                    removed.append(entry.name)
        return removed

    @staticmethod
    def _try_reap(lock: Path, run: Path | None) -> bool:
        try:
            fd = os.open(lock, os.O_RDWR)
        except FileNotFoundError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False  # a live worker holds it
            if run is not None:
                shutil.rmtree(run, ignore_errors=True)
            with contextlib.suppress(FileNotFoundError):
                lock.unlink()
            return True
        finally:
            os.close(fd)

    def item_dir(self, item_id: str) -> str:
        """A fresh, empty directory for one item."""
        assert self.run_dir is not None, "ScratchSpace.open() first"
        path = self.run_dir / item_id
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True)
        return str(path)

    def release(self, path: str | None) -> None:
        """Delete an item's directory and everything in it."""
        if not path or self.run_dir is None:
            return
        p = Path(path)
        if p.parent != self.run_dir:  # only ever delete what item_dir() handed out
            return
        shutil.rmtree(p, ignore_errors=True)

    def close(self) -> None:
        if self.run_dir is not None:
            shutil.rmtree(self.run_dir, ignore_errors=True)
        if self._lock_path is not None:
            with contextlib.suppress(FileNotFoundError):
                self._lock_path.unlink()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
        self.run_dir = self._lock_fd = self._lock_path = None
