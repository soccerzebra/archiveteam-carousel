"""Contract test for the Universal Tracker protocol layer.

Pure + offline: exercises request building, response parsing, and status-code semantics
against committed fixtures. No network, no third-party deps (runs under the system Python
like the realize parity test). The fixtures are pinned to the `urls` project and can be
refreshed from the live tracker via scripts/capture_tracker_exchange.py.
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "worker"))  # our clean-room `seesaw`

import seesaw  # noqa: E402
from seesaw import tracker as t  # noqa: E402
from seesaw.item import ItemInterpolation  # noqa: E402

FIX = HERE / "fixtures" / "urls_tracker"

# urls-grab's real tracker wiring (reference/urls-grab/pipeline.py).
TRACKER_URL = "https://legacy-api.arpa.li/urls"
DOWNLOADER = "testhandle"
VERSION = "20261027.01"

ITEM = {
    "item_name": "a89f2c1b\0b7d3e4f5",
    "item_dir": "/data/urls/a89f2c1b",
    "data_dir": "/data/urls/a89f2c1b/data",
    "warc_file_base": "urls-20261027-000000-abcd",
    "dict_project": "urls",
    "dict_id": "42",
}


def test_user_agent_identical_to_warrior():
    # Must match the ArchiveTeam Warrior exactly so tracker requests look the same.
    assert t.user_agent() == "ArchiveTeam Warrior/0.10.3 Warrior"
    assert "  " not in t.user_agent()  # no empty slots


def test_request_body_matches_contract():
    task = t.GetItemFromTracker(f"{TRACKER_URL}/multi=5/", DOWNLOADER, VERSION)
    assert task.body(ITEM) == {"downloader": DOWNLOADER, "version": VERSION}


def test_upload_body_includes_item_name_and_version():
    task = t.UploadWithTracker(TRACKER_URL, downloader=DOWNLOADER, version=VERSION, files=[])
    assert task.body(ITEM) == {
        "downloader": DOWNLOADER,
        "item_name": ITEM["item_name"],
        "version": VERSION,
    }


def test_upload_body_omits_version_when_absent():
    task = t.UploadWithTracker(TRACKER_URL, downloader=DOWNLOADER, files=[])
    assert "version" not in task.body(ITEM)


def test_parse_request_response_accepts_batch():
    body = (FIX / "request_response.json").read_text()
    data = t.parse_request_response(body)
    assert data is not None
    assert data["item_name"] == "a89f2c1b\0b7d3e4f5"
    assert data["items"] == ["a89f2c1b", "b7d3e4f5"]


def test_parse_request_response_empty_is_none():
    assert t.parse_request_response(json.dumps({})) is None


def test_parse_upload_target_rsync():
    body = (FIX / "upload_response.json").read_text()
    target = t.parse_upload_target(body)
    assert target == t.UploadTarget("rsync", "rsync://legacy-rsync.arpa.li/urls/")


def test_parse_upload_target_curl():
    target = t.parse_upload_target(json.dumps({"upload_target": "https://up.example.org/urls/"}))
    assert target == t.UploadTarget("curl", "https://up.example.org/urls/")


def test_parse_upload_target_invalid_scheme():
    assert t.parse_upload_target(json.dumps({"upload_target": "ftp://nope/"})) is None
    assert t.parse_upload_target(json.dumps({})) is None


def test_done_ok():
    assert t.done_ok((FIX / "done_response.txt").read_text()) is True
    assert t.done_ok("something else") is False


def test_status_classification():
    assert t.classify(200).status is t.Status.OK and not t.classify(200).retry
    assert t.classify(404).status is t.Status.NO_ITEM
    assert t.classify(429).status is t.Status.RATE_LIMITED
    assert t.classify(420).status is t.Status.RATE_LIMITED
    assert t.classify(455).status is t.Status.OUT_OF_DATE
    assert t.classify(599).status is t.Status.TRACKER_DOWN
    assert t.classify(500).status is t.Status.ERROR
    # every non-200 is retryable
    assert all(t.classify(c).retry for c in (404, 420, 429, 455, 599, 500, 503))


def test_prepare_stats_matches_urls_shape():
    # urls-grab: defaults {downloader, version}, one 'data' file group, an id_function.
    task = t.PrepareStatsForTracker(
        defaults={"downloader": DOWNLOADER, "version": VERSION},
        file_groups={"data": [ItemInterpolation("%(item_dir)s/%(warc_file_base)s.warc.zst")]},
        id_function=lambda item: item["warc_file_base"],
    )
    fake_sizes = {"/data/urls/a89f2c1b/urls-20261027-000000-abcd.warc.zst": 1234}
    stats = task.compute(ITEM, sizeof=lambda p: fake_sizes[p])
    assert stats == {
        "downloader": DOWNLOADER,
        "version": VERSION,
        "item": ITEM["item_name"],
        "bytes": {"data": 1234},
        "id": "urls-20261027-000000-abcd",
    }


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
