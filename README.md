# Carousel

A Docker-native replacement for the [ArchiveTeam Warrior](https://wiki.archiveteam.org/index.php?title=ArchiveTeam_Warrior)
that runs **multiple archiving projects at once** under a single control plane and a
single downloader identity — instead of one project per container.

> Status: **early scaffold.** See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the design,
> the decisions behind it, and the milestone roadmap.

## Why

The upstream warrior is structurally one-project-at-a-time; its own docs tell you to run
N containers on N ports to run N projects. This project lifts that by supervising one
isolated worker process per active project, while staying fully compatible with existing
ArchiveTeam projects and the live tracker. Details and the upstream analysis are in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## Layout

- `controlplane/` — the new web UI, API, supervisor, HQ client, config (zero seesaw).
- `worker/` — the per-project execution unit; hosts the Python pipeline API.
- `docker/` — base (worker runtime) and control-plane images.
- `reference/` — upstream repos we study (gitignored, not shipped).

## Development

```bash
python3.11 -m venv .venv && . .venv/bin/activate   # 3.11 — projects use distutils (gone in 3.12)
pip install -e '.[dev]'
pytest                 # 63 tests, all offline (no network, no production tracker)
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
| `test_supervisor.py` | Budget split, status parsing, reconcile (start/stop/restart), crash recovery, update detection. |
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

## Docker images

Everything runs on **one Python, 3.11** — the newest that still ships stdlib `distutils`,
which the existing project pipelines we run unchanged import. The images are based on
ArchiveTeam's `grab-base` (for `wget-at` and its shared libs) with 3.11 installed as the
default interpreter. `grab-base` is `linux/amd64`, so build for amd64 (it runs under
emulation on Apple Silicon):

```bash
docker build --platform linux/amd64 -f docker/Dockerfile.base -t carousel-base .  # worker runtime
docker build --platform linux/amd64 -f docker/Dockerfile      -t carousel      .  # control plane
```

## Running a real project end to end (integration)

The unit suite avoids external binaries; actually crawling needs the worker image. This
claims a **real** item from the live tracker and uploads results — use your own downloader
handle (`^[-_a-zA-Z0-9]{3,30}$`). The project's `warrior-install.sh` runs at startup to
install its extra deps (e.g. `lupa`, ghostscript).

```bash
docker run --rm --platform linux/amd64 \
  -v "$PWD/reference/urls-grab:/project:ro" \
  carousel-base \
  python3 -m worker.run_pipeline --project urls --pipeline /project/pipeline.py \
      --downloader YOUR_HANDLE --concurrent-items 1 --data-dir /home/warrior/data
```

Outbound rsync (tcp/873) must be allowed for uploads to reach the tracker-assigned target.
Later refinements: the distinct per-TLS-variant `wget-at` binaries (we symlink one), and
building `wget-at` from source to drop the `grab-base` dependency.
