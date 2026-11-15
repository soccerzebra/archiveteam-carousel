"""The supervisor: the heart of the multi-project model.

Owns a dict of workers keyed by project name, so many projects run at once. Each worker
is one OS process running
``worker/run_pipeline.py`` against a pinned project version. The supervisor:

  * reconciles running workers to the set of ENABLED projects (start/stop the diff),
  * divides the global concurrency budget across active projects and passes each its slice,
  * reads each worker's line-delimited JSON status into a WorkerStatus,
  * exposes an aggregate snapshot to the web layer.

The worker backend is abstracted (``WorkerBackend``) so a container backend can be added
later without touching reconcile logic; the default is a subprocess (the user's choice).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, Protocol

from .config import ConfigStore
from .models import ProjectInfo, WorkerState, WorkerStatus
from .projects import ProjectInstaller


def apply_status_line(status: WorkerStatus, line: str) -> None:
    """Fold one JSON status line emitted by a worker into its WorkerStatus."""
    line = line.strip()
    if not line:
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return  # non-JSON output (shouldn't happen on the status channel) is ignored
    kind = event.get("event")
    if kind == "install_hook":
        status.state = WorkerState.STARTING
    elif kind == "loaded":
        status.title = event.get("title", status.title)
    elif kind == "running":
        status.state = WorkerState.RUNNING
        status.concurrent_items = event.get("concurrent_items", status.concurrent_items)
    elif kind == "item_end":
        status.items_done = event.get("completed", status.items_done)
        status.items_failed = event.get("failed", status.items_failed)
    elif kind == "stopped":
        status.state = WorkerState.STOPPED
        status.items_done = event.get("completed", status.items_done)
        status.items_failed = event.get("failed", status.items_failed)


def split_budget(projects: list[str], total: int, overrides: dict[str, int]) -> dict[str, int]:
    """Divide ``total`` concurrent items across ``projects`` (equal split, min 1 each).

    A per-project override wins for that project; the remaining total is split among the
    rest. This is deliberately simple; weighting by HQ priority can come later.
    """
    if not projects:
        return {}
    budget: dict[str, int] = {}
    free = [p for p in projects if p not in overrides]
    for p in projects:
        if p in overrides:
            budget[p] = max(1, overrides[p])
    used = sum(budget.values())
    remaining = max(0, total - used)
    if free:
        each = max(1, remaining // len(free))
        for p in free:
            budget[p] = each
    return budget


class WorkerHandle(Protocol):
    project: str
    concurrent_items: int
    version_dir: Path

    async def stop(self, graceful: bool = True) -> None: ...
    def snapshot(self) -> WorkerStatus: ...


class WorkerBackend(Protocol):
    async def spawn(self, project: str, version_dir: Path, concurrent_items: int,
                    downloader: str, data_dir: str) -> WorkerHandle: ...


class SubprocessWorker:
    """A worker running as ``python -m worker.run_pipeline`` in its own process."""

    def __init__(self, project: str, version_dir: Path, concurrent_items: int,
                 proc: asyncio.subprocess.Process):
        self.project = project
        self.version_dir = version_dir
        self.concurrent_items = concurrent_items
        self._proc = proc
        self.status = WorkerStatus(project=project, state=WorkerState.STARTING,
                                   pid=proc.pid, concurrent_items=concurrent_items)
        self._reader = asyncio.create_task(self._read_status())

    async def _read_status(self) -> None:
        assert self._proc.stdout is not None
        async for raw in self._proc.stdout:
            apply_status_line(self.status, raw.decode("utf-8", "replace"))
        returncode = await self._proc.wait()
        if self.status.state != WorkerState.STOPPED:
            self.status.state = (WorkerState.STOPPED if returncode == 0
                                 else WorkerState.FAILED)
        if returncode not in (0, -signal.SIGINT, -signal.SIGTERM):
            self.status.last_error = f"worker exited with code {returncode}"

    async def stop(self, graceful: bool = True) -> None:
        self.status.state = WorkerState.STOPPING
        try:
            self._proc.send_signal(signal.SIGINT if graceful else signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=30)
        except asyncio.TimeoutError:
            self._proc.kill()
        self._reader.cancel()

    def snapshot(self) -> WorkerStatus:
        return self.status


class SubprocessBackend:
    def __init__(self, worker_root: Path):
        # Directory that makes `python -m worker.run_pipeline` importable.
        self.worker_root = str(Path(worker_root).resolve())

    async def spawn(self, project, version_dir, concurrent_items, downloader, data_dir):
        env = dict(os.environ)
        env["PYTHONPATH"] = self.worker_root + os.pathsep + env.get("PYTHONPATH", "")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "worker.run_pipeline",
            "--project", project,
            "--pipeline", str(Path(version_dir) / "pipeline.py"),
            "--downloader", downloader,
            "--concurrent-items", str(concurrent_items),
            "--data-dir", data_dir,
            cwd=self.worker_root,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,  # human/wget logs flow to the control plane's stderr
        )
        return SubprocessWorker(project, Path(version_dir), concurrent_items, proc)


class Supervisor:
    def __init__(self, config: ConfigStore, installer: ProjectInstaller,
                 backend: WorkerBackend, data_dir: str = "/home/warrior/data"):
        self.config = config
        self.installer = installer
        self.backend = backend
        self.data_dir = data_dir
        self.workers: dict[str, WorkerHandle] = {}
        self.catalog: dict[str, ProjectInfo] = {}
        self._lock = asyncio.Lock()

    def set_catalog(self, projects: list[ProjectInfo]) -> None:
        self.catalog = {p.name: p for p in projects}

    def _desired(self) -> list[str]:
        return [n for n in self.config.enabled_projects() if n in self.catalog]

    def budget(self) -> dict[str, int]:
        desired = self._desired()
        overrides = {n: self.config.project(n).concurrent_items
                     for n in desired if self.config.project(n).concurrent_items}
        return split_budget(desired, self.config.global_config.max_concurrent_items, overrides)

    async def reconcile(self) -> None:
        """Make running workers match the enabled set and the current budget."""
        async with self._lock:
            # No downloader identity => can't run anything; stop everything and wait.
            if not self.config.global_config.downloader:
                for name in list(self.workers):
                    await self._stop(name)
                return
            desired = self._desired()
            budget = self.budget()
            for name in list(self.workers):
                if name not in desired:
                    await self._stop(name)
            for name in desired:
                want = budget.get(name, 1)
                current = self.workers.get(name)
                if current is None:
                    await self._start(name, want)
                elif self._is_dead(current) or current.concurrent_items != want:
                    # restart a crashed/exited worker, or re-slice its budget
                    await self._stop(name)
                    await self._start(name, want)

    @staticmethod
    def _is_dead(worker: WorkerHandle) -> bool:
        return worker.snapshot().state in (WorkerState.FAILED, WorkerState.STOPPED)

    async def restart(self, name: str) -> None:
        """Operator-triggered restart of one project (stop, then reconcile brings it back)."""
        async with self._lock:
            if name in self.workers:
                await self._stop(name)
        await self.reconcile()

    async def check_updates(self) -> None:
        """Restart workers whose project code changed upstream (run on the slow HQ loop)."""
        async with self._lock:
            stale = []
            for name, worker in self.workers.items():
                info = self.catalog.get(name)
                if info and await self.installer.has_update(info, worker.version_dir):
                    stale.append(name)
            for name in stale:
                await self._stop(name)
        if stale:
            await self.reconcile()

    async def _start(self, name: str, concurrent_items: int) -> None:
        info = self.catalog[name]
        version_dir = await self.installer.prepare(info)
        self.workers[name] = await self.backend.spawn(
            name, version_dir, concurrent_items,
            self.config.global_config.downloader, self.data_dir)

    async def _stop(self, name: str) -> None:
        worker = self.workers.pop(name)
        await worker.stop()
        self.installer.release(worker.version_dir)

    def snapshot(self) -> list[WorkerStatus]:
        return [w.snapshot() for w in self.workers.values()]

    async def stop_all(self, graceful: bool = True) -> None:
        async with self._lock:
            for name in list(self.workers):
                await self._stop(name)
