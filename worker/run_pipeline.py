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
import contextlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# wget-at ships under several TLS-variant names; project pipelines probe for one of these.
WGET_AT_VARIANTS = ("wget-at", "wget-at-gnutls", "wget-at-openssl", "wget-at-nss")

# When run as a module (python -m worker.run_pipeline) the package root is importable; when
# run as a script, make sure `seesaw` (next to this file) is on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner import Runner  # noqa: E402  (import after the sys.path.insert above)
from seesaw.context import RunContext  # noqa: E402
from seesaw.item import Item  # noqa: E402


def emit(obj: dict) -> None:
    """Write one status line for the supervisor."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def apply_bandwidth_limit(project: str, data_dir: str, limit_rate: int) -> None:
    """Cap wget's download rate at ``limit_rate`` bytes/sec via a generated wgetrc.

    wget reads its startup file from ``$WGETRC``; we write one with ``limit_rate`` and point
    the whole worker process (and therefore every wget it spawns) at it. No project pipeline
    sets ``limit_rate`` or ``WGETRC``, so this is purely additive. ``limit_rate <= 0`` leaves
    wget unthrottled.
    """
    rc_dir = Path(data_dir)
    rc_path = rc_dir / f"carousel-wgetrc-{project}"
    if limit_rate <= 0:
        # Lifting the cap (e.g. a live change back to unlimited): drop our wgetrc so new
        # wgets run unthrottled again.
        if os.environ.get("WGETRC") == str(rc_path):
            os.environ.pop("WGETRC", None)
        with contextlib.suppress(OSError):
            rc_path.unlink()
        return
    rc_dir.mkdir(parents=True, exist_ok=True)
    rc_path.write_text(f"limit_rate = {int(limit_rate)}\n")
    os.environ["WGETRC"] = str(rc_path)


def ensure_wget_at(data_dir: str) -> None:
    """Make the real wget-at binary discoverable at the path project pipelines probe.

    The base image symlinks the variant names into ``/home/warrior/data``, but a host
    directory mounted over ``/home/warrior`` shadows those baked symlinks, so they vanish at
    runtime. Project pipelines resolve ``WGET_AT`` at import time by probing
    ``<data_dir>/wget-at-nss`` (etc.), so recreate the symlinks inside the mounted data dir
    here, before any pipeline loads. No-op when no wget-at binary is on PATH.
    """
    target = shutil.which("wget-lua") or shutil.which("wget-at")
    if not target:
        return
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    for name in WGET_AT_VARIANTS:
        link = d / name
        with contextlib.suppress(OSError):
            if link.is_symlink() and os.readlink(link) == target:
                continue
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(target)


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
        # Submodules are fetched by the installer at clone time, so pipelines that would
        # otherwise run a runtime `git submodule update` (e.g. urlteam2) should skip it —
        # that in-container git update fails and isn't needed.
        "no_submodule": True,
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


async def read_commands(runner: Runner, args: argparse.Namespace) -> None:
    """Apply live control commands the supervisor writes to our stdin, one JSON object per
    line. Lets concurrency and bandwidth change WITHOUT restarting the process, so in-flight
    items keep running. If stdin isn't a readable pipe, there's simply no live control.
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    try:
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    except (ValueError, OSError):
        return  # stdin not connectable (interactive / closed) — no live control channel
    async for raw in reader:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = cmd.get("cmd")
        if action == "reconfigure":
            if "concurrent_items" in cmd:
                runner.set_concurrency(int(cmd["concurrent_items"]))
            if "limit_rate" in cmd:
                apply_bandwidth_limit(args.project, args.data_dir, int(cmd["limit_rate"]))
            emit(
                {
                    "event": "running",
                    "project": args.project,
                    "concurrent_items": runner.concurrent_items,
                }
            )
        elif action == "stop":
            runner.stop()


def run_install_hook(project_dir: str) -> None:
    """Run a project's warrior-install.sh if present (installs its extra dependencies)."""
    hook = Path(project_dir) / "warrior-install.sh"
    if not hook.exists():
        return
    emit({"event": "install_hook", "status": "running"})
    # DEVNULL stdin: our control channel owns the worker's stdin, so the hook must not read it.
    result = subprocess.run(["bash", str(hook)], cwd=project_dir, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"warrior-install.sh failed with code {result.returncode}")
    emit({"event": "install_hook", "status": "done"})


async def run(args: argparse.Namespace) -> int:
    project_dir = Path(args.pipeline).resolve().parent
    run_install_hook(str(project_dir))
    ensure_wget_at(args.data_dir)
    apply_bandwidth_limit(args.project, args.data_dir, args.limit_rate)

    project, pipeline = load_pipeline(args.pipeline, args.downloader)

    # Run with the project directory as the working directory. Projects that shell out to a
    # relative script (e.g. urlteam2's scraper.py with PYTHONPATH=terroroftinytown) and those
    # reading project-dir assets rely on this; wget projects use absolute item_dir paths, so
    # it's harmless for them. One worker hosts one project, so a process-wide chdir is safe.
    os.chdir(project_dir)
    emit({"event": "loaded", "project": args.project, "title": getattr(project, "title", "")})

    import httpx  # imported here so the loader is usable without httpx installed

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http:
        ctx = RunContext(http=http, data_dir=args.data_dir, downloader=args.downloader)
        runner: Runner = Runner(
            pipeline,
            args.concurrent_items,
            ctx,
            on_item_end=lambda item: emit(
                {
                    "event": "item_end",
                    "project": args.project,
                    "item": item.description(),
                    "state": item.state,
                    "ok": item.state == Item.COMPLETED,
                    "error": getattr(item, "last_error", None),
                    "bytes": int(item.get("carousel_bytes", 0)),
                    "completed": runner.completed,
                    "failed": runner.failed,
                }
            ),
            on_threads=lambda threads: emit(
                {
                    "event": "threads",
                    "project": args.project,
                    "threads": threads,
                }
            ),
        )

        loop = asyncio.get_running_loop()
        # e.g. not the main thread / unsupported platform
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(__import__("signal").SIGINT, runner.stop)

        run_task = asyncio.create_task(runner.run())
        # Live control channel (concurrency / bandwidth changes without a restart).
        cmd_task = asyncio.create_task(read_commands(runner, args))
        emit(
            {"event": "running", "project": args.project, "concurrent_items": args.concurrent_items}
        )
        try:
            await run_task
        finally:
            cmd_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cmd_task

    emit(
        {
            "event": "stopped",
            "project": args.project,
            "completed": runner.completed,
            "failed": runner.failed,
        }
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="worker.run_pipeline")
    parser.add_argument("--project", required=True)
    parser.add_argument("--pipeline", required=True, help="path to the project's pipeline.py")
    parser.add_argument("--downloader", required=True)
    parser.add_argument("--concurrent-items", type=int, default=1)
    parser.add_argument("--data-dir", default="/home/warrior/data")
    parser.add_argument(
        "--limit-rate", type=int, default=0, help="per-download cap in bytes/sec (0 = unlimited)"
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
