"""ControlPlane: the orchestration logic behind the HTTP layer.

Bundles config + supervisor + HQ client and exposes the operations the web app and the
background loop call. Kept separate from app.py so it can be driven directly in tests with
a fake supervisor and no network.
"""

from __future__ import annotations

from typing import Any

from .config import ConfigStore
from .models import PROJECT_THREADS_MAX
from .supervisor import Supervisor, clamp_threads


class ControlPlane:
    def __init__(
        self,
        config: ConfigStore,
        supervisor: Supervisor,
        hq: Any | None = None,
        stats: Any | None = None,
    ):
        self.config = config
        self.supervisor = supervisor
        self.hq = hq
        self.stats = stats
        self._auto_projects: list[str] = []  # ArchiveTeam's current picks (last heartbeat)

    async def heartbeat_once(self) -> None:
        """Register (first time), fetch the HQ catalog, then reconcile workers."""
        if self.hq is None:
            await self.supervisor.reconcile()
            return
        gc = self.config.global_config
        if not gc.warrior_id:
            gc.warrior_id = await self.hq.register()
            self.config.save()
        update = await self.hq.heartbeat(
            gc.warrior_id, gc.downloader, self.config.enabled_projects()
        )
        self.supervisor.set_catalog(update.projects)
        self._auto_projects = update.auto_projects
        self._apply_choice()
        await self.supervisor.reconcile()
        await self.supervisor.check_updates()

    def _apply_choice(self) -> None:
        """Set the supervisor's forced set from ArchiveTeam's Choice (empty when off)."""
        gc = self.config.global_config
        self.supervisor.forced = set(self._auto_projects) if gc.archiveteam_choice else set()

    async def monitor_once(self) -> None:
        """Fast loop: restart crashed workers, re-apply budget. No network."""
        await self.supervisor.reconcile()

    def status(self) -> dict:
        gc = self.config.global_config
        budget = self.supervisor.budget()
        total_threads = sum(budget.values())
        return {
            "downloader": gc.downloader,
            "warrior_id": gc.warrior_id,
            "archiveteam_choice": gc.archiveteam_choice,
            "auto_projects": list(self._auto_projects),
            "default_project_threads": gc.default_project_threads,
            "max_rsync_threads": gc.max_rsync_threads,
            "bandwidth_limit": gc.bandwidth_limit,
            "http_username": gc.http_username,
            "http_password_set": bool(gc.http_password),
            "http_auth": bool(gc.http_username and gc.http_password),
            "budget": budget,
            "total_threads": total_threads,
            "thread_max": PROJECT_THREADS_MAX,
            "workers": [w.model_dump() for w in self.supervisor.snapshot()],
        }

    def stats_series(self, hours: float = 24.0, buckets: int = 48) -> dict:
        if self.stats is None:
            return {
                "window_hours": hours,
                "bucket_seconds": 0,
                "buckets": [],
                "totals": {"items_done": 0, "items_failed": 0, "bytes": 0},
            }
        return self.stats.series(hours=hours, buckets=buckets)

    def projects(self) -> dict:
        enabled = set(self.config.enabled_projects())
        auto = set(self._auto_projects) if self.config.global_config.archiveteam_choice else set()
        rows = []
        for p in self.supervisor.catalog.values():
            row = p.model_dump()
            # a project is on when manually enabled OR force-run by ArchiveTeam's Choice
            row["at_choice"] = p.name in auto  # tagged + locked in the UI while choice is on
            row["enabled"] = p.name in enabled or p.name in auto
            pc = self.config.projects.get(p.name)
            row["concurrent_items"] = pc.concurrent_items if pc else None
            rows.append(row)
        return {"projects": rows, "enabled": [r["name"] for r in rows if r["enabled"]]}

    async def set_enabled(self, name: str, enabled: bool) -> None:
        pc = self.config.project(name)
        pc.enabled = enabled
        # Seed a concrete thread count the first time a project is enabled, so the per-project
        # value is explicit and editable rather than an implicit fallback.
        if enabled and not pc.concurrent_items:
            pc.concurrent_items = clamp_threads(self.config.global_config.default_project_threads)
        self.config.save()
        await self.supervisor.reconcile()

    async def set_project_concurrency(self, name: str, concurrent_items: int | None) -> None:
        self.config.project(name).concurrent_items = (
            clamp_threads(concurrent_items) if concurrent_items else None
        )
        self.config.save()
        await self.supervisor.reconcile()

    async def restart_project(self, name: str) -> None:
        await self.supervisor.restart(name)

    async def update_settings(self, data: dict) -> None:
        gc = self.config.global_config
        if "downloader" in data:
            gc.downloader = str(data["downloader"]).strip()
        if "archiveteam_choice" in data:
            gc.archiveteam_choice = bool(data["archiveteam_choice"])
            self._apply_choice()  # apply from the last heartbeat's picks; next beat refreshes
        if "default_project_threads" in data:
            gc.default_project_threads = clamp_threads(data["default_project_threads"])
        if "max_rsync_threads" in data:
            gc.max_rsync_threads = int(data["max_rsync_threads"])
        if "bandwidth_limit" in data:
            gc.bandwidth_limit = max(0, int(data["bandwidth_limit"] or 0))
        # Web-interface auth. Username empty => protection off. A blank password keeps the
        # existing one (so the UI never has to echo the stored password back).
        if "http_username" in data:
            gc.http_username = str(data["http_username"]).strip()
        if "http_password" in data and str(data["http_password"]):
            gc.http_password = str(data["http_password"])
        if not gc.http_username:
            gc.http_password = ""
        # per-project thread counts, e.g. {"projects": {"urls": 3}} (clamped 1..MAX)
        for name, ci in (data.get("projects") or {}).items():
            self.config.project(name).concurrent_items = clamp_threads(ci) if ci else None
        self.config.save()
        await self.supervisor.reconcile()

    async def stop_all(self) -> None:
        await self.supervisor.stop_all()
