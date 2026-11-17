# Architecture

**Carousel** is a Docker-native control plane for ArchiveTeam archiving work. It runs
**multiple archiving projects simultaneously** under a single control plane, a single
downloader identity, one dashboard, and one shared resource budget.

Each project runs in its own OS process. The control plane spawns and supervises these
workers, talks to Warrior HQ and the per-project trackers, and serves the dashboard.

## System shape

```
                         ┌───────────────────────────────────────────┐
                         │        control-plane container             │
                         │                                            │
   browser ──8001──▶  web UI + REST/WebSocket (FastAPI)               │
                         │        │                                   │
   Warrior HQ  ◀────────┼─ hq client (catalog, register, heartbeat)   │
   (catalog)            │        │                                   │
                         │   supervisor ── spawns 1 worker / project   │
                         │        │          │          │             │
                         │   ┌────▼───┐  ┌───▼────┐  ┌──▼─────┐        │
                         │   │worker A│  │worker B│  │worker C│  …     │
                         │   │pipeline│  │pipeline│  │pipeline│        │
                         │   └───┬────┘  └───┬────┘  └───┬────┘        │
                         └───────┼───────────┼──────────┼─────────────┘
                                 ▼           ▼          ▼
                            tracker A    tracker B   tracker C   (per-project
                            + rsync/curl upload targets assigned by tracker)
```

- **Control plane** — one container. FastAPI serves the web UI, REST, and WebSocket state
  push; the HQ client handles catalog/register/heartbeat; the supervisor spawns, monitors,
  live-reconfigures, and restarts one worker per active project. Each worker runs at its own
  thread count (bandwidth is the one globally shared budget).
- **Workers** — one OS process per project, each running that project's pipeline against a
  shared `wget-at` binary and a per-project data/version directory. Workers report
  status and logs to the supervisor over a line-delimited JSON channel (stdout or a unix
  socket).
- **Web layer** — a read-model and command surface over supervisor state.

Each project drives its own item lifecycle: claim an item from its tracker, download with
`wget-at`, upload to the tracker-assigned rsync/curl target, and report done-stats.

## Repository layout

```
carousel/
├── ARCHITECTURE.md          ← this file
├── README.md
├── pyproject.toml
├── controlplane/
│   ├── app.py               ← FastAPI: web UI, REST, WebSocket state push
│   ├── supervisor.py        ← spawn / monitor / restart one worker per active project
│   ├── hq.py                ← Warrior HQ client: catalog, register, heartbeat
│   ├── projects.py          ← install project code (git clone / docker-image via crane)
│   ├── config.py            ← global + per-project config store (config.json)
│   └── models.py            ← shared dataclasses (ProjectInfo, WorkerStatus, …)
├── worker/                  ← per-project execution unit
│   ├── run_pipeline.py      ← load one project's pipeline.py, run it, emit status JSON
│   ├── runner.py            ← drive N concurrent items through one pipeline
│   └── seesaw/              ← the clean-room, import-compatible `seesaw` runtime API
├── docker/
│   └── Dockerfile           ← single image: toolchain + worker runtime + control plane
├── web/                     ← dashboard assets (served by controlplane/app.py)
└── tests/
```

## Protocols

**Warrior HQ** (`https://warriorhq.archiveteam.org`) — the project catalog:
- `POST /api/register.json` → `{warrior_id}`
- `POST /api/update.json` `{warrior_id, downloader, selected_project, lat_lng}` →
  `{projects:[{name, repository?, docker?, …}], auto_project, auto_projects_config, …}`.
  `auto_projects_config` (a weighted list) is what ArchiveTeam currently prioritizes; it
  drives "ArchiveTeam's Choice".

**Universal Tracker** (per project, URL comes from the pipeline) — the work-unit server:
- `POST /request` `{downloader, version}` → `{item_name}` (multi-item batches `\0`-joined)
- `POST /release` — return rejected items
- `POST /upload` `{downloader, item_name}` → `{upload_target}` (rsync:// or http(s)://)
- `POST /done` `{stats}` → `"OK"`
- `User-Agent: ArchiveTeam Warrior/<ver> <runner_type> <build>`
- The downloader nickname identifies the warrior on every call
  (regex `^[-_a-zA-Z0-9]{3,30}$`); one nickname is shared across all workers.

## Worker runtime API

The worker exposes a runtime under the import name `seesaw`, which existing project
`pipeline.py` files import. The surface:

| Module | Public surface | Notes |
|--------|----------------|-------|
| `seesaw` | `__version__` | Projects gate on `StrictVersion(seesaw.__version__) >= '0.8.5'`. |
| `seesaw.project` | `Project(title, project_html, utc_deadline=…)` | Data holder (title + HTML blurb). |
| `seesaw.pipeline` | `Pipeline(*tasks)` | Ordered task list; `.project`; drives one item through tasks. |
| `seesaw.item` | `ItemValue(key)`, `ItemInterpolation(fmt)`, `item.on_finish` | Lazy refs resolved against an item; `on_finish` runs per-item cleanup (fires on success or failure). |
| `seesaw.task` | `SimpleTask` (override `process(item)`), `LimitConcurrent(n, inner)` | `LimitConcurrent` bounds per-task parallelism (e.g. rsync threads). |
| `seesaw.externalprocess` | `ExternalProcess(name, args, env=…)`, `WgetDownload(args, max_tries=, accept_on_exit_code=, env=)` | Async subprocess; realizes lazy args. |
| `seesaw.tracker` | `GetItemFromTracker(url, downloader, version)`, `PrepareStatsForTracker(defaults, file_groups, id_function)`, `UploadWithTracker(url, downloader=, version=, files=, rsync_target_source_path=, rsync_extra_args=)`, `SendDoneToTracker(tracker_url=, stats=)` | The tracker wire protocol (`/request`, `/upload`, `/done`, rsync/curl to the assigned target). |
| `seesaw.config` | `realize(value, item=None)`, `NumberConfigValue(min, max, default, name, title, description)`, `StringConfigValue(…)` | `realize` unwraps lazy/config values. |
| `seesaw.util` | `find_executable(name, version_matcher, paths)` | Locates a suitable `wget-at`. |

The worker sandbox runs Python 3.11 and provides the PyPI packages project code imports
directly: `tornado`, `requests`, `zstandard`.

Projects also ship assets the runner invokes but never parses: `.lua` scripts for
`wget-at`, `ignore-list`, cookies, user-agent lists, and similar.

## Concurrency and resources

- **Threads are per-project and absolute.** Each enabled project runs at its own
  `concurrent_items` (1..6, matching the upstream per-project ceiling), seeded from a global
  `default_project_threads`. They are never auto-grown, so disabling one project only reduces
  load — it never pushes a rate-limited project higher.
- **Bandwidth is the one globally shared budget:** a global cap is split across all active
  download slots and handed to each worker as a generated `wgetrc` `limit_rate` (0 =
  unlimited).
- **ArchiveTeam's Choice** (optional): force-run whatever HQ currently prioritizes
  (`auto_projects_config`) on top of the user's own selection, refreshed each heartbeat; a
  single pick can be opted back out.
- Each project's pipeline-declared settings live under its own config namespace.
- Project version directories are reference-counted across all active workers; a
  disabled/updated worker drains in the background (finishing its in-flight item) before its
  version dir is released.
- Docker-image projects have their `/grab` extracted via `crane` and run as a process,
  keeping the one-process-per-project model.

## Item files

Every item gets its own `item["data_dir"]`, the directory project pipelines build their
working dir under and move the finished WARC into before uploading. The runner deletes it
when the item ends (completed, failed, or cancelled), so nothing an item wrote outlives it; a
failed item's partial data is discarded and the tracker re-issues the item. Layout:
`<data_dir>/<project>/run-<pid>-<rand>/<item_id>/`. Each worker holds an `flock` on its
`run-*.lock` for its lifetime; on startup a worker removes every run directory whose lock is
free, which covers workers that were killed outright.
