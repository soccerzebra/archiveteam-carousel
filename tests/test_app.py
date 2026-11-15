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
        self.reconciles = 0

    def set_catalog(self, projects):
        self.catalog = {p.name: p for p in projects}

    def budget(self):
        return {n: 3 for n in self.config.enabled_projects()}

    def snapshot(self):
        return [WorkerStatus(project=n, state=WorkerState.RUNNING, concurrent_items=3,
                             items_done=7)
                for n in self.config.enabled_projects()]

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
    config.global_config.downloader = "josh"
    sup = FakeSupervisor(config)
    sup.set_catalog([
        ProjectInfo(name="urls", title="URLs", source=ProjectSource.GIT, repository="git://u"),
        ProjectInfo(name="reddit", title="Reddit", source=ProjectSource.GIT, repository="git://r"),
    ])
    cp = ControlPlane(config, sup, hq=None)
    app = create_app(cp, run_background=False)
    return TestClient(app), cp


def test_status_endpoint(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        r = client.get("/api/status")
    assert r.status_code == 200
    assert r.json()["downloader"] == "josh"


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
        r = client.post("/api/settings", json={"downloader": "newname", "max_concurrent_items": 10})
    assert r.status_code == 200
    assert cp.config.global_config.downloader == "newname"
    assert cp.config.global_config.max_concurrent_items == 10
    # persisted to disk
    reloaded = ConfigStore(cp.config.path)
    reloaded.load()
    assert reloaded.global_config.downloader == "newname"


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


def test_index_served(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        r = client.get("/")
    assert r.status_code == 200 and "Carousel" in r.text
