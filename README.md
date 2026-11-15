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

## Running a real project end to end (integration)

The unit suite avoids external binaries; actually crawling needs the worker image, which
provides `wget-at` and project deps:

```bash
docker build -f docker/Dockerfile.base -t carousel-base:latest .

docker run --rm -it \
  -v "$PWD/worker:/opt/carousel/worker" \
  -v "$PWD/reference/urls-grab:/project" \
  carousel-base:latest \
  python -m worker.run_pipeline --project urls --pipeline /project/pipeline.py \
      --downloader YOUR_HANDLE --concurrent-items 1 --data-dir /home/warrior/data
```

This claims a real item from the live tracker and uploads results — use your own
downloader handle. Known rough edges to smooth next: the project install hook
(`warrior-install.sh`) needs root/sudo, and `wget-at` is currently copied from `grab-base`
rather than built from source.
