"""Running subprocesses asynchronously — wget-at, rsync, curl.

Public surface projects use: ``ExternalProcess``, ``WgetDownload``, ``RsyncUpload``,
``CurlUpload``.

The wget/rsync/curl command-line flags must match what the archiving infra expects, so
they are fixed; execution is ``asyncio.create_subprocess_exec`` with a retry loop. Two
deliberate choices:
  * each subprocess inherits the worker's cwd — the PROJECT directory — so a pipeline's
    relative assets (``--lua-script youtube.lua``, ``data.txt``, ignore-lists) resolve, just
    as in the real warrior. Per-item outputs use absolute ``%(item_dir)s`` paths, so a shared
    cwd is safe across concurrent items (the worker chdirs there once at startup, and runs
    exactly one project);
  * the child environment is ``os.environ`` overlaid with the task's vars, so PATH/HOME
    are present while every variable a project sets (read by its .lua scripts) is applied.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import os.path
import re
from pathlib import Path
from typing import Any

from seesaw.config import realize
from seesaw.task import Task

# wget -nv prints one line per saved response: `… URL:<url> [<bytes>] -> "<file>" [1]`.
# Summing <bytes> across the item's wget output is the exact total pulled off the network
# (wget core prints these even when a project's .lua customizes other logging).
_WGET_BYTES_RE = re.compile(r"URL:\S+ \[(\d+)(?:/\d+)?\] -> ")


def _sum_wget_downloaded(text: str) -> int:
    return sum(int(m.group(1)) for m in _WGET_BYTES_RE.finditer(text))


def _wget_log_path(argv: list[str]) -> str | None:
    """The file wget was told to log to (`-o FILE` / `--output-file FILE`), if any."""
    for flag in ("-o", "--output-file"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                return argv[i + 1]
    return None


# Child output is split on \r as well as \n: `rsync --progress` (and wget's progress bar)
# redraw one line with bare carriage returns, so a newline-only reader would see a single
# ever-growing "line" for the whole transfer.
_LINE_BREAKS = re.compile(rb"[\r\n]+")
_READ_CHUNK = 64 * 1024
_MAX_LINE = 64 * 1024  # an unterminated run longer than this is flushed as its own line


async def _pump_lines(stream: asyncio.StreamReader, emit: Any) -> None:
    """Feed ``stream`` to ``emit`` one text line at a time until EOF.

    Reads raw chunks rather than ``readline()``, so no amount of unterminated output can
    overrun the reader's buffer limit. Blank lines are dropped.
    """
    pending = b""
    while chunk := await stream.read(_READ_CHUNK):
        *lines, pending = _LINE_BREAKS.split(pending + chunk)
        for line in lines:
            if line:
                emit(line.decode("utf-8", "replace"))
        if len(pending) > _MAX_LINE:
            emit(pending.decode("utf-8", "replace"))
            pending = b""
    if pending:
        emit(pending.decode("utf-8", "replace"))


class ExternalProcessError(Exception):
    def __init__(self, name: str, exit_code: int):
        super().__init__(f"{name} failed with exit code {exit_code}")
        self.name = name
        self.exit_code = exit_code


class ExternalProcess(Task):
    def __init__(
        self,
        name,
        args,
        max_tries=1,
        retry_delay=2,
        accept_on_exit_code=None,
        retry_on_exit_code=None,
        env=None,
    ):
        super().__init__(name)
        self.args = args
        self.max_tries = max_tries  # None => unlimited
        self.retry_delay = retry_delay
        self.accept_on_exit_code = [0] if accept_on_exit_code is None else list(accept_on_exit_code)
        self.retry_on_exit_code = retry_on_exit_code  # None => retry on any non-accept code
        self.env = dict(env or {})
        self.env.setdefault("PYTHONIOENCODING", "utf8:replace")

    def stdin_data(self, item: Any) -> bytes:
        return b""

    def build_args(self, item: Any) -> list[str]:
        """Resolve args to concrete strings. (A project's WgetArgs().realize() already
        returns concrete values; list args resolve element-wise.)"""
        return [str(a) for a in realize(self.args, item)]

    def build_env(self, item: Any) -> dict[str, str]:
        overlay = {str(k): str(v) for k, v in realize(self.env, item).items()}
        return {**os.environ, **overlay}

    async def run(self, item: Any, ctx: Any) -> None:
        item.log_output(f"Starting {self.name} for {item.description()}.")
        tries = 0
        while True:
            tries += 1
            exit_code = await self._spawn(item)
            if exit_code in self.accept_on_exit_code:
                item.log_output(f"Finished {self.name} for {item.description()}.")
                return
            item.log_output(f"{self.name} returned exit code {exit_code} for {item.description()}.")
            may_retry = self.max_tries is None or tries < self.max_tries
            code_wants_retry = (
                self.retry_on_exit_code is None or exit_code in self.retry_on_exit_code
            )
            if may_retry and code_wants_retry:
                item.log_output(f"Retrying {self.name} after {self.retry_delay}s (try {tries}).")
                await asyncio.sleep(self.retry_delay)
                continue
            raise ExternalProcessError(self.name, exit_code)

    async def _spawn(self, item: Any) -> int:
        args = self.build_args(item)
        # Stash the realized argv on the item (per-item, so concurrency-safe) so a subclass can
        # inspect it after the run without re-realizing (which would re-fire WgetArgs side effects).
        item.carousel_argv = args
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.build_env(item),
            # Inherit the worker's cwd (the project dir) so relative pipeline assets — lua
            # scripts, data.txt, ignore-lists — resolve; outputs use absolute item_dir paths.
            cwd=None,
        )

        async def feed_stdin() -> None:
            try:
                data = self.stdin_data(item)
                if data and proc.stdin is not None:
                    proc.stdin.write(data)
                    await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                if proc.stdin is not None:
                    with contextlib.suppress(Exception):
                        proc.stdin.close()

        feeder = asyncio.create_task(feed_stdin())
        assert proc.stdout is not None
        await _pump_lines(proc.stdout, item.log_output)
        await feeder
        return await proc.wait()


class WgetDownload(ExternalProcess):
    def __init__(
        self,
        args,
        max_tries=1,
        accept_on_exit_code=None,
        retry_on_exit_code=None,
        env=None,
        stdin_data_function=None,
    ):
        super().__init__(
            "WgetDownload",
            args=args,
            max_tries=max_tries,
            accept_on_exit_code=[0] if accept_on_exit_code is None else accept_on_exit_code,
            retry_on_exit_code=retry_on_exit_code,
            env=env,
        )
        self.stdin_data_function = stdin_data_function

    def stdin_data(self, item: Any) -> bytes:
        return self.stdin_data_function(item) if self.stdin_data_function else b""

    async def run(self, item: Any, ctx: Any) -> None:
        await super().run(item, ctx)
        # Tally the real bytes pulled off the network for this item. wget logs to a file
        # (`-o FILE`, the common case — complete) or, absent that, to stdout which we captured
        # in item.log (may be truncated for very large crawls; accepted). Accumulates across
        # multiple WgetDownload tasks / retries on the same item.
        argv = getattr(item, "carousel_argv", None) or []
        text = ""
        log_path = _wget_log_path(argv)
        if log_path and os.path.exists(log_path):
            with contextlib.suppress(OSError):
                text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        if not text:
            text = "\n".join(item.log)
        prior = int(item.get("carousel_bytes_down", 0))
        item["carousel_bytes_down"] = prior + _sum_wget_downloaded(text)


def _record_bytes(item: Any, paths: list) -> None:
    """Add the on-disk size of ``paths`` to the item's archived-bytes tally.

    Called after a successful upload, so the files are still present. The total is read by
    the worker on item completion and reported to the control plane for the stats history.
    """
    total = 0
    for p in paths:
        with contextlib.suppress(OSError):
            total += os.path.getsize(p)
    item["carousel_bytes"] = int(item.get("carousel_bytes", 0)) + total


class RsyncUpload(ExternalProcess):
    def __init__(
        self, target, files, target_source_path="./", bwlimit="0", max_tries=None, extra_args=None
    ):
        args = [
            "rsync",
            "-rltv",
            "--timeout=300",
            "--contimeout=300",
            "--progress",
            "--bwlimit",
            bwlimit,
        ]
        if extra_args is not None:
            args.extend(extra_args)
        args.extend(["--files-from=-", target_source_path, target])
        super().__init__("RsyncUpload", args=args, max_tries=max_tries)
        self.files = files
        self.target_source_path = target_source_path

    def build_args(self, item: Any) -> list[str]:
        # rsync accepts --contimeout only for daemon (rsync://) targets; strict rsync (e.g.
        # Ubuntu 3.2.7) errors on it for a local copy. Real upload targets are rsync:// daemons,
        # so they keep it unchanged; drop it only for a non-daemon target.
        args = super().build_args(item)
        target = args[-1] if args else ""
        if not target.startswith("rsync://"):
            args = [a for a in args if a != "--contimeout=300"]
        return args

    def stdin_data(self, item: Any) -> bytes:
        base = realize(self.target_source_path, item)
        lines = [f"{os.path.relpath(realize(f, item), base)}\n" for f in realize(self.files, item)]
        return "".join(lines).encode("utf-8")

    async def run(self, item: Any, ctx: Any) -> None:
        await super().run(item, ctx)
        _record_bytes(item, [realize(f, item) for f in realize(self.files, item)])


class CurlUpload(ExternalProcess):
    def __init__(
        self,
        target,
        filename,
        connect_timeout="60",
        speed_limit="1",
        speed_time="900",
        max_tries=None,
    ):
        args = [
            "curl",
            "--fail",
            "--output",
            "/dev/null",
            "--connect-timeout",
            str(connect_timeout),
            "--speed-limit",
            str(speed_limit),
            "--speed-time",
            str(speed_time),
            "--header",
            f"X-Curl-Limits: inf,{speed_limit},{speed_time}",
            "--write-out",
            "Upload server: %{url_effective}\\n",
            "--location",
            "--upload-file",
            filename,
            target,
        ]
        super().__init__("CurlUpload", args=args, max_tries=max_tries)
        self.filename = filename

    async def run(self, item: Any, ctx: Any) -> None:
        await super().run(item, ctx)
        _record_bytes(item, [realize(self.filename, item)])
