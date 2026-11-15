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
import contextlib
import json
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .config import ConfigStore
from .models import PROJECT_THREADS_MAX, ItemStatus, ProjectInfo, WorkerState, WorkerStatus
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


def clamp_threads(value: int) -> int:
    """Clamp a per-project thread count to the allowed 1..PROJECT_THREADS_MAX range."""
    return max(1, min(PROJECT_THREADS_MAX, int(value)))


class WorkerHandle(Protocol):
    project: str
    concurrent_items: int
    limit_rate: int
    version_dir: Path

    async def stop(self, graceful: bool = True, timeout: float | None = 30.0) -> None: ...
    async def reconfigure(self, concurrent_items: int, limit_rate: int = 0) -> None: ...
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

    async def stop(self, graceful: bool = True, timeout: float | None = 30.0) -> None:
        """Signal the worker and wait for it to exit.

        ``graceful`` sends SIGINT (the worker drains: finishes in-flight items, stops taking
        new ones, exits); otherwise SIGTERM. ``timeout`` caps the wait before a hard SIGKILL;
        ``None`` waits indefinitely (used for drain-to-completion on retire).
        """
        self.status.state = WorkerState.STOPPING
        try:
            self._proc.send_signal(signal.SIGINT if graceful else signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            if timeout is None:
                await self._proc.wait()
            else:
                await asyncio.wait_for(self._proc.wait(), timeout=timeout)
        except TimeoutError:
            self._proc.kill()
        finally:
            self._reader.cancel()

    async def reconfigure(self, concurrent_items: int, limit_rate: int = 0) -> None:
        """Change the running worker's slot count / bandwidth live, without a restart.

        Writes a control command to the worker's stdin; the worker grows/shrinks its pool
        and re-points wget's rate cap, leaving in-flight items untouched.
        """
        cmd = {
            "cmd": "reconfigure",
            "concurrent_items": int(concurrent_items),
            "limit_rate": int(limit_rate),
        }
        if self._proc.stdin is not None:
            self._proc.stdin.write((json.dumps(cmd) + "\n").encode("utf-8"))
            with contextlib.suppress(Exception):
                await self._proc.stdin.drain()
        self.concurrent_items = int(concurrent_items)
        self.limit_rate = int(limit_rate)
        self.status.concurrent_items = int(concurrent_items)

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
            stdin=asyncio.subprocess.PIPE,  # live control channel (reconfigure commands)
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
        drain_timeout: float | None = 3600.0,
    ):
        self.config = config
        self.installer = installer
        self.backend = backend
        self.data_dir = data_dir
        # Disabled / updated workers drain in the background (finish in-flight items) rather
        # than block reconcile. drain_timeout caps a wedged worker before a hard kill so it
        # can't leak a process + version-dir reference forever; None = wait indefinitely.
        self.drain_timeout = drain_timeout
        self.workers: dict[str, WorkerHandle] = {}
        self.catalog: dict[str, ProjectInfo] = {}
        self._draining: set[WorkerHandle] = set()
        self._drain_tasks: set[asyncio.Task[None]] = set()
        self._shutting_down = False
        self._lock = asyncio.Lock()

    def set_catalog(self, projects: list[ProjectInfo]) -> None:
        self.catalog = {p.name: p for p in projects}

    def _desired(self) -> list[str]:
        return [n for n in self.config.enabled_projects() if n in self.catalog]

    def budget(self) -> dict[str, int]:
        """Per-project thread counts — absolute, not a divided global pie.

        Each enabled project runs its own ``concurrent_items`` (clamped 1..MAX), falling back
        to ``default_project_threads`` when unset. Disabling a project never changes another's
        count, so a rate-limited project can never be pushed above its configured threads.
        """
        default = self.config.global_config.default_project_threads
        return {
            n: clamp_threads(self.config.project(n).concurrent_items or default)
            for n in self._desired()
        }

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
            # No downloader identity => can't run anything; drain everything and wait.
            if not self.config.global_config.downloader:
                for name in list(self.workers):
                    self._retire(name)
                return
            desired = self._desired()
            budget = self.budget()
            rate = self.download_rate()
            for name in list(self.workers):
                if name not in desired:
                    self._retire(name)
            for name in desired:
                want = budget.get(name, 1)
                current = self.workers.get(name)
                if current is None:
                    await self._start(name, want, rate)
                elif self._is_dead(current):
                    # A crashed/exited worker: respawn it on the SAME pinned code.
                    await self._restart(name, want, rate)
                elif current.concurrent_items != want or getattr(current, "limit_rate", 0) != rate:
                    # Budget / bandwidth changed: reconfigure the live worker in place so its
                    # in-flight items keep running — no restart, no re-install.
                    await current.reconfigure(want, rate)

    @staticmethod
    def _is_dead(worker: WorkerHandle) -> bool:
        return worker.snapshot().state in (WorkerState.FAILED, WorkerState.STOPPED)

    async def restart(self, name: str) -> None:
        """Operator-triggered restart of one project: retire the old instance (it drains in
        the background), then reconcile brings up a fresh one."""
        async with self._lock:
            if name in self.workers:
                self._retire(name)
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
                self._retire(name)
        if stale:
            await self.reconcile()

    async def _start(self, name: str, concurrent_items: int, limit_rate: int = 0) -> None:
        """Install/pin the project (acquires its version dir) and spawn a worker on it."""
        info = self.catalog[name]
        version_dir = await self.installer.prepare(info)
        await self._spawn(name, version_dir, concurrent_items, limit_rate)

    async def _spawn(
        self, name: str, version_dir: Path, concurrent_items: int, limit_rate: int = 0
    ) -> None:
        self.workers[name] = await self.backend.spawn(
            name,
            version_dir,
            concurrent_items,
            self.config.global_config.downloader,
            self.data_dir,
            limit_rate=limit_rate,
        )

    async def _restart(self, name: str, concurrent_items: int, limit_rate: int = 0) -> None:
        """Replace a dead worker, REUSING its pinned version dir (no release, no re-install).

        The version dir is still referenced across the restart, so it must not be released;
        re-installing would needlessly re-fetch over the network (and could fail, leaving the
        project permanently stopped).
        """
        old = self.workers.pop(name)
        await old.stop()
        await self._spawn(name, old.version_dir, concurrent_items, limit_rate)

    def _retire(self, name: str) -> None:
        """Remove a worker from the active set and let it drain in the background.

        The worker finishes its in-flight items (graceful SIGINT) on its own timeline, so
        reconcile never blocks on the drain. Its version dir is released only once the
        process has actually exited (in ``_drain``), which also avoids deleting code out from
        under a still-running worker.
        """
        worker = self.workers.pop(name)
        self._draining.add(worker)
        task = asyncio.create_task(self._drain(worker))
        self._drain_tasks.add(task)
        task.add_done_callback(self._drain_tasks.discard)

    async def _drain(self, worker: WorkerHandle) -> None:
        try:
            await worker.stop(graceful=True, timeout=self.drain_timeout)
        finally:
            self._draining.discard(worker)
            # During shutdown, stop_all owns the release so it happens exactly once.
            if not self._shutting_down:
                self.installer.release(worker.version_dir)

    def snapshot(self) -> list[WorkerStatus]:
        return [w.snapshot() for w in self.workers.values()]

    async def stop_all(self, graceful: bool = True) -> None:
        """Shut everything down with a bounded wait — active and still-draining workers."""
        self._shutting_down = True  # _drain tasks stop releasing; stop_all releases once
        for task in list(self._drain_tasks):
            task.cancel()
        async with self._lock:
            workers = [*self.workers.values(), *self._draining]
            self.workers.clear()
            self._draining.clear()
        for worker in workers:
            with contextlib.suppress(Exception):
                await worker.stop(graceful=graceful, timeout=30.0)
            self.installer.release(worker.version_dir)
