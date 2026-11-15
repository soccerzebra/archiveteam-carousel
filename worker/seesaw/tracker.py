"""Talking to the Universal Tracker — the one compatibility that truly matters.

The wire format follows ArchiveTeam's Universal Tracker protocol. It is deliberately split:

  * a PURE protocol layer (request bodies, response parsing, status-code semantics) with
    no I/O and no third-party deps — fully contract-testable offline, and
  * a thin async ``TrackerClient`` (httpx) that POSTs + retries using that layer, and
  * the seesaw-surface task classes projects import (``GetItemFromTracker`` etc.), which
    hold their config and delegate to the client. Their execution plugs into the runner
    in M1 brick #2; their request-building is live and tested now.

Protocol, per the Universal Tracker:
  POST {tracker_url}/{command}   Content-Type: application/json
  User-Agent: "ArchiveTeam Warrior/{version} {runner_type} {build}"
    request -> {downloader, version}          -> 200 {item_name, items?, queues?, ...}
    upload  -> {downloader, item_name, ver?}  -> 200 {upload_target}  (rsync:// or http(s)://)
    done    -> <stats dict>                   -> 200 body "OK"
    release -> {downloader, version, items}   -> 200 {items: [...]}
  Non-200: 404 no item · 420/429 rate limited · 455 project out of date · 599 tracker
  down · else malfunction. All non-200 are retried with incremental backoff.
"""

from __future__ import annotations

import asyncio
import enum
import json
import os.path
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import seesaw
from seesaw.config import realize
from seesaw.task import Task, SimpleTask
from seesaw.externalprocess import RsyncUpload, CurlUpload

# --- pure protocol layer -----------------------------------------------------------

_RSYNC_TARGET = re.compile(r"^rsync://.+/$")
_HTTP_TARGET = re.compile(r"^https?://.+/$")


def user_agent() -> str:
    return (f"ArchiveTeam Warrior/{seesaw.__version__} "
            f"{seesaw.runner_type} {seesaw.warrior_build}").strip()


def request_headers() -> dict[str, str]:
    return {"Content-Type": "application/json", "User-Agent": user_agent()}


class Status(enum.Enum):
    OK = "ok"
    NO_ITEM = "no_item"           # 404
    RATE_LIMITED = "rate_limited"  # 420 / 429
    OUT_OF_DATE = "out_of_date"    # 455 — project code needs upgrading
    TRACKER_DOWN = "tracker_down"  # 599
    ERROR = "error"                # anything else


_STATUS_BY_CODE = {404: Status.NO_ITEM, 420: Status.RATE_LIMITED,
                   429: Status.RATE_LIMITED, 455: Status.OUT_OF_DATE,
                   599: Status.TRACKER_DOWN}

_STATUS_MESSAGE = {
    Status.NO_ITEM: "No item received. No items are available for this project right now.",
    Status.RATE_LIMITED: "Tracker rate limiting is active; slowing down.",
    Status.OUT_OF_DATE: "Project code is out of date and needs upgrading.",
    Status.TRACKER_DOWN: "No response from tracker; it is probably overloaded.",
    Status.ERROR: "Tracker returned an unexpected status; it may have malfunctioned.",
}


@dataclass(frozen=True)
class Classification:
    status: Status
    retry: bool
    message: str


def classify(code: int) -> Classification:
    """Map an HTTP status code to protocol meaning. Every non-200 is retryable."""
    if code == 200:
        return Classification(Status.OK, retry=False, message="")
    status = _STATUS_BY_CODE.get(code, Status.ERROR)
    msg = _STATUS_MESSAGE[status]
    if status is Status.ERROR:
        msg = f"Tracker returned status {code}. " + msg
    return Classification(status, retry=True, message=msg)


# Request body builders (mirror the tracker's `data()` contract). `item` lets lazy
# values (ItemValue/ItemInterpolation) resolve; plain strings pass through unchanged.

def request_body(downloader: Any, version: Any, item: Any = None) -> dict:
    return {"downloader": realize(downloader, item), "version": realize(version, item)}


def upload_body(downloader: Any, item_name: str, version: Any = None,
                item: Any = None) -> dict:
    body = {"downloader": realize(downloader, item), "item_name": item_name}
    if version:
        body["version"] = realize(version, item)
    return body


def release_body(downloader: Any, version: Any, items: Any, item: Any = None) -> dict:
    return {"downloader": realize(downloader, item),
            "version": realize(version, item),
            "items": realize(items, item)}


# Response parsers.

def parse_request_response(body: str) -> dict | None:
    """Return the item payload (all keys get copied onto the item), or None if empty."""
    data = json.loads(body)
    return data if "item_name" in data else None


@dataclass(frozen=True)
class UploadTarget:
    uploader: str   # "rsync" | "curl"
    target: str


def parse_upload_target(body: str) -> UploadTarget | None:
    """Decide the uploader from the tracker-assigned target URI, or None if absent/invalid."""
    data = json.loads(body)
    target = data.get("upload_target")
    if not target:
        return None
    if _RSYNC_TARGET.match(target):
        return UploadTarget("rsync", target)
    if _HTTP_TARGET.match(target):
        return UploadTarget("curl", target)
    return None  # unrecognized scheme -> caller retries / alerts


def done_ok(body: str) -> bool:
    return body.strip() == "OK"


# --- thin async client -------------------------------------------------------------

class TrackerError(Exception):
    def __init__(self, classification: Classification):
        super().__init__(classification.message)
        self.classification = classification


class TrackerClient:
    """POSTs protocol requests with incremental backoff. One per worker is plenty.

    ``http`` is any object with ``async post(url, headers=, content=) -> resp`` exposing
    ``.status_code`` and ``.text`` (httpx.AsyncClient fits; so does a test fake). ``sleep``
    is injectable so tests don't wait real seconds.
    """

    RETRY_START = 60
    RETRY_STEP = 10
    RETRY_MAX = 300

    def __init__(self, tracker_url: str, http,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.tracker_url = tracker_url.rstrip("/")
        self._http = http
        self._sleep = sleep

    async def _post(self, command: str, body: Any) -> tuple[int, str]:
        resp = await self._http.post(
            f"{self.tracker_url}/{command}",
            headers=request_headers(),
            content=json.dumps(body),
        )
        return resp.status_code, resp.text

    async def _request(self, command: str, body: Any, retry_on: set[Status]) -> str:
        """POST, returning the 200 body; retry (backoff) on ``retry_on``, else raise."""
        delay = self.RETRY_START
        while True:
            code, text = await self._post(command, body)
            c = classify(code)
            if c.status is Status.OK:
                return text
            if c.status not in retry_on:
                raise TrackerError(c)
            await self._sleep(delay)
            delay = min(self.RETRY_MAX, delay + self.RETRY_STEP)

    # High-level operations ---------------------------------------------------------

    _POLL = {Status.NO_ITEM, Status.RATE_LIMITED, Status.TRACKER_DOWN, Status.ERROR}
    _TRANSIENT = {Status.RATE_LIMITED, Status.TRACKER_DOWN, Status.ERROR}

    async def get_item(self, downloader, version, item=None) -> dict:
        """Poll /request until a work item is assigned. OUT_OF_DATE surfaces to the caller."""
        body = request_body(downloader, version, item)
        delay = self.RETRY_START
        while True:
            code, text = await self._post("request", body)
            c = classify(code)
            if c.status is Status.OK:
                data = parse_request_response(text)
                if data is not None:
                    return data
                # 200 but empty -> no work right now; wait and poll again.
            elif c.status is Status.OUT_OF_DATE:
                raise TrackerError(c)
            await self._sleep(delay)
            delay = min(self.RETRY_MAX, delay + self.RETRY_STEP)

    async def upload(self, downloader, item_name, version=None, item=None) -> UploadTarget:
        text = await self._request("upload", upload_body(downloader, item_name, version, item),
                                   self._TRANSIENT)
        target = parse_upload_target(text)
        if target is None:
            raise TrackerError(Classification(
                Status.ERROR, True, "Tracker did not provide a valid upload target."))
        return target

    async def send_done(self, stats) -> bool:
        text = await self._request("done", stats, self._TRANSIENT)
        if not done_ok(text):
            raise TrackerError(Classification(Status.ERROR, True,
                                              f"Tracker responded unexpectedly: {text.strip()!r}"))
        return True

    async def release(self, downloader, version, items, item=None) -> bool:
        await self._request("release", release_body(downloader, version, items, item),
                            self._TRANSIENT)
        return True


# --- seesaw-surface task classes ----------------------------------------------------
# These are what projects import. They hold config, build the exact requests the contract
# test pins, and execute via a TrackerClient over ctx.http.

class GetItemFromTracker(Task):
    def __init__(self, tracker_url, downloader, version, item_filter=None):
        super().__init__("GetItemFromTracker")
        self.tracker_url = tracker_url
        self.downloader = downloader
        self.version = version
        self.item_filter = item_filter  # TODO(#later): client-side item filtering + release

    def body(self, item=None) -> dict:
        return request_body(self.downloader, self.version, item)

    data = body  # alias used by the differential parity check

    async def run(self, item, ctx) -> None:
        client = TrackerClient(self.tracker_url, ctx.http)
        data = await client.get_item(self.downloader, self.version, item)
        for key, value in data.items():
            item[key] = value
        item.log_output(f"Received item '{item['item_name']}' from tracker.")


class UploadWithTracker(Task):
    def __init__(self, tracker_url, downloader, files, version=None,
                 rsync_target_source_path="./", rsync_bwlimit="0",
                 rsync_extra_args=(), curl_connect_timeout="60",
                 curl_speed_limit="1", curl_speed_time="900"):
        super().__init__("UploadWithTracker")
        self.tracker_url = tracker_url
        self.downloader = downloader
        self.files = files
        self.version = version
        self.rsync_target_source_path = rsync_target_source_path
        self.rsync_bwlimit = rsync_bwlimit
        self.rsync_extra_args = list(rsync_extra_args)
        self.curl_connect_timeout = curl_connect_timeout
        self.curl_speed_limit = curl_speed_limit
        self.curl_speed_time = curl_speed_time

    def body(self, item) -> dict:
        return upload_body(self.downloader, item["item_name"], self.version, item)

    data = body  # alias used by the differential parity check

    async def run(self, item, ctx) -> None:
        client = TrackerClient(self.tracker_url, ctx.http)
        target = await client.upload(self.downloader, item["item_name"], self.version, item)
        item.log_output(f"Uploading via {target.uploader} to {target.target}")
        if target.uploader == "rsync":
            uploader = RsyncUpload(
                target.target, self.files,
                target_source_path=self.rsync_target_source_path,
                bwlimit=self.rsync_bwlimit, extra_args=self.rsync_extra_args, max_tries=1)
        else:  # curl — single file only
            if len(self.files) != 1:
                raise ValueError("Curl upload target expects exactly one file.")
            uploader = CurlUpload(
                target.target, self.files[0], self.curl_connect_timeout,
                self.curl_speed_limit, self.curl_speed_time, max_tries=1)
        await uploader.run(item, ctx)


class SendDoneToTracker(Task):
    def __init__(self, tracker_url, stats):
        super().__init__("SendDoneToTracker")
        self.tracker_url = tracker_url
        self.stats = stats

    def body(self, item) -> Any:
        return realize(self.stats, item)

    data = body  # alias used by the differential parity check

    async def run(self, item, ctx) -> None:
        client = TrackerClient(self.tracker_url, ctx.http)
        await client.send_done(realize(self.stats, item))
        item.log_output(f"Tracker confirmed item '{item['item_name']}'.")


class PrepareStatsForTracker(SimpleTask):
    """Compute the stats dict sent to /done and store it on the item."""

    def __init__(self, defaults=None, file_groups=None, id_function: Callable | None = None):
        super().__init__("PrepareStatsForTracker")
        self.defaults = defaults or {}
        self.file_groups = file_groups or {}
        self.id_function = id_function

    def compute(self, item, sizeof: Callable[[str], int]) -> dict:
        """Build stats for ``item``. ``sizeof`` is injected (os.path.getsize) for testability."""
        total_bytes = {
            group: sum(sizeof(realize(f, item)) for f in files)
            for group, files in self.file_groups.items()
        }
        stats = dict(self.defaults)
        stats["item"] = item["item_name"]
        stats["bytes"] = total_bytes
        if self.id_function:
            stats["id"] = self.id_function(item)
        return realize(stats, item)

    def process(self, item) -> None:
        item["stats"] = self.compute(item, os.path.getsize)
