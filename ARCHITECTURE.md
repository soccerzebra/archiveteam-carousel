# Architecture

> **Carousel** — a seesaw seats two in opposition, one up one down; a carousel carries
> many riders at once. That's the whole idea: run many archiving projects simultaneously.
>
> A greenfield, Docker-native replacement for the ArchiveTeam Warrior that can run
> **multiple archiving projects simultaneously** under a single control plane and a
> single downloader identity — instead of one-project-per-container.

## Why a rebuild, not a fork

The upstream warrior (`seesaw-kit` + `warrior-dockerfile`) is structurally locked to
one project at a time. This is not a config limit — it is baked into the data model.
From the upstream map (`reference/`, gitignored):

| # | Single-project assumption | Location (upstream) |
|---|---------------------------|---------------------|
| 1 | `Runner.pipeline` is a scalar; `set_current_pipeline` evicts + cancels the prior pipeline | `seesaw/runner.py:40-51` |
| 2 | `Warrior.selected_project` / `current_project(_name)` are scalars | `seesaw/warrior.py:252-255` |
| 3 | HQ `update.json` reports a single `selected_project` string | `seesaw/warrior.py:365` |
| 4 | `concurrent_items` is one global, Runner-wide count | `seesaw/warrior.py:211-219`, `runner.py:97` |
| 5 | Per-project config values share one flat namespace, wiped on switch | `seesaw/warrior.py:1166-1173` |
| 6 | `cleanup_project_versions` deletes any version dir not owned by the single active pipeline | `seesaw/warrior.py:1012-1025` |
| 7 | `pipeline.py` is `exec`'d into a shared context expecting top-level `project`/`pipeline` globals | `seesaw/warrior.py:1069-1098` |
| 8 | Hard coupling to Tornado 4 (`stack_context`, `gen.Callback/Wait` — removed in modern Tornado) | `pipeline.py:5`, `task.py:6`, `warrior.py:600` |
| 9 | `BandwidthMonitor` reads `/proc/net/dev` on `eth0` only | `seesaw/warrior.py:101-158` |

Upstream's own answer to "run multiple projects" is *"run N containers on N ports"*
(`warrior-dockerfile/README.md:51`) — N web UIs, N downloader identities, no shared
resource budget, no unified dashboard. That is the thing we are replacing.

## Core decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Isolation model | **Process-per-project** in one control-plane container | No `docker.sock` needed; single container to run; workers share one base image's `wget-at` + Python runtime. Worker backend is abstracted so a container backend can be added later. |
| Control-plane language | **Python** (FastAPI + asyncio) | The worker sandbox must be Python regardless (see below), so one language = shared config/models/tests and no IPC type drift. |
| Compatibility | **Full** — run existing AT project pipelines unchanged, talk to the live tracker + Warrior HQ | Can contribute to real archiving on day one. We rebuild only the warrior/control layer, never the projects or the tracker. |
| Code reuse from seesaw | **Zero. Clean-room, every line rewritten.** | seesaw is outdated code we are replacing, not building on. Not a single line is vendored or copied. |
| Compatibility scope | **Wire protocol only** — Warrior HQ endpoints + Universal Tracker (request / upload / done) + the upload layout we deliver | We match how real ArchiveTeam infra is *called* and how finished work is *delivered back*, nothing else. |

### Hard rule: no seesaw code, anywhere

seesaw is studied in `reference/` and never shipped. Our worker is a greenfield async
runner we write from scratch. Compatibility is defined entirely by the **wire protocol**
(HQ + tracker, see below), not by any Python API.

### Open decision that defines the worker — see [Open questions](#open-questions)

What remains genuinely undecided is how a *project's crawl recipe* reaches our worker,
because that recipe (wget-at `.lua` scripts, wget flags, item naming, done-stats) is
authored in each **project's own repo** — not in seesaw. Two greenfield paths:

- **A — clean-room, run existing `pipeline.py`:** we write, from scratch, a modern runner
  that presents the same *API surface* existing project `pipeline.py` files call (our
  code, zero seesaw lines). Real AT projects run unchanged; we contribute day one.
- **B — new project format:** we define our own clean project descriptor and only reuse
  the HQ + tracker wire protocol. Maximum freedom, but every existing project must be
  ported to the new format before it can run.

This is the next thing to settle; the rest of the design holds either way.

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

- **One downloader identity, one dashboard, one resource budget**, N projects.
- Each worker is an isolated OS process running exactly one project's pipeline against
  the shared `wget-at` binary and a per-project data/version directory.
- Workers report status/logs to the supervisor over a line-delimited JSON channel
  (stdout or a unix socket); the web layer is a read-model + command surface over
  supervisor state.

## Repository layout

```
carousel/
├── ARCHITECTURE.md          ← this file
├── README.md
├── pyproject.toml
├── controlplane/            ← greenfield, zero seesaw
│   ├── app.py               ← FastAPI: web UI, REST, WebSocket state push
│   ├── supervisor.py        ← spawn / monitor / restart one worker per active project
│   ├── hq.py                ← Warrior HQ client: catalog, register, heartbeat
│   ├── projects.py          ← install project code (git clone / docker-image via crane)
│   ├── config.py            ← global + per-project config store (config.json)
│   └── models.py            ← shared dataclasses (ProjectInfo, WorkerStatus, …)
├── worker/                  ← per-project execution unit (Python; hosts the seesaw API)
│   ├── run_pipeline.py      ← load one project's pipeline.py, run it, emit status JSON
│   └── runtime/             ← our reimplemented seesaw-compatible API (grows over time)
├── docker/
│   ├── Dockerfile.base      ← wget-at + python + vendored-seesaw sandbox (worker runtime)
│   └── Dockerfile           ← control-plane image (FROM base)
├── web/                     ← dashboard assets (served by controlplane/app.py)
└── tests/
```

`reference/` (gitignored) holds the upstream repos we study: `warrior-dockerfile`,
`seesaw-kit`, `warrior-code2`.

## Protocols we must speak (inherited, unchanged)

**Warrior HQ** (`https://warriorhq.archiveteam.org`) — the project catalog:
- `POST /api/register.json` → `{warrior_id}`
- `POST /api/update.json` `{warrior_id, downloader, selected_project, lat_lng}` →
  `{projects:[{name, repository?, docker?}], seesaw_version, auto_project, broadcast_message}`

  **Change for multi-project:** we report a *set* of selected projects. If HQ only
  accepts one `selected_project`, we heartbeat per project or send the primary and
  track the rest locally. (Confirm against live HQ before relying on either.)

**Universal Tracker** (per project, URL comes from the pipeline) — the work-unit server:
- `POST /request` `{downloader, version}` → `{item_name}` (multi-item batches `\0`-joined)
- `POST /release` — return rejected items
- `POST /upload` `{downloader, item_name}` → `{upload_target}` (rsync:// or http(s)://)
- `POST /done` `{stats}` → `"OK"`
- `User-Agent: ArchiveTeam Warrior/<ver> <runner_type> <build>`
- The **downloader nickname** identifies us on every call (regex `^[-_a-zA-Z0-9]{3,30}$`).
  One nickname, shared across all our workers.

## What changes vs. upstream, concretely

- **Per-project concurrency budget** instead of one global `concurrent_items`. A global
  cap (total simultaneous items / total rsync threads) is split across active projects;
  each worker gets its own `concurrent_items`.
- **Per-project config namespace** — each project's pipeline-declared settings live under
  its own key, never wiped by another project starting (fixes #5).
- **Version dirs reference-counted across all active workers** (fixes #6).
- **asyncio throughout**, no Tornado (fixes #8).
- **Cross-platform bandwidth/stat collection** (not `eth0`/`/proc` only) (fixes #9).
- **Docker-image projects**: extract `/grab` via `crane` exactly as upstream does, then
  run as a process — not a nested container — keeping the process-per-project model.

## Worker API contract (decision A)

Our clean-room runner is exposed in the worker sandbox under the import name `seesaw`
— that name is the import contract existing `pipeline.py` files expect; **none of the
code behind it is upstream's.** The surface below was extracted from five current AT
projects (`reference/{reddit,tumblr,telegram,imgur,urls}-grab/pipeline.py`) and is the
exact set we must implement:

| Module | Public surface projects import | Notes |
|--------|--------------------------------|-------|
| `seesaw` | `__version__` | Projects gate on `StrictVersion(seesaw.__version__) >= '0.8.5'` — report ≥ that. |
| `seesaw.project` | `Project(title, project_html, utc_deadline=…)` | Data holder (title + HTML blurb). |
| `seesaw.pipeline` | `Pipeline(*tasks)` | Ordered task list; `.project`; drives one item through tasks. |
| `seesaw.item` | `ItemValue(key)`, `ItemInterpolation(fmt)` | Lazy refs resolved against an item; the templating used in task args. |
| `seesaw.task` | `SimpleTask` (override `process(item)`), `LimitConcurrent(n, inner)` | `LimitConcurrent` is the per-task parallelism bound (e.g. rsync threads). |
| `seesaw.externalprocess` | `ExternalProcess(name, args, env=…)`, `WgetDownload(args, max_tries=, accept_on_exit_code=, env=)` | Async subprocess; realizes lazy args. |
| **`seesaw.tracker`** | `GetItemFromTracker(url, downloader, version)`, `PrepareStatsForTracker(defaults, file_groups, id_function)`, `UploadWithTracker(url, downloader=, version=, files=, rsync_target_source_path=, rsync_extra_args=)`, `SendDoneToTracker(tracker_url=, stats=)` | **This is the compatibility that matters** — the wire protocol (`/request`, `/upload`, `/done`, rsync/curl to tracker-assigned target). |
| `seesaw.config` | `realize(value, item=None)`, `NumberConfigValue(min, max, default, name, title, description)`, `StringConfigValue(…)` | `realize` unwraps lazy/config values everywhere. |
| `seesaw.util` | `find_executable(name, version_matcher, paths)` | Projects use it to locate a suitable `wget-at`. |

Sandbox also needs these PyPI deps available because project code imports them directly
(not ours to implement): `tornado` (projects use `tornado.httpclient`), `requests`,
`zstandard`. **Pin the sandbox to Python 3.11** — projects still `from distutils.version
import StrictVersion`, and distutils was removed from the stdlib in 3.12.

Projects also ship assets our runner just invokes, never parses: `.lua` scripts for
`wget-at`, `ignore-list`, cookies, user-agent lists, etc.

## Roadmap (milestones)

1. **M1 — Single project, new stack, end to end. ✅ (worker half)** Clean-room worker:
   `seesaw`-compatible runtime, async Runner, tracker client, wget/rsync/curl, loads a real
   `pipeline.py` and runs it claim→download→upload→done. Tracker retry/rate-limit semantics
   (404/420/429/455/599) already handled. Live `urls` crawl is the Docker integration run.
2. **M2 — Multiple projects simultaneously. ✅** Supervisor runs one worker process per
   enabled project, divides the global concurrency budget across them, aggregates JSON
   status, and reconciles on config/catalog change. FastAPI control plane + WebSocket + one
   dashboard, one downloader identity, persisted config, git project installer with
   reference-counted version dirs, HQ heartbeat loop. *The headline feature.*
3. **M3 — Lifecycle polish.** Per-project settings in the UI, auto-project, project-update
   detection, per-item live progress, bandwidth stats (cross-platform, not `eth0`-only).
4. **M4 — Hardening.** Docker-image projects (crane export of `/grab`), install-hook
   privileges, build `wget-at` from source, resume after restart, auth on the web UI.
5. **M5 — Polish.** Container-backend option (docker.sock), k8s, metrics, more docs.

## Open questions

- **Worker shape (A vs B).** Run existing project `pipeline.py` via a clean-room runner
  that mirrors their API (A), or define a new greenfield project format and port projects
  to it (B)? Decides the whole worker layer. *(Next to settle.)*
- **Package/command** is `carousel` (lowercase); the display name is "Carousel".
- **HQ multi-select.** Does live HQ tolerate reporting multiple selected projects, or do
  we heartbeat per project? Needs a probe against the real endpoint.
- **Global budget policy.** How is the total item/rsync budget divided across projects —
  equal split, weighted, or user-set per project?
