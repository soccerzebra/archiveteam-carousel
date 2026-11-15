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
    project, pipeline = run_pipeline.load_pipeline(str(FAKE), downloader="josh")
    assert project.title == "Demo project"
    assert len(pipeline.tasks) == 6  # GetItem, PrepareDirs, Wget, Stats, Upload(limited), Done


def test_downloader_injected_into_requests():
    _, pipeline = run_pipeline.load_pipeline(str(FAKE), downloader="josh")
    get_item = pipeline.tasks[0]
    assert isinstance(get_item, t.GetItemFromTracker)
    assert get_item.body() == {"downloader": "josh", "version": "20261101.01"}


def test_upload_task_built_through_limit_concurrent():
    _, pipeline = run_pipeline.load_pipeline(str(FAKE), downloader="josh")
    limited = pipeline.tasks[4]
    assert isinstance(limited, LimitConcurrent)
    upload = limited.inner_task
    assert isinstance(upload, t.UploadWithTracker)
    item = Item(None, {"item_name": "abc"})
    assert upload.body(item) == {"downloader": "josh", "item_name": "abc", "version": "20261101.01"}


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
    _, pipeline = run_pipeline.load_pipeline(str(RUNNABLE), downloader="josh")

    request_body = json.dumps({"item_name": "item-001", "items": ["item-001"]})
    http = FakeHTTP([FakeResp(200, request_body), FakeResp(200, "OK")])
    ctx = RunContext(http=http, data_dir="/tmp", downloader="josh")

    item = Item()
    asyncio.run(pipeline.run_item(item, ctx))

    assert item["item_name"] == "item-001"
    assert item["stats"]["item"] == "item-001"
    # the two tracker calls, in order: claim then done
    assert http.calls[0]["url"].endswith("/request")
    assert http.calls[1]["url"].endswith("/done")
    # the claim carried the injected downloader
    assert json.loads(http.calls[0]["content"]) == {"downloader": "josh", "version": "1"}


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
