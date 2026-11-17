"""Per-item cleanup: an item's files are deleted when it ends, whatever the outcome.
Offline; real filesystem under tmp_path."""

import asyncio
import os
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "worker"))

from runner import GracefulStop, Runner
from scratch import ScratchSpace
from seesaw.context import RunContext
from seesaw.pipeline import Pipeline
from seesaw.task import SimpleTask, Task

# --- a pipeline shaped like the real projects' --------------------------------------------


class PrepareDirectories(SimpleTask):
    """As in the project pipelines: a working dir under item["data_dir"], WARC inside it."""

    def __init__(self):
        super().__init__("PrepareDirectories")

    def process(self, item):
        item["item_dir"] = os.path.join(item["data_dir"], "0123abcd")
        os.makedirs(item["item_dir"])
        item["warc_file_base"] = "demo-" + item.item_id
        with open("{item_dir}/{warc_file_base}.warc.zst".format(**item), "wb") as f:
            f.write(b"w" * 4096)


class MoveFiles(SimpleTask):
    """As in the project pipelines: finished WARC moved up into data_dir, item_dir removed."""

    def __init__(self):
        super().__init__("MoveFiles")

    def process(self, item):
        os.rename(
            "{item_dir}/{warc_file_base}.warc.zst".format(**item),
            "{data_dir}/{warc_file_base}.warc.zst".format(**item),
        )
        shutil.rmtree(item["item_dir"])


class Upload(Task):
    """Stands in for UploadWithTracker: reads the file from data_dir, never deletes it."""

    def __init__(self, sink, fail_on=()):
        super().__init__("Upload")
        self.sink = sink
        self.fail_on = set(fail_on)

    async def run(self, item, ctx):
        path = "{data_dir}/{warc_file_base}.warc.zst".format(**item)
        self.sink.append(os.path.getsize(path))
        if len(self.sink) in self.fail_on:
            raise RuntimeError("upload target refused")


class StopAfter(Task):
    def __init__(self, n):
        super().__init__("StopAfter")
        self.n = n
        self.seen = 0

    async def run(self, item, ctx):
        self.seen += 1
        if self.seen > self.n:
            raise GracefulStop


def files_under(path):
    return sorted(str(p.relative_to(path)) for p in pathlib.Path(path).rglob("*") if p.is_file())


def test_finished_items_leave_nothing_on_disk(tmp_path):
    scratch = ScratchSpace(tmp_path / "data" / "demo")
    scratch.open()
    sink = []
    pipe = Pipeline(StopAfter(5), PrepareDirectories(), MoveFiles(), Upload(sink, fail_on={2}))
    runner = Runner(
        pipe,
        concurrent_items=2,
        ctx=RunContext(data_dir=str(tmp_path / "data")),
        fail_delay=0,
        scratch=scratch,
    )
    asyncio.run(runner.run())
    assert len(sink) == 5 and set(sink) == {4096}  # every item reached the upload
    assert runner.completed == 4 and runner.failed == 1
    # Uploaded, failed and stopped items alike: no WARCs, no item dirs left behind.
    assert files_under(scratch.root) == [f"{scratch.run_dir.name}.lock"]
    assert list(scratch.run_dir.iterdir()) == []
    scratch.close()
    assert list(scratch.root.iterdir()) == []


def test_each_item_gets_its_own_directory(tmp_path):
    scratch = ScratchSpace(tmp_path / "proj")
    scratch.open()
    seen = []

    class Capture(Task):
        async def run(self, item, ctx):
            seen.append(item["data_dir"])
            assert os.path.isdir(item["data_dir"])

    runner = Runner(
        Pipeline(StopAfter(3), Capture("c")),
        1,
        RunContext(data_dir=str(tmp_path)),
        scratch=scratch,
    )
    asyncio.run(runner.run())
    assert len(set(seen)) == 3
    assert all(pathlib.Path(d).parent == scratch.run_dir for d in seen)
    assert not any(os.path.exists(d) for d in seen)
    scratch.close()


def test_sweep_removes_dead_runs_and_keeps_live_ones(tmp_path):
    root = tmp_path / "proj"
    live = ScratchSpace(root)
    live.open()
    keep = pathlib.Path(live.item_dir("inflight"))
    (keep / "big.warc.zst").write_bytes(b"x")

    # A run whose worker was SIGKILLed: lock file present but nobody holds it.
    dead = root / "run-1-dead00"
    (dead / "item").mkdir(parents=True)
    (dead / "item" / "stale.warc.zst").write_bytes(b"x")
    (root / "run-1-dead00.lock").touch()
    # A directory with no lock file at all.
    (root / "run-2-orphan" / "item").mkdir(parents=True)
    # A lock file with no directory.
    (root / "run-3-gone00.lock").touch()

    fresh = ScratchSpace(root)
    fresh.open()  # sweeps on open
    names = sorted(p.name for p in root.iterdir())
    assert names == sorted(
        [
            live.run_dir.name,
            f"{live.run_dir.name}.lock",
            fresh.run_dir.name,
            f"{fresh.run_dir.name}.lock",
        ]
    )
    assert (keep / "big.warc.zst").exists()  # the live worker's in-flight item survives
    fresh.close()
    live.close()


def test_release_only_deletes_item_dirs(tmp_path):
    scratch = ScratchSpace(tmp_path / "proj")
    scratch.open()
    (tmp_path / "keep").mkdir()
    scratch.release(str(tmp_path / "keep"))
    scratch.release(str(scratch.root))
    assert (tmp_path / "keep").exists() and scratch.root.exists()
    scratch.close()
