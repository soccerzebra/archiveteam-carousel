"""Warrior HQ client: project catalog, registration, heartbeat.

HQ (https://warriorhq.archiveteam.org) is the project *catalog* — distinct from the
per-project Universal Tracker that hands out work items (that lives in the worker).

HQ's update.json carries a single ``selected_project``; we run many at once, so this is
the one place the HQ protocol pinches. See ``heartbeat`` below — resolving it needs a probe
against the live endpoint.
"""

from __future__ import annotations

import httpx

from .models import ProjectInfo, ProjectSource

DEFAULT_HQ = "https://warriorhq.archiveteam.org"


class HQClient:
    def __init__(self, base_url: str = DEFAULT_HQ, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=30)

    async def register(self) -> str:
        """POST /api/register.json -> warrior_id (only when we don't have one yet)."""
        resp = await self._client.post(f"{self.base_url}/api/register.json")
        resp.raise_for_status()
        return resp.json()["warrior_id"]

    async def heartbeat(
        self, warrior_id: str, downloader: str, selected: list[str]
    ) -> list[ProjectInfo]:
        """POST /api/update.json -> catalog.

        TODO(M3): the endpoint takes a single ``selected_project``. Probe the live endpoint
        to decide between (a) sending a comma/newline-joined set, (b) sending a primary
        and tracking the rest locally, or (c) one heartbeat per selected project.
        """
        payload = {
            "warrior_id": warrior_id,
            "downloader": downloader,
            "selected_project": selected[0] if selected else "none",
        }
        resp = await self._client.post(f"{self.base_url}/api/update.json", json=payload)
        resp.raise_for_status()
        return [_parse_project(p) for p in resp.json().get("projects", [])]

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_project(p: dict) -> ProjectInfo:
    source = ProjectSource.DOCKER if p.get("docker") else ProjectSource.GIT
    return ProjectInfo(
        name=p["name"],
        title=p.get("title", p["name"]),
        source=source,
        repository=p.get("repository"),
        docker=p.get("docker"),
        deadline=p.get("deadline"),
    )
