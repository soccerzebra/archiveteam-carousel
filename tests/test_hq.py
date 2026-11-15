"""HQ client wire-format tests: the exact register/update bodies, headers, and User-Agent
HQ requires, plus catalog parsing — pinned with a mock transport (no network)."""

import json
import pathlib
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from controlplane.hq import UA_REGISTER, UA_UPDATE, HQClient
from controlplane.models import ProjectSource

CATALOG = {
    "projects": [
        {"name": "urls", "title": "URLs", "repository": "git://x/urls"},
        {"name": "instagram", "title": "Instagram", "docker": "atdr/ig", "repository": ""},
    ],
    "auto_project": "urls",
    "auto_projects_config": [
        {"project": "instagram", "weight": 6},
        {"project": "urls", "weight": 2},
    ],
}


def make_client(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/register.json":
            return httpx.Response(200, json={"warrior_id": "wid-123"})
        if request.url.path == "/api/update.json":
            return httpx.Response(200, json=CATALOG)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    return HQClient(client=httpx.AsyncClient(transport=transport))


async def test_register_sends_warrior_body_and_user_agent():
    seen = []
    hq = make_client(seen)
    wid = await hq.register()
    assert wid == "wid-123"
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/api/register.json"
    assert req.headers["user-agent"] == UA_REGISTER == "ArchiveTeam Warrior/0.10.3"
    assert json.loads(req.content) == {"warrior": {"version": "0.10.3"}}
    await hq.aclose()


async def test_heartbeat_wraps_payload_and_parses_catalog():
    seen = []
    hq = make_client(seen)
    update = await hq.heartbeat("wid-123", downloader="josh", selected=["urls"])
    req = seen[0]
    assert req.url.path == "/api/update.json"
    assert req.headers["user-agent"] == UA_UPDATE == "ArchiveTeam Warrior/0.10.3 Warrior"
    body = json.loads(req.content)
    assert body == {
        "warrior": {
            "warrior_id": "wid-123",
            "lat_lng": None,
            "downloader": "josh",
            "selected_project": "urls",
        }
    }
    # catalog parsing: git vs docker classification
    by_name = {p.name: p for p in update.projects}
    assert by_name["urls"].source is ProjectSource.GIT
    assert by_name["instagram"].source is ProjectSource.DOCKER
    await hq.aclose()


async def test_heartbeat_parses_auto_projects_in_hq_order():
    seen = []
    hq = make_client(seen)
    update = await hq.heartbeat("wid-123", downloader="josh", selected=[])
    # the weighted config wins over the single auto_project, kept in HQ's order
    assert update.auto_projects == ["instagram", "urls"]
    await hq.aclose()


async def test_heartbeat_selected_defaults_to_none():
    seen = []
    hq = make_client(seen)
    await hq.heartbeat("wid-123", downloader="", selected=[])
    body = json.loads(seen[0].content)
    assert body["warrior"]["selected_project"] == "none"
    await hq.aclose()
