# Contributing

Contributions are welcome — bug reports, fixes, new project support, docs, all of it.

**AI-assisted contributions are explicitly welcome.** Use whatever tools help you; what
matters is that the change is correct, tested, and clearly explained. The bar is the code,
not how it was written. (This is, in part, why Carousel exists — see the README.)

## Project shape

Start with [`ARCHITECTURE.md`](ARCHITECTURE.md). In short:

- `controlplane/` — FastAPI app, supervisor, HQ client, config, models.
- `worker/` — `run_pipeline.py` (entrypoint), `runner.py` (concurrency), and `seesaw/`, our
  clean-room, import-compatible runtime that project `pipeline.py` files load.
- `web/` — the dashboard.
- `docker/` — the single image (worker runtime + control plane).

**Clean-room rule:** `worker/seesaw/` is a from-scratch reimplementation of the surface
ArchiveTeam projects import. Do **not** copy upstream `seesaw` source into it — match
behavior and wire format only. Compatibility is pinned by golden tests.

## Development

```bash
python3.11 -m venv .venv && . .venv/bin/activate   # 3.11: projects use distutils (gone in 3.12)
pip install -e '.[dev]'
pytest                                             # fully offline — no network, no tracker
```

Before opening a PR, make the four checks CI runs pass locally:

```bash
ruff format --check .      # formatting
ruff check .               # lint
mypy                       # types
pytest --cov               # tests
```

`ruff format .` fixes formatting; `ruff check --fix .` fixes the safe lint issues.

### Rebuilding the image

One image, one build — it carries both the worker runtime and the control plane:

```bash
docker build -f docker/Dockerfile -t carousel .
```

Layer caching keeps this fast: the heavy toolchain layers are reused, and editing
`controlplane/` or `web/` only re-runs the final few layers.

### Regenerating the golden files

Only when the pinned scenarios change (CI never needs this). Upstream seesaw needs Tornado 4,
so use a throwaway interpreter:

```bash
python3.9 -m venv /tmp/tv && /tmp/tv/bin/pip install 'tornado>=4,<5'
/tmp/tv/bin/python tests/golden/gen_realize.py
/tmp/tv/bin/python tests/golden/gen_differential.py
```

## Pull requests

- Branch off `master`; keep PRs focused.
- Write a clear description of *what* changed and *why*. Reference an issue if there is one.
- Add or update tests for behavior changes. New wire-format behavior should be covered by a
  golden or differential test.
- Keep commits readable; small, logical commits are easier to review than one large dump.

## Reporting bugs

Open an issue with: what you ran (including whether ArchiveTeam's Choice / which projects were
enabled), what you expected, what happened, and the relevant worker/control-plane log lines.
If it involves a specific project, note the project name and whether it's git- or
Docker-published.
