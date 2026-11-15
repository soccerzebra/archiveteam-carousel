"""TrackerClient tests using a fake async transport — no httpx, no network.

The client depends only on an object with ``async post(url, headers=, content=)`` whose
result has ``.status_code`` and ``.text``; httpx.AsyncClient satisfies that in production,
this fake satisfies it in tests. ``sleep`` is stubbed so backoff costs no real time.
"""

import asyncio
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "worker"))

from seesaw import tracker as t  # noqa: E402

FIX = HERE / "fixtures" / "urls_tracker"
TRACKER = "https://legacy-api.arpa.li/urls"


class FakeResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class FakeHTTP:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def post(self, url, headers=None, content=None):
        self.calls.append({"url": url, "headers": headers, "content": content})
        return self._responses.pop(0)


async def _nosleep(_seconds):
    return None


def client(responses):
    http = FakeHTTP(responses)
    return t.TrackerClient(f"{TRACKER}/multi=1", http, sleep=_nosleep), http


def test_get_item_polls_past_404_then_succeeds():
    body = (FIX / "request_response.json").read_text()
    c, http = client([FakeResp(404, ""), FakeResp(200, body)])
    data = asyncio.run(c.get_item("testhandle", "20261027.01"))
    assert data["item_name"] == "a89f2c1b\0b7d3e4f5"
    assert len(http.calls) == 2
    assert http.calls[0]["url"] == f"{TRACKER}/multi=1/request"
    # the posted body is exactly the pinned request contract
    assert json.loads(http.calls[0]["content"]) == {"downloader": "testhandle", "version": "20261027.01"}
    assert http.calls[0]["headers"]["User-Agent"].startswith("ArchiveTeam Warrior/")


def test_get_item_retries_empty_200():
    c, http = client([FakeResp(200, json.dumps({})), FakeResp(200, (FIX / "request_response.json").read_text())])
    data = asyncio.run(c.get_item("testhandle", "20261027.01"))
    assert "item_name" in data and len(http.calls) == 2


def test_get_item_raises_on_out_of_date():
    c, _ = client([FakeResp(455, "")])
    try:
        asyncio.run(c.get_item("testhandle", "old"))
        assert False, "expected TrackerError"
    except t.TrackerError as e:
        assert e.classification.status is t.Status.OUT_OF_DATE


def test_upload_returns_rsync_target():
    c, http = client([FakeResp(200, (FIX / "upload_response.json").read_text())])
    target = asyncio.run(c.upload("testhandle", "a89f2c1b", "20261027.01"))
    assert target == t.UploadTarget("rsync", "rsync://legacy-rsync.arpa.li/urls/")
    assert http.calls[0]["url"] == f"{TRACKER}/multi=1/upload"
    assert json.loads(http.calls[0]["content"])["item_name"] == "a89f2c1b"


def test_send_done_ok_then_retry():
    # first a transient 599, then OK
    c, http = client([FakeResp(599, ""), FakeResp(200, "OK")])
    assert asyncio.run(c.send_done({"item": "a89f2c1b", "bytes": {"data": 1}})) is True
    assert len(http.calls) == 2
    assert http.calls[1]["url"] == f"{TRACKER}/multi=1/done"


def test_send_done_rejects_unexpected_body():
    c, _ = client([FakeResp(200, "NOPE")])
    try:
        asyncio.run(c.send_done({"item": "x"}))
        assert False, "expected TrackerError"
    except t.TrackerError as e:
        assert e.classification.status is t.Status.ERROR


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
