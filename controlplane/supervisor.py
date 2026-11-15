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
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .config import ConfigStore
from .models import ItemStatus, ProjectInfo, WorkerState, WorkerStatus
from .projects import ProjectInstaller


def apply_status_event(status: WorkerStatus, event: dict) -> None:
    """Fold one already-parsed worker event into its WorkerStatus."""
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
        status.bytes_uploaded += int(event.get("bytes", 0) or 0)
        # surface the most recent per-item failure reason (cleared on a success)
        status.last_error = event.get("error") or (None if event.get("ok") else status.last_error)
    elif kind == "threads":
        status.active_items = [ItemStatus(**t) for t in event.get("threads", [])]
    elif kind == "stopped":
        status.state = WorkerState.STOPPED
        status.active_items = []
        status.items_done = event.get("completed", status.items_done)
        status.items_failed = event.get("failed", status.items_failed)


def apply_status_line(status: WorkerStatus, line: str) -> dict | None:
    """Parse one JSON status line from a worker, fold it in, and return the event.

    Returns the parsed event (so callers can forward it, e.g. to the stats store) or None
    for blank / non-JSON lines, which are ignored.
    """
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None  # non-JSON output (shouldn't happen on the status channel) is ignored
    apply_status_event(status, event)
    return event


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
    limit_rate: int
    version_dir: Path

    async def stop(self, graceful: bool = True) -> None: ...
    def snapshot(self) -> WorkerStatus: ...


class WorkerBackend(Protocol):
    async def spawn(
        self,
        project: str,
        version_dir: Path,
        concurrent_items: int,
        downloader: str,
        data_dir: str,
        limit_rate: int = 0,
    ) -> WorkerHandle: ...


class SubprocessWorker:
    """A worker running as ``python -m worker.run_pipeline`` in its own process."""

    def __init__(
        self,
        project: str,
        version_dir: Path,
        concurrent_items: int,
        proc: asyncio.subprocess.Process,
        limit_rate: int = 0,
        on_event: Callable[[str, dict], None] | None = None,
    ):
        self.project = project
        self.version_dir = version_dir
        self.concurrent_items = concurrent_items
        self.limit_rate = limit_rate
        self._proc = proc
        self._on_event = on_event
        self.status = WorkerStatus(
            project=project,
            state=WorkerState.STARTING,
            pid=proc.pid,
            concurrent_items=concurrent_items,
        )
        self._reader = asyncio.create_task(self._read_status())

    async def _read_status(self) -> None:
        assert self._proc.stdout is not None
        async for raw in self._proc.stdout:
            event = apply_status_line(self.status, raw.decode("utf-8", "replace"))
            if event and self._on_event:
                self._on_event(self.project, event)
        returncode = await self._proc.wait()
        self.status.active_items = []  # no live threads once the process is gone
        if self.status.state != WorkerState.STOPPED:
            self.status.state = WorkerState.STOPPED if returncode == 0 else WorkerState.FAILED
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
        except TimeoutError:
            self._proc.kill()
        self._reader.cancel()

    def snapshot(self) -> WorkerStatus:
        return self.status


class SubprocessBackend:
    def __init__(self, worker_root: Path, on_event: Callable[[str, dict], None] | None = None):
        # Directory that makes `python -m worker.run_pipeline` importable.
        self.worker_root = str(Path(worker_root).resolve())
        self.on_event = on_event

    async def spawn(
        self, project, version_dir, concurrent_items, downloader, data_dir, limit_rate=0
    ):
        env = dict(os.environ)
        env["PYTHONPATH"] = self.worker_root + os.pathsep + env.get("PYTHONPATH", "")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "worker.run_pipeline",
            "--project",
            project,
            "--pipeline",
            str(Path(version_dir) / "pipeline.py"),
            "--downloader",
            downloader,
            "--concurrent-items",
            str(concurrent_items),
            "--data-dir",
            data_dir,
            "--limit-rate",
            str(int(limit_rate)),
            cwd=self.worker_root,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,  # human/wget logs flow to the control plane's stderr
        )
        return SubprocessWorker(
            project,
            Path(version_dir),
            concurrent_items,
            proc,
            limit_rate=limit_rate,
            on_event=self.on_event,
        )


class Supervisor:
    def __init__(
        self,
        config: ConfigStore,
        installer: ProjectInstaller,
        backend: WorkerBackend,
        data_dir: str = "/home/warrior/data",
    ):
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
        overrides = {n: ci for n in desired if (ci := self.config.project(n).concurrent_items)}
        return split_budget(desired, self.config.global_config.max_concurrent_items, overrides)

    def download_rate(self) -> int:
        """Per-download byte/sec cap: the global limit split across every active slot.

        Each worker runs its ``concurrent_items`` downloads at this rate, so the sum across
        all workers approximates the global ``bandwidth_limit``. 0 means unlimited.
        """
        limit = self.config.global_config.bandwidth_limit
        if limit <= 0:
            return 0
        total_slots = sum(self.budget().values()) or 1
        return max(1, limit // total_slots)

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
            rate = self.download_rate()
            for name in list(self.workers):
                if name not in desired:
                    await self._stop(name)
            for name in desired:
                want = budget.get(name, 1)
                current = self.workers.get(name)
                if current is None:
                    await self._start(name, want, rate)
                elif (
                    self._is_dead(current)
                    or current.concurrent_items != want
                    or getattr(current, "limit_rate", 0) != rate
                ):
                    # restart a crashed/exited worker, or re-slice budget / bandwidth
                    await self._stop(name)
                    await self._start(name, want, rate)

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

    async def _start(self, name: str, concurrent_items: int, limit_rate: int = 0) -> None:
        info = self.catalog[name]
        version_dir = await self.installer.prepare(info)
        self.workers[name] = await self.backend.spawn(
            name,
            version_dir,
            concurrent_items,
            self.config.global_config.downloader,
            self.data_dir,
            limit_rate=limit_rate,
        )

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
