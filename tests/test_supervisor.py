"""Supervisor tests: budget split, status-line parsing, and reconcile — with fake backend
and fake installer, so no real subprocesses, git, or network."""

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
        self.status = WorkerStatus(
            project=project, state=WorkerState.RUNNING, concurrent_items=concurrent_items
        )

    async def stop(self, graceful=True):
        self.stopped = True

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
    worker = sup.workers["reddit"]
    config.project("reddit").enabled = False
    await sup.reconcile()
    assert set(sup.workers) == {"urls"}
    assert worker.stopped is True
    assert pathlib.Path("/versions/reddit") in sup.installer.released
    # urls is alone now => gets the whole budget of 6, so it was restarted at the new slice
    assert sup.workers["urls"].concurrent_items == 6


async def test_reconcile_restarts_on_budget_change(tmp_path):
    config, sup = make_supervisor(tmp_path, max_items=6)
    config.project("urls").enabled = True
    await sup.reconcile()
    assert sup.workers["urls"].concurrent_items == 6
    config.global_config.max_concurrent_items = 2
    await sup.reconcile()
    assert sup.workers["urls"].concurrent_items == 2
    assert len(sup.backend.spawns) == 2  # started, then restarted at new budget


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


async def test_download_rate_splits_global_limit(tmp_path):
    config, sup = make_supervisor(tmp_path, max_items=6)
    config.global_config.bandwidth_limit = 0
    assert sup.download_rate() == 0  # unlimited by default
    config.project("urls").enabled = True
    config.project("reddit").enabled = True
    config.global_config.bandwidth_limit = 6_000_000  # 6 MB/s across 6 slots (3 + 3)
    assert sup.download_rate() == 1_000_000


async def test_bandwidth_change_restarts_workers(tmp_path):
    config, sup = make_supervisor(tmp_path, max_items=4)
    config.project("urls").enabled = True
    await sup.reconcile()
    assert sup.workers["urls"].limit_rate == 0
    config.global_config.bandwidth_limit = 4_000_000  # 4 MB/s over 4 slots => 1 MB/s each
    await sup.reconcile()
    assert sup.workers["urls"].limit_rate == 1_000_000
    assert len(sup.backend.spawns) == 2  # restarted to apply the new rate


async def test_snapshot_reports_all_workers(tmp_path):
    config, sup = make_supervisor(tmp_path)
    config.project("urls").enabled = True
    config.project("reddit").enabled = True
    await sup.reconcile()
    snap = {w.project for w in sup.snapshot()}
    assert snap == {"urls", "reddit"}
