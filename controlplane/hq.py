"""Warrior HQ client: project catalog, registration, heartbeat.

HQ (https://warriorhq.archiveteam.org) is the project *catalog* — distinct from the
per-project Universal Tracker that hands out work items (that lives in the worker).

HQ's update.json carries a single ``selected_project``; we run many at once, so this is
the one place the HQ protocol pinches. See ``heartbeat`` below — resolving it needs a probe
against the live endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from .models import ProjectInfo, ProjectSource

DEFAULT_HQ = "https://warriorhq.archiveteam.org"


@dataclass
class CatalogUpdate:
    """One heartbeat's worth of HQ state: the full catalog plus ArchiveTeam's current picks.

    ``auto_projects`` is the ordered set of projects HQ is prioritizing right now — the
    ``auto_projects_config`` list (weighted), or ``[auto_project]`` if HQ sends only the legacy
    single field. It drives "ArchiveTeam's Choice". We keep HQ's order but ignore the weights
    for now: every picked project runs at the user's default thread count.
    """

    projects: list[ProjectInfo]
    auto_projects: list[str] = field(default_factory=list)

# Identity HQ sees. Must match the warrior exactly: the version gates project compatibility,
# and the User-Agent is what HQ expects (an empty/foreign one is rejected). Same strings the
# worker uses on the tracker, so HQ and the trackers see one consistent warrior.
WARRIOR_VERSION = "0.10.3"
RUNNER_TYPE = "Warrior"
UA_REGISTER = f"ArchiveTeam Warrior/{WARRIOR_VERSION}"
UA_UPDATE = f"ArchiveTeam Warrior/{WARRIOR_VERSION} {RUNNER_TYPE}"


class HQClient:
    def __init__(self, base_url: str = DEFAULT_HQ, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=30)

    async def register(self) -> str:
        """POST /api/register.json -> warrior_id (only when we don't have one yet).

        HQ requires the ``{"warrior": {"version": ...}}`` body and the warrior User-Agent;
        a bare POST is answered with 500.
        """
        resp = await self._client.post(
            f"{self.base_url}/api/register.json",
            headers={"Content-Type": "application/json", "User-Agent": UA_REGISTER},
            json={"warrior": {"version": WARRIOR_VERSION}},
        )
        resp.raise_for_status()
        return resp.json()["warrior_id"]

    async def heartbeat(
        self, warrior_id: str, downloader: str, selected: list[str]
    ) -> CatalogUpdate:
        """POST /api/update.json -> catalog + ArchiveTeam's current picks.

        HQ's protocol carries a single ``selected_project``; we run many at once, so the
        heartbeat reports a primary (the enabled set is driven locally by the supervisor,
        not by HQ's selection) and we keep the full catalog HQ returns, plus the auto-pick
        set for "ArchiveTeam's Choice".
        """
        payload = {
            "warrior": {
                "warrior_id": warrior_id,
                "lat_lng": None,
                "downloader": downloader,
                "selected_project": selected[0] if selected else "none",
            }
        }
        resp = await self._client.post(
            f"{self.base_url}/api/update.json",
            headers={"Content-Type": "application/json", "User-Agent": UA_UPDATE},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        projects = [_parse_project(p) for p in data.get("projects", [])]
        return CatalogUpdate(projects=projects, auto_projects=_auto_projects(data))

    async def aclose(self) -> None:
        await self._client.aclose()


def _auto_projects(data: dict) -> list[str]:
    """ArchiveTeam's current picks, in HQ's order: the weighted ``auto_projects_config`` list,
    else the legacy single ``auto_project``, else empty."""
    config = data.get("auto_projects_config")
    if isinstance(config, list) and config:
        names = [e.get("project") for e in config if isinstance(e, dict) and e.get("project")]
        if names:
            return names
    single = data.get("auto_project")
    return [single] if single else []


def _parse_project(p: dict) -> ProjectInfo:
    # HQ marks a project Docker-only when it has no git repository but does have an image.
    has_repo = bool(p.get("repository"))
    source = ProjectSource.DOCKER if (not has_repo and p.get("docker")) else ProjectSource.GIT
    return ProjectInfo(
        name=p["name"],
        title=p.get("title", p["name"]),
        source=source,
        repository=p.get("repository"),
        docker=p.get("docker"),
        deadline=p.get("deadline"),
        description=p.get("description", "") or "",
        logo=p.get("logo") or None,
        leaderboard=p.get("leaderboard") or None,
    )
