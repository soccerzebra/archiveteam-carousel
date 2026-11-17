# Carousel

A Docker-native replacement for the [ArchiveTeam Warrior](https://wiki.archiveteam.org/index.php?title=ArchiveTeam_Warrior)
that runs **multiple archiving projects at once** under a single control plane and a single
downloader identity — instead of one project per container. It's a clean-room reimplementation,
wire-compatible with ArchiveTeam's Warrior HQ catalog, the per-project trackers, and existing
project pipelines (git- and Docker-published).

## Getting started

Carousel runs as **one control-plane container** that supervises a worker process per active
project. The image is multi-arch — `linux/amd64` and `linux/arm64` — so it runs natively on
x86 servers, Apple Silicon and ARM boards.

**Quickest** (once a release is published — pulls the image, no local build):

```bash
docker compose up -d        # ghcr.io/soccerzebra/archiveteam-carousel
```

**Or build it yourself:**

```bash
# 1. Build the image
docker build -f docker/Dockerfile -t carousel .

# 2. Run it. The data/ volume holds config, project checkouts, WARCs, and stats.
docker run -d -p 8001:8001 \
  -v "$PWD/data:/home/warrior" --name carousel carousel
```

Then open **http://localhost:8001** and use the dashboard:

1. **Settings** — set your **downloader handle** (`^[-_a-zA-Z0-9]{3,30}$`); nothing runs
   until this is set. Optionally set a default per-project thread count and a bandwidth cap.
2. **Projects** — enable the projects you want, or turn on **ArchiveTeam's Choice** to follow
   whatever HQ is currently prioritizing. Each project runs at its own thread count (max 6).
3. **Dashboard** — watch live per-thread status, data moved, and stats over time.

> Enabling a project claims **real** work from ArchiveTeam's live trackers and uploads real
> data under your handle — it appears on the public leaderboards. To just run the test suite
> instead (fully offline), see [Development](#development).

## Why

The upstream Warrior is one project per container: to run *N* projects you run *N* containers
on *N* ports. Carousel runs them all at once under a single control plane and identity.

It's also a home for fixes that stall upstream: the maintainer gates a lot of bugfixes out of
the tree, and AI-assisted contributions in particular get rejected — so they ship here instead.
The fuller design and upstream analysis are in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Layout

- `controlplane/` — the new web UI, API, supervisor, HQ client, config (zero seesaw).
- `worker/` — the per-project execution unit; hosts the Python pipeline API.
- `docker/` — the single image (worker runtime + control plane).
- `reference/` — upstream repos we study (gitignored, not shipped).

## Web dashboard

The control plane serves a single live dashboard (FastAPI + WebSocket) on `:8001` for every
project at once:

- **Pick projects** — enable/disable each project from the HQ catalog, with an optional
  per-project concurrency override.
- **Start/stop** — enabling a project starts a worker; disabling stops it; "Stop all" halts
  everything. Nothing runs until a downloader handle is set.
- **Downloader identity** — one handle for all projects (shown on the leaderboards).
- **Bandwidth limit** — a global download cap in MB/s, with a live "≈ X GB/day" readout. The
  supervisor divides it across all active download slots and passes each worker its share via
  a generated `wgetrc` (`limit_rate`); `0` means unlimited.
- **Stats over time** — a bucketed chart of data archived and items done/failed over the last
  24h, plus totals, backed by an on-disk SQLite history (`STATS_DB`, default
  `$WARRIOR_HOME/projects/stats.db`) that survives restarts.

## Development

```bash
python3.11 -m venv .venv && . .venv/bin/activate   # 3.11 — projects use distutils (gone in 3.12)
pip install -e '.[dev]'
pytest                 # 105 tests, all offline (no network, no production tracker)
```

## Tests & validation

The suite is fully offline — no network, no production tracker, no real work items. Where
behavior must match the existing ArchiveTeam warrior, it is pinned **byte-for-byte against
upstream** via committed golden files; where it must actually work, it runs **real
binaries** (`curl`, `rsync`) against a local server.

| File | What it validates |
|------|-------------------|
| `test_realize_parity.py` | The `realize`/templating layer, **differential vs. upstream seesaw** (golden). |
| `test_differential.py` | **Byte-for-byte vs. upstream**: tracker `/request`,`/upload`,`/done` bodies, realized `wget`/`rsync` argv, rsync file-list, stats dict (golden). |
| `test_tracker_protocol.py` | Tracker wire format: request bodies, response parsing, 404/420/429/455/599 semantics. |
| `test_tracker_client.py` | Client poll/retry/backoff/upload/done via a fake transport. |
| `test_engine.py` | Task ordering, cancel, `LimitConcurrent` cap, Runner concurrency + failure survival. |
| `test_externalprocess.py` | `wget`/`rsync`/`curl` argv compatibility + real subprocess run/retry/accept logic. |
| `test_load_and_run.py` | Loading a real-shaped `pipeline.py`, downloader injection, independent loads, claim→done vs. a fake tracker. |
| `test_full_chain.py` | **Hermetic full chain**: claim → `curl` download → stats → `curl`/`rsync` upload → done, asserting uploaded bytes + `/done` stats exactly. |
| `test_supervisor.py` | Budget split, bandwidth split, status parsing, reconcile (start/stop/restart), crash recovery, update detection. |
| `test_stats.py` | SQLite stats history: record, bucketed time-series read, window filtering, persistence across reopen. |
| `test_hq.py` | Warrior HQ wire format: register/update bodies, User-Agent, catalog parsing (git vs docker). |
| `test_app.py` | HTTP API (status, projects, enable/disable, settings, restart, concurrency) via `TestClient`. |
| `test_surface.py` | Our `seesaw` exports every symbol real projects import. |

**What "byte-for-byte vs. upstream" covers and doesn't:** it pins the bytes our warrior
*sends* and the commands it *execs* (tracker bodies, argv, file-lists, stats). It does **not**
compare the WARC payload — `wget-at` embeds timestamps and record UUIDs, so a WARC is not
reproducible even across two upstream runs. WARC correctness is `wget-at`'s own concern,
exercised only by the live crawl.

**Regenerating the goldens** (only when the scenarios change — CI never needs upstream):
upstream seesaw requires Tornado 4, so use a separate interpreter:

```bash
python3.9 -m venv /tmp/tv && /tmp/tv/bin/pip install 'tornado>=4,<5'
/tmp/tv/bin/python tests/golden/gen_realize.py        # realize layer golden
/tmp/tv/bin/python tests/golden/gen_differential.py   # orchestration-output golden
```

**Not yet covered by tests** (→ the live crawl): `wget-at` actually producing a valid WARC,
and running the real `urls`/`reddit`/… `pipeline.py` with their `.lua` scripts and deps.

## Docker image

A single image (`docker/Dockerfile`, built in [Getting started](#getting-started)) carries
the worker runtime *and* the control plane — one container runs the control plane and spawns
a worker subprocess per active project. Everything runs on **one Python, 3.11** — the newest
that still ships stdlib `distutils`, which the project pipelines we run unchanged import.
`wget-at` is compiled from ArchiveTeam's [wget-lua](https://github.com/ArchiveTeam/wget-lua)
source at a pinned commit, following that repo's own build recipe (the NSS TLS build behind
`--impersonate`, the same library versions and configure flags), so it's the same binary
ArchiveTeam ships, built for whichever architecture the image targets. The Lua rocks and system
packages project scripts expect are installed alongside it at the versions ArchiveTeam's
`grab-base` uses. To take a new `wget-at` release, bump `WGET_AT_COMMIT` and `WGET_AT_VERSION`
together in `docker/Dockerfile` (the build fails if they disagree).
`.github/workflows/catalog-compat.yml` checks weekly that this one shared `wget-at` still
satisfies every project in the live HQ catalog.

## Running a single project directly (integration)

The unit suite avoids external binaries; actually crawling needs the image. This claims a
**real** item from the live tracker and uploads results — use your own downloader handle
(`^[-_a-zA-Z0-9]{3,30}$`). The project's `warrior-install.sh` runs at startup to install its
extra deps (e.g. `lupa`, ghostscript).

```bash
docker run --rm \
  -v "$PWD/reference/urls-grab:/project:ro" \
  carousel \
  python3 -m worker.run_pipeline --project urls --pipeline /project/pipeline.py \
      --downloader YOUR_HANDLE --concurrent-items 1 --data-dir /home/warrior/data
```

Outbound rsync (tcp/873) must be allowed for uploads to reach the tracker-assigned target.
Later refinement: the distinct per-TLS-variant `wget-at` binaries (we symlink one).
