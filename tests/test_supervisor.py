"""Supervisor tests: budget split, status-line parsing, and reconcile — with fake backend
and fake installer, so no real subprocesses, git, or network."""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from controlplane.config import ConfigStore
from controlplane.models import ProjectInfo, ProjectSource, WorkerState, WorkerStatus
from controlplane.supervisor import Supervisor, apply_status_line, split_budget


class FakeWorker:
    def __init__(self, project, version_dir, concurrent_items, limit_rate=0):
        self.project = project
        self.version_dir = version_dir
        self.concurrent_items = concurrent_items
        self.limit_rate = limit_rate
        self.stopped = False
        self._gate = None  # when set, stop() blocks until release_stop() — models a slow drain
        self.status = WorkerStatus(
            project=project, state=WorkerState.RUNNING, concurrent_items=concurrent_items
        )

    def block_stop(self):
        self._gate = asyncio.Event()

    def release_stop(self):
        if self._gate is not None:
            self._gate.set()

    async def stop(self, graceful=True, timeout=30.0):
        if self._gate is not None:
            await self._gate.wait()
        self.stopped = True

    async def reconfigure(self, concurrent_items, limit_rate=0):
        self.concurrent_items = concurrent_items
        self.limit_rate = limit_rate
        self.status.concurrent_items = concurrent_items

    def snapshot(self):
        return self.status


class FakeBackend:
    def __init__(self):
        self.spawns = []

    async def spawn(
        self, project, version_dir, concurrent_items, downloader, data_dir, limit_rate=0
    ):
        self.spawns.append((project, concurrent_items, downloader))
        return FakeWorker(project, version_dir, concurrent_items, limit_rate)


class FakeInstaller:
    def __init__(self):
        self.prepared = []
        self.released = []

    async def prepare(self, info):
        self.prepared.append(info.name)
        return pathlib.Path(f"/versions/{info.name}")

    def release(self, version_dir):
        self.released.append(version_dir)


async def _finish_drains(sup):
    """Wait for all background drain tasks to complete (release their version dirs)."""
    await asyncio.gather(*list(sup._drain_tasks))


def make_supervisor(tmp_path, downloader="josh", max_items=6):
    config = ConfigStore(pathlib.Path(tmp_path) / "config.json")
    config.global_config.downloader = downloader
    config.global_config.max_concurrent_items = max_items
    sup = Supervisor(config, FakeInstaller(), FakeBackend(), data_dir="/data")
    sup.set_catalog(
        [
            ProjectInfo(name="urls", title="URLs", source=ProjectSource.GIT, repository="git://u"),
            ProjectInfo(
                name="reddit", title="Reddit", source=ProjectSource.GIT, repository="git://r"
            ),
        ]
    )
    return config, sup


# --- pure helpers ------------------------------------------------------------------


def test_split_budget_equal():
    assert split_budget(["a", "b"], 6, {}) == {"a": 3, "b": 3}


def test_split_budget_with_override():
    assert split_budget(["urls", "reddit"], 6, {"urls": 2}) == {"urls": 2, "reddit": 4}


def test_split_budget_minimum_one():
    assert split_budget(["a", "b", "c", "d", "e"], 2, {}) == dict.fromkeys("abcde", 1)


def test_apply_status_line_progression():
    s = WorkerStatus(project="urls")
    apply_status_line(s, '{"event":"loaded","title":"URLs"}')
    assert s.title == "URLs"
    apply_status_line(s, '{"event":"running","concurrent_items":3}')
    assert s.state is WorkerState.RUNNING and s.concurrent_items == 3
    apply_status_line(s, '{"event":"item_end","completed":5,"failed":1,"bytes":1000}')
    assert s.items_done == 5 and s.items_failed == 1
    apply_status_line(s, '{"event":"item_end","completed":6,"failed":1,"bytes":500}')
    assert s.items_done == 6 and s.bytes_uploaded == 1500  # bytes accumulate across items
    apply_status_line(s, '{"event":"stopped","completed":6,"failed":1}')
    assert s.state is WorkerState.STOPPED and s.items_done == 6


def test_apply_status_line_threads_populates_active_items():
    s = WorkerStatus(project="urls")
    apply_status_line(
        s,
        '{"event":"threads","threads":['
        '{"slot":0,"item_id":"a1","item_name":"job A","task":"WgetDownload",'
        '"started":123.0,"bytes_up":500},'
        '{"slot":1,"item_id":"b2","item_name":"job B","task":"RsyncUpload"}]}',
    )
    assert [t.slot for t in s.active_items] == [0, 1]
    assert s.active_items[0].task == "WgetDownload" and s.active_items[0].bytes_up == 500
    assert s.active_items[1].item_name == "job B"
    # a stop clears the live threads
    apply_status_line(s, '{"event":"stopped","completed":2,"failed":0}')
    assert s.active_items == []


def test_apply_status_line_returns_event():
    s = WorkerStatus(project="urls")
    event = apply_status_line(s, '{"event":"item_end","completed":1,"failed":0,"bytes":42}')
    assert event == {"event": "item_end", "completed": 1, "failed": 0, "bytes": 42}
    assert apply_status_line(s, "not json") is None
    assert apply_status_line(s, "") is None


def test_apply_status_line_ignores_garbage():
    s = WorkerStatus(project="urls")
    apply_status_line(s, "not json")
    apply_status_line(s, "")
    assert s.state is WorkerState.STARTING  # unchanged


# --- reconcile ---------------------------------------------------------------------


async def test_reconcile_starts_enabled_projects(tmp_path):
    config, sup = make_supervisor(tmp_path)
    config.project("urls").enabled = True
    config.project("reddit").enabled = True
    await sup.reconcile()
    assert set(sup.workers) == {"urls", "reddit"}
    # 6 items split across 2 projects => 3 each
    assert {p: ci for p, ci, _ in sup.backend.spawns} == {"urls": 3, "reddit": 3}


async def test_reconcile_stops_disabled_project(tmp_path):
    config, sup = make_supervisor(tmp_path)
    config.project("urls").enabled = True
    config.project("reddit").enabled = True
    await sup.reconcile()
    reddit = sup.workers["reddit"]
    urls = sup.workers["urls"]
    config.project("reddit").enabled = False
    await sup.reconcile()
    assert set(sup.workers) == {"urls"}
    # urls is alone now => gets the whole budget of 6, applied live (same worker, no respawn).
    assert sup.workers["urls"] is urls
    assert urls.concurrent_items == 6
    assert len(sup.backend.spawns) == 2  # urls + reddit started once each; urls not respawned
    # reddit drains in the background, then is stopped and released.
    await _finish_drains(sup)
    assert reddit.stopped is True
    assert pathlib.Path("/versions/reddit") in sup.installer.released


async def test_reconcile_reconfigures_on_budget_change(tmp_path):
    config, sup = make_supervisor(tmp_path, max_items=6)
    config.project("urls").enabled = True
    await sup.reconcile()
    worker = sup.workers["urls"]
    assert worker.concurrent_items == 6
    config.global_config.max_concurrent_items = 2
    await sup.reconcile()
    # Same process, reconfigured live: in-flight items keep running.
    assert sup.workers["urls"] is worker
    assert worker.concurrent_items == 2
    assert len(sup.backend.spawns) == 1  # never respawned


async def test_reconcile_without_downloader_runs_nothing(tmp_path):
    config, sup = make_supervisor(tmp_path, downloader="")
    config.project("urls").enabled = True
    await sup.reconcile()
    assert sup.workers == {}


async def test_reconcile_restarts_crashed_worker(tmp_path):
    config, sup = make_supervisor(tmp_path)
    config.project("urls").enabled = True
    await sup.reconcile()
    crashed = sup.workers["urls"]
    crashed.status.state = WorkerState.FAILED  # simulate the process dying
    await sup.reconcile()
    assert sup.workers["urls"] is not crashed  # replaced with a fresh worker
    assert sup.workers["urls"].snapshot().state is WorkerState.RUNNING
    assert len(sup.backend.spawns) == 2
    # Non-destructive: reused the pinned dir (no re-install, no release).
    assert sup.installer.prepared == ["urls"]
    assert sup.installer.released == []


async def test_check_updates_restarts_stale_worker(tmp_path):
    config, sup = make_supervisor(tmp_path)
    config.project("urls").enabled = True
    await sup.reconcile()
    original = sup.workers["urls"]

    async def has_update(info, version_dir):  # installer reports an upstream change
        return True

    sup.installer.has_update = has_update

    await sup.check_updates()
    assert sup.workers["urls"] is not original  # restarted on update
    assert len(sup.backend.spawns) == 2
    # Code update IS destructive: new code re-prepared, old version released after it drains.
    assert sup.installer.prepared == ["urls", "urls"]
    await _finish_drains(sup)
    assert pathlib.Path("/versions/urls") in sup.installer.released


async def test_download_rate_splits_global_limit(tmp_path):
    config, sup = make_supervisor(tmp_path, max_items=6)
    config.global_config.bandwidth_limit = 0
    assert sup.download_rate() == 0  # unlimited by default
    config.project("urls").enabled = True
    config.project("reddit").enabled = True
    config.global_config.bandwidth_limit = 6_000_000  # 6 MB/s across 6 slots (3 + 3)
    assert sup.download_rate() == 1_000_000


async def test_bandwidth_change_reconfigures_workers(tmp_path):
    config, sup = make_supervisor(tmp_path, max_items=4)
    config.project("urls").enabled = True
    await sup.reconcile()
    worker = sup.workers["urls"]
    assert worker.limit_rate == 0
    config.global_config.bandwidth_limit = 4_000_000  # 4 MB/s over 4 slots => 1 MB/s each
    await sup.reconcile()
    assert sup.workers["urls"] is worker  # applied live, not restarted
    assert worker.limit_rate == 1_000_000
    assert len(sup.backend.spawns) == 1


async def test_disable_drains_in_background_then_releases(tmp_path):
    config, sup = make_supervisor(tmp_path)
    config.project("urls").enabled = True
    await sup.reconcile()
    worker = sup.workers["urls"]
    worker.block_stop()  # model a slow-draining worker (item still in flight)

    config.project("urls").enabled = False
    await sup.reconcile()  # must return promptly, without waiting for the drain
    assert "urls" not in sup.workers
    assert worker in sup._draining
    assert worker.stopped is False  # still draining
    assert sup.installer.released == []  # NOT released while the process is alive

    worker.release_stop()  # item finished; process exits
    await _finish_drains(sup)
    assert worker.stopped is True
    assert worker not in sup._draining
    assert pathlib.Path("/versions/urls") in sup.installer.released


async def test_reenable_during_drain_starts_fresh_worker(tmp_path):
    config, sup = make_supervisor(tmp_path)
    config.project("urls").enabled = True
    await sup.reconcile()
    old = sup.workers["urls"]
    old.block_stop()

    config.project("urls").enabled = False
    await sup.reconcile()
    assert old in sup._draining and "urls" not in sup.workers

    config.project("urls").enabled = True
    await sup.reconcile()  # a fresh worker starts while the old one is still draining
    assert "urls" in sup.workers
    assert sup.workers["urls"] is not old
    assert old in sup._draining  # old copy keeps finishing its items

    old.release_stop()
    await _finish_drains(sup)
    assert sup.workers["urls"] is not old  # fresh worker unaffected


async def test_stop_all_stops_active_and_draining(tmp_path):
    config, sup = make_supervisor(tmp_path)
    config.project("urls").enabled = True
    config.project("reddit").enabled = True
    await sup.reconcile()
    urls = sup.workers["urls"]
    reddit = sup.workers["reddit"]
    reddit.block_stop()  # keep reddit in the draining set until shutdown forces it
    config.project("reddit").enabled = False
    await sup.reconcile()
    assert reddit in sup._draining

    reddit.release_stop()  # let shutdown's stop() complete rather than hang on the gate
    await sup.stop_all()
    assert sup.workers == {}
    assert sup._draining == set()
    assert urls.stopped is True and reddit.stopped is True
    # each version dir released exactly once (no double release across drain + shutdown)
    assert sorted(sup.installer.released) == [
        pathlib.Path("/versions/reddit"),
        pathlib.Path("/versions/urls"),
    ]


async def test_snapshot_reports_all_workers(tmp_path):
    config, sup = make_supervisor(tmp_path)
    config.project("urls").enabled = True
    config.project("reddit").enabled = True
    await sup.reconcile()
    snap = {w.project for w in sup.snapshot()}
    assert snap == {"urls", "reddit"}
