"""Worker entrypoint: run ONE project's pipeline, report status as JSON lines.

Invoked by the supervisor as a subprocess:

    python -m worker.run_pipeline --project urls \
        --pipeline /versions/urls-<hash>/pipeline.py \
        --downloader josh --concurrent-items 2 --data-dir /home/warrior/data

Because each project gets its own process, the runner only ever hosts one pipeline.

Status channel: one JSON object per line on stdout (parsed by controlplane/supervisor.py).
Human/wget/rsync output goes to the item logs (and on to stderr in this brick).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# When run as a module (python -m worker.run_pipeline) the package root is importable; when
# run as a script, make sure `seesaw` (next to this file) is on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from seesaw.context import RunContext  # noqa: E402
from runner import Runner  # noqa: E402


def emit(obj: dict) -> None:
    """Write one status line for the supervisor."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def load_pipeline(pipeline_path: str, downloader: str, extra_context: dict | None = None):
    """Execute a project's pipeline.py and return (project, pipeline).

    The file is an executable script (not an importable module): it builds module-level
    ``project`` and ``pipeline`` using our ``seesaw`` package, reading an injected
    ``downloader``. We chdir into the project directory for the exec so its assets (.lua,
    ignore-list, static-*.txt) resolve — safe here because a worker loads exactly one
    project once, at startup, before any concurrency begins.
    """
    path = Path(pipeline_path).resolve()
    source = path.read_text()
    namespace: dict[str, Any] = {
        "__name__": "__pipeline__",
        "__file__": str(path),
        "downloader": downloader,
    }
    if extra_context:
        namespace.update(extra_context)

    prev_cwd = os.getcwd()
    os.chdir(path.parent)
    try:
        exec(compile(source, str(path), "exec"), namespace)
    finally:
        os.chdir(prev_cwd)

    if "project" not in namespace or "pipeline" not in namespace:
        raise ValueError(f"{path} did not define both `project` and `pipeline`.")
    return namespace["project"], namespace["pipeline"]


def run_install_hook(project_dir: str) -> None:
    """Run a project's warrior-install.sh if present (installs its extra dependencies)."""
    hook = Path(project_dir) / "warrior-install.sh"
    if not hook.exists():
        return
    emit({"event": "install_hook", "status": "running"})
    result = subprocess.run(["bash", str(hook)], cwd=project_dir)
    if result.returncode != 0:
        raise RuntimeError(f"warrior-install.sh failed with code {result.returncode}")
    emit({"event": "install_hook", "status": "done"})


async def run(args: argparse.Namespace) -> int:
    run_install_hook(str(Path(args.pipeline).resolve().parent))

    project, pipeline = load_pipeline(args.pipeline, args.downloader)
    emit({"event": "loaded", "project": args.project, "title": getattr(project, "title", "")})

    import httpx  # imported here so the loader is usable without httpx installed

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http:
        ctx = RunContext(http=http, data_dir=args.data_dir, downloader=args.downloader)
        runner = Runner(
            pipeline, args.concurrent_items, ctx,
            on_item_end=lambda item: emit({
                "event": "item_end", "project": args.project,
                "item": item.description(), "state": item.state,
                "completed": runner.completed, "failed": runner.failed,
            }),
        )

        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(__import__("signal").SIGINT, runner.stop)
        except (NotImplementedError, RuntimeError):
            pass  # e.g. not the main thread / unsupported platform

        emit({"event": "running", "project": args.project,
              "concurrent_items": args.concurrent_items})
        await runner.run()

    emit({"event": "stopped", "project": args.project,
          "completed": runner.completed, "failed": runner.failed})
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="worker.run_pipeline")
    parser.add_argument("--project", required=True)
    parser.add_argument("--pipeline", required=True, help="path to the project's pipeline.py")
    parser.add_argument("--downloader", required=True)
    parser.add_argument("--concurrent-items", type=int, default=1)
    parser.add_argument("--data-dir", default="/home/warrior/data")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
