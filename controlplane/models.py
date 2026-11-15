"""Shared data models for the control plane and the supervisor<->worker channel.

These are the vocabulary of the whole system. Keeping them in one place (and in Python)
is why a single-language stack pays off: the supervisor, the web API, and the worker
status channel all speak these exact types with no serialization drift.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel

# ArchiveTeam's warrior caps each project at 6 concurrent items; we match that ceiling.
PROJECT_THREADS_MAX = 6


class ProjectSource(str, enum.Enum):
    GIT = "git"
    DOCKER = "docker"  # extracted via `crane`, run as a process (not a nested container)


class ProjectInfo(BaseModel):
    """One project as advertised by Warrior HQ's catalog."""

    name: str
    title: str = ""
    source: ProjectSource
    repository: str | None = None  # git URL   (source == GIT)
    docker: str | None = None  # image ref (source == DOCKER)
    deadline: str | None = None
    description: str = ""  # one-line blurb from HQ
    logo: str | None = None  # project icon URL (HQ-hosted)
    leaderboard: str | None = None  # tracker leaderboard URL


class WorkerState(str, enum.Enum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class ItemStatus(BaseModel):
    """One in-flight item = one busy worker thread (slot)."""

    slot: int = 0  # which concurrency slot (0..concurrent_items-1)
    item_id: str = ""
    item_name: str = ""
    task: str = ""  # current pipeline step
    phase: str = ""  # derived rest state ("waiting"/"sleeping"/…); "" = actively working
    started: float = 0.0  # epoch seconds the item began (UI derives elapsed)
    bytes_up: int = 0  # archived bytes uploaded so far for this item
    bytes_down: int = 0  # bytes downloaded (when the pipeline reports it)
    log: list[str] = []  # last few log lines for this thread (live tail)


class WorkerStatus(BaseModel):
    """A snapshot the supervisor keeps for each worker, fed by the worker's JSON channel.

    One of these per active project (many run at once).
    """

    project: str
    title: str = ""
    state: WorkerState = WorkerState.STARTING
    pid: int | None = None
    concurrent_items: int = 0  # this project's slice of the global budget
    active_items: list[ItemStatus] = []
    items_done: int = 0
    items_failed: int = 0
    bytes_uploaded: int = 0
    last_error: str | None = None


class GlobalConfig(BaseModel):
    """Warrior-wide settings. Shared across every worker."""

    downloader: str = ""  # single nickname on the leaderboards
    warrior_id: str = ""  # assigned by HQ
    http_username: str = ""
    http_password: str = ""
    # Thread allocation. Per-project thread counts (ProjectConfig.concurrent_items) are
    # ABSOLUTE and authoritative — never auto-grown — so disabling a project can only reduce
    # total load, never push a rate-limited project higher. ``default_project_threads`` seeds a
    # project the first time it's enabled.
    default_project_threads: int = 2  # matches ArchiveTeam's warrior default
    max_rsync_threads: int = 20
    # "ArchiveTeam's Choice": when true, the projects HQ currently prioritizes are force-run
    # (at default_project_threads) ON TOP OF the user's manual selection, and refreshed on the
    # HQ heartbeat. See ControlPlane / Supervisor.forced.
    archiveteam_choice: bool = False
    # Global download rate cap in bytes/sec (0 = unlimited). The supervisor divides this
    # across all active download slots and passes each worker its per-download share.
    bandwidth_limit: int = 0


class ProjectConfig(BaseModel):
    """Per-project settings, including pipeline-declared config values.

    Namespaced per project, so starting project B never wipes project A's settings.
    """

    project: str
    enabled: bool = False
    # User opted this project OUT of ArchiveTeam's Choice: even when HQ picks it, don't run it.
    # Lets the user drop one AT pick (e.g. a rate-limited project) without leaving the mode.
    at_optout: bool = False
    # Absolute per-project thread count (concurrent items), 1..PROJECT_THREADS_MAX. None means
    # "not set yet" — the supervisor falls back to GlobalConfig.default_project_threads.
    concurrent_items: int | None = None
    values: dict[str, str] = {}  # pipeline-declared config values
