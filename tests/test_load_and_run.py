"""Brick #4: load a real-shaped pipeline.py against our seesaw, then run one end to end.

Offline — the fixture pipelines deliberately avoid wget-at/requests/lupa so the loader and
the full task composition run under the system Python. Running the actual `urls` project
(which needs wget-at and project deps) is the Docker integration test documented in the
worker image, not this unit test.
"""

import asyncio
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "worker"))

import run_pipeline
from seesaw import tracker as t
from seesaw.context import RunContext
from seesaw.item import Item
from seesaw.task import LimitConcurrent

FAKE = HERE / "fixtures" / "fake_project" / "pipeline.py"
RUNNABLE = HERE / "fixtures" / "runnable_project" / "pipeline.py"


# --- loading + construction --------------------------------------------------------


def test_loads_project_and_pipeline():
    project, pipeline = run_pipeline.load_pipeline(str(FAKE), downloader="archivist")
    assert project.title == "Demo project"
    assert len(pipeline.tasks) == 6  # GetItem, PrepareDirs, Wget, Stats, Upload(limited), Done


def test_downloader_injected_into_requests():
    _, pipeline = run_pipeline.load_pipeline(str(FAKE), downloader="archivist")
    get_item = pipeline.tasks[0]
    assert isinstance(get_item, t.GetItemFromTracker)
    assert get_item.body() == {"downloader": "archivist", "version": "20261101.01"}


def test_upload_task_built_through_limit_concurrent():
    _, pipeline = run_pipeline.load_pipeline(str(FAKE), downloader="archivist")
    limited = pipeline.tasks[4]
    assert isinstance(limited, LimitConcurrent)
    upload = limited.inner_task
    assert isinstance(upload, t.UploadWithTracker)
    item = Item(None, {"item_name": "abc"})
    assert upload.body(item) == {
        "downloader": "archivist",
        "item_name": "abc",
        "version": "20261101.01",
    }


def test_two_loads_are_independent():
    # Loading twice must not bleed the downloader (no shared module globals like upstream).
    _, p1 = run_pipeline.load_pipeline(str(FAKE), downloader="alice")
    _, p2 = run_pipeline.load_pipeline(str(FAKE), downloader="bob")
    assert p1.tasks[0].body()["downloader"] == "alice"
    assert p2.tasks[0].body()["downloader"] == "bob"


# --- end-to-end against a fake tracker ---------------------------------------------


class FakeResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class FakeHTTP:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def post(self, url, headers=None, content=None):
        self.calls.append({"url": url, "content": content})
        return self._responses.pop(0)


def test_runnable_pipeline_claims_and_reports_done():
    _, pipeline = run_pipeline.load_pipeline(str(RUNNABLE), downloader="archivist")

    request_body = json.dumps({"item_name": "item-001", "items": ["item-001"]})
    http = FakeHTTP([FakeResp(200, request_body), FakeResp(200, "OK")])
    ctx = RunContext(http=http, data_dir="/tmp", downloader="archivist")

    item = Item()
    asyncio.run(pipeline.run_item(item, ctx))

    assert item["item_name"] == "item-001"
    assert item["stats"]["item"] == "item-001"
    # the two tracker calls, in order: claim then done
    assert http.calls[0]["url"].endswith("/request")
    assert http.calls[1]["url"].endswith("/done")
    # the claim carried the injected downloader
    assert json.loads(http.calls[0]["content"]) == {"downloader": "archivist", "version": "1"}


def test_ensure_wget_at_symlinks_into_data_dir():
    """A wget-lua on PATH gets linked under every variant name in the data dir (idempotently)."""
    import os
    import shutil
    import tempfile

    bindir = tempfile.mkdtemp()
    datadir = tempfile.mkdtemp()
    old_path = os.environ.get("PATH", "")
    try:
        fake = pathlib.Path(bindir) / "wget-lua"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        os.environ["PATH"] = bindir + os.pathsep + old_path
        run_pipeline.ensure_wget_at(datadir)
        run_pipeline.ensure_wget_at(datadir)  # second call must be a no-op, not an error
        for name in run_pipeline.WGET_AT_VARIANTS:
            link = pathlib.Path(datadir) / name
            assert link.is_symlink() and os.readlink(link) == str(fake)
    finally:
        os.environ["PATH"] = old_path
        shutil.rmtree(bindir, ignore_errors=True)
        shutil.rmtree(datadir, ignore_errors=True)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
