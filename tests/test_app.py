"""HTTP API tests via FastAPI TestClient, driving a ControlPlane over a fake supervisor.

Background heartbeat loop is disabled (run_background=False) so tests are deterministic.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from controlplane.app import create_app
from controlplane.config import ConfigStore
from controlplane.core import ControlPlane
from controlplane.models import ProjectInfo, ProjectSource, WorkerState, WorkerStatus


class FakeSupervisor:
    """Minimal supervisor: records reconciles, reports catalog + a fixed worker snapshot."""

    def __init__(self, config):
        self.config = config
        self.catalog = {}
        self.forced = set()
        self.reconciles = 0

    def set_catalog(self, projects):
        self.catalog = {p.name: p for p in projects}

    def budget(self):
        return dict.fromkeys(self.config.enabled_projects(), 3)

    def snapshot(self):
        return [
            WorkerStatus(project=n, state=WorkerState.RUNNING, concurrent_items=3, items_done=7)
            for n in self.config.enabled_projects()
        ]

    async def reconcile(self):
        self.reconciles += 1

    async def check_updates(self):
        pass

    async def restart(self, name):
        self.restarted = getattr(self, "restarted", [])
        self.restarted.append(name)

    async def stop_all(self, graceful=True):
        pass


def make_client(tmp_path):
    config = ConfigStore(pathlib.Path(tmp_path) / "config.json")
    config.global_config.downloader = "archivist"
    sup = FakeSupervisor(config)
    sup.set_catalog(
        [
            ProjectInfo(name="urls", title="URLs", source=ProjectSource.GIT, repository="git://u"),
            ProjectInfo(
                name="reddit", title="Reddit", source=ProjectSource.GIT, repository="git://r"
            ),
        ]
    )
    cp = ControlPlane(config, sup, hq=None)
    app = create_app(cp, run_background=False)
    return TestClient(app), cp


def test_status_endpoint(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        r = client.get("/api/status")
    assert r.status_code == 200
    assert r.json()["downloader"] == "archivist"


def test_projects_endpoint_lists_catalog(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        data = client.get("/api/projects").json()
    assert {p["name"] for p in data["projects"]} == {"urls", "reddit"}
    assert data["enabled"] == []


def test_enable_then_status_shows_worker(tmp_path):
    client, cp = make_client(tmp_path)
    with client:
        client.post("/api/projects/urls/enable")
        assert "urls" in cp.config.enabled_projects()
        status = client.get("/api/status").json()
    assert [w["project"] for w in status["workers"]] == ["urls"]
    assert status["budget"] == {"urls": 3}
    assert cp.supervisor.reconciles >= 1  # enabling triggered a reconcile


def test_enable_disable_roundtrip(tmp_path):
    client, cp = make_client(tmp_path)
    with client:
        client.post("/api/projects/urls/enable")
        client.post("/api/projects/reddit/enable")
        assert set(cp.config.enabled_projects()) == {"urls", "reddit"}
        client.post("/api/projects/urls/disable")
        assert cp.config.enabled_projects() == ["reddit"]


def test_settings_update_persists_and_reconciles(tmp_path):
    client, cp = make_client(tmp_path)
    with client:
        r = client.post(
            "/api/settings", json={"downloader": "newname", "default_project_threads": 3}
        )
    assert r.status_code == 200
    assert cp.config.global_config.downloader == "newname"
    assert cp.config.global_config.default_project_threads == 3
    # persisted to disk
    reloaded = ConfigStore(cp.config.path)
    reloaded.load()
    assert reloaded.global_config.downloader == "newname"


def test_stop_pauses_until_start(tmp_path):
    client, cp = make_client(tmp_path)
    with client:
        r = client.post("/api/stop")
        assert r.status_code == 200 and r.json()["paused"] is True
        assert cp.config.global_config.paused is True
        # persisted, so the stop survives a restart
        reloaded = ConfigStore(cp.config.path)
        reloaded.load()
        assert reloaded.global_config.paused is True
        r = client.post("/api/start")
        assert r.status_code == 200 and r.json()["paused"] is False
    assert cp.supervisor.reconciles == 2


def test_restart_endpoint(tmp_path):
    client, cp = make_client(tmp_path)
    with client:
        r = client.post("/api/projects/urls/restart")
    assert r.status_code == 200
    assert cp.supervisor.restarted == ["urls"]


def test_per_project_concurrency_persists(tmp_path):
    client, cp = make_client(tmp_path)
    with client:
        client.post("/api/projects/urls/enable")
        r = client.post("/api/projects/urls/concurrency", json={"concurrent_items": 5})
    assert r.status_code == 200
    assert cp.config.project("urls").concurrent_items == 5
    # projects() surfaces the override
    with client:
        row = next(p for p in client.get("/api/projects").json()["projects"] if p["name"] == "urls")
    assert row["concurrent_items"] == 5 and row["enabled"] is True


def test_stats_endpoint_without_store(tmp_path):
    # ControlPlane is built with stats=None here; the endpoint still returns a valid shape.
    client, _ = make_client(tmp_path)
    with client:
        r = client.get("/api/stats?hours=24&buckets=12")
    assert r.status_code == 200
    body = r.json()
    assert body["totals"] == {"items_done": 0, "items_failed": 0, "bytes": 0, "bytes_down": 0}
    assert "buckets" in body


def _basic(user, pw):
    import base64

    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_http_auth_protects_ui_and_healthz_stays_open(tmp_path):
    client, _cp = make_client(tmp_path)
    with client:
        # enabling auth is allowed (not yet protected)
        r = client.post("/api/settings", json={"http_username": "bob", "http_password": "pw"})
        assert r.status_code == 200 and r.json()["http_auth"] is True
        # now unauthenticated requests are rejected...
        assert client.get("/api/status").status_code == 401
        assert client.get("/").status_code == 401
        # ...but the health check endpoint stays open...
        assert client.get("/healthz").status_code == 200
        # ...and correct credentials work.
        assert client.get("/api/status", headers=_basic("bob", "pw")).status_code == 200
        assert client.get("/api/status", headers=_basic("bob", "wrong")).status_code == 401


def test_blank_password_keeps_existing_and_clearing_user_disables(tmp_path):
    client, cp = make_client(tmp_path)
    with client:
        client.post("/api/settings", json={"http_username": "bob", "http_password": "pw"})
        auth = _basic("bob", "pw")
        # a save with a blank password keeps the stored one (UI never echoes it back)
        r = client.post(
            "/api/settings",
            json={"default_project_threads": 5, "http_username": "bob", "http_password": ""},
            headers=auth,
        )
        assert r.status_code == 200
        assert cp.config.global_config.http_password == "pw"
        # clearing the username turns protection off and drops the password
        client.post("/api/settings", json={"http_username": "", "http_password": ""}, headers=auth)
    assert cp.config.global_config.http_username == ""
    assert cp.config.global_config.http_password == ""


def test_index_served(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        r = client.get("/")
    assert r.status_code == 200 and "Carousel" in r.text


def test_at_choice_optout_disables_one_pick(tmp_path):
    client, cp = make_client(tmp_path)
    cp._auto_projects = ["urls", "reddit"]  # HQ's current picks
    cp.config.global_config.archiveteam_choice = True
    cp._apply_choice()
    assert cp.supervisor.forced == {"urls", "reddit"}
    with client:
        # opt one pick out — it stops, the other keeps following AT
        client.post("/api/projects/urls/disable")
        assert cp.config.project("urls").at_optout is True
        assert cp.supervisor.forced == {"reddit"}
        # re-enabling clears the opt-out and it rejoins the set
        client.post("/api/projects/urls/enable")
        assert cp.config.project("urls").at_optout is False
        assert "urls" in cp.supervisor.forced
