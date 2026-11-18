"""Remote hints client: parsing, ETag revalidation, interval, cache, and fail-safe fallback
(a bad fetch must never wipe the list). Mock transport only, no network."""

import json
import pathlib
import sys

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from controlplane.hints import BUNDLED_HINTS, HintsClient, parse_hints

DOC = {"version": 1, "inactive": {"twitch": {"note": "done", "since": "2026-01-01"}}}
DOC2 = {"version": 1, "inactive": {"pastebin": {}}}


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def mock_client(responses, seen):
    """Serve ``responses`` (a list of httpx.Response or exceptions) in order."""

    def handler(request):
        seen.append(request)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make(tmp_path, responses, seen, bundled=None, **kw):
    return HintsClient(
        url="https://example.test/hints.json",
        cache_path=tmp_path / "cache.json",
        bundled_path=bundled,
        client=mock_client(responses, seen),
        **kw,
    )


def test_parse_hints_normalizes_entries():
    out = parse_hints({"version": 1, "future_key": [1], "inactive": {"a": {"note": 5}, "b": None}})
    assert out == {"a": {"note": "", "since": ""}, "b": {"note": "", "since": ""}}


@pytest.mark.parametrize(
    "bad", [[], {"inactive": {}}, {"version": "1"}, {"version": 1, "inactive": ["x"]}]
)
def test_parse_hints_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_hints(bad)


def test_bundled_hints_file_is_valid():
    parse_hints(json.loads(BUNDLED_HINTS.read_text()))


async def test_fetch_caches_and_revalidates_with_etag(tmp_path):
    seen = []
    clock = FakeClock()
    responses = [
        httpx.Response(200, json=DOC, headers={"etag": '"v1"'}),
        httpx.Response(304),
    ]
    h = make(tmp_path, responses, seen, clock=clock, refresh_interval=60)
    assert await h.maybe_refresh() is True
    assert set(h.inactive) == {"twitch"}
    assert seen[0].headers["user-agent"] == "Carousel hints"

    # Not due yet: no request.
    assert await h.maybe_refresh() is False
    assert len(seen) == 1

    clock.t += 61
    assert await h.maybe_refresh() is False  # 304
    assert seen[1].headers["if-none-match"] == '"v1"'
    assert set(h.inactive) == {"twitch"}

    # A fresh client (e.g. after restart) loads the cache, ETag included.
    h2 = make(tmp_path, [httpx.Response(304)], seen)
    assert set(h2.inactive) == {"twitch"}
    await h2.maybe_refresh()
    assert seen[2].headers["if-none-match"] == '"v1"'


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500),
        httpx.Response(200, text="<html>not json"),
        httpx.Response(200, json={"version": 1, "inactive": "nope"}),
        httpx.ConnectError("offline"),
    ],
)
async def test_failed_fetch_keeps_previous_hints(tmp_path, failure):
    seen = []
    h = make(tmp_path, [httpx.Response(200, json=DOC), failure], seen)
    await h.maybe_refresh()
    assert await h.maybe_refresh(force=True) is False
    assert set(h.inactive) == {"twitch"}
    # cache still holds the last good copy
    assert json.loads((tmp_path / "cache.json").read_text())["hints"] == DOC


async def test_update_replaces_list(tmp_path):
    seen = []
    h = make(tmp_path, [httpx.Response(200, json=DOC), httpx.Response(200, json=DOC2)], seen)
    await h.maybe_refresh()
    assert await h.maybe_refresh(force=True) is True
    assert set(h.inactive) == {"pastebin"}  # projects can come back off the list


def test_falls_back_to_bundled_then_ignores_corrupt_cache(tmp_path):
    bundled = tmp_path / "bundled.json"
    bundled.write_text(json.dumps(DOC2))
    (tmp_path / "cache.json").write_text("{corrupt")
    h = make(tmp_path, [], [], bundled=bundled)
    assert set(h.inactive) == {"pastebin"}


async def test_empty_url_disables_fetching(tmp_path):
    seen = []
    h = HintsClient(
        url="", cache_path=tmp_path / "c.json", bundled_path=None, client=mock_client([], seen)
    )
    assert await h.maybe_refresh(force=True) is False
    assert seen == [] and h.inactive == {}
