"""externalprocess tests: argv compatibility, stdin_data, and real subprocess execution.

Argv assertions pin the rsync/curl flag sets to upstream (compatibility). Execution tests
spawn real harmless `sh -c` commands — offline, no third-party deps.
"""

import asyncio
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "worker"))

from seesaw.context import RunContext
from seesaw.externalprocess import (
    CurlUpload,
    ExternalProcess,
    ExternalProcessError,
    RsyncUpload,
    WgetDownload,
)
from seesaw.item import (
    Item,
    ItemInterpolation,
)

CTX = RunContext()


def run(task, item):
    return asyncio.run(task.run(item, CTX))


# --- argv compatibility ------------------------------------------------------------


def test_rsync_argv_matches_upstream():
    task = RsyncUpload(
        "rsync://t.example/urls/",
        ["/data/f.warc.zst"],
        target_source_path="/data/",
        bwlimit="0",
        extra_args=["--recursive", "--min-size", "1"],
        max_tries=1,
    )
    assert task.args == [
        "rsync",
        "-rltv",
        "--timeout=300",
        "--contimeout=300",
        "--progress",
        "--bwlimit",
        "0",
        "--recursive",
        "--min-size",
        "1",
        "--files-from=-",
        "/data/",
        "rsync://t.example/urls/",
    ]


def test_curl_argv_matches_upstream():
    task = CurlUpload("https://up.example/urls/", "/data/f.warc.zst", "60", "1", "900")
    assert task.args == [
        "curl",
        "--fail",
        "--output",
        "/dev/null",
        "--connect-timeout",
        "60",
        "--speed-limit",
        "1",
        "--speed-time",
        "900",
        "--header",
        "X-Curl-Limits: inf,1,900",
        "--write-out",
        "Upload server: %{url_effective}\\n",
        "--location",
        "--upload-file",
        "/data/f.warc.zst",
        "https://up.example/urls/",
    ]


def test_rsync_stdin_data_is_relative_file_list():
    item = Item(None, {"data_dir": "/data/urls"})
    task = RsyncUpload(
        "rsync://t/urls/",
        [
            ItemInterpolation("%(data_dir)s/a.warc.zst"),
            ItemInterpolation("%(data_dir)s/a_data.txt"),
        ],
        target_source_path=ItemInterpolation("%(data_dir)s/"),
    )
    assert task.stdin_data(item) == b"a.warc.zst\na_data.txt\n"


def test_wget_stdin_data_function():
    assert (
        WgetDownload([], stdin_data_function=lambda item: b"payload").stdin_data(Item())
        == b"payload"
    )
    assert WgetDownload([]).stdin_data(Item()) == b""


# --- real execution ----------------------------------------------------------------


def test_process_completes_and_captures_output():
    item = Item(None, {"item_name": "x"})
    run(ExternalProcess("echo", ["sh", "-c", "echo hello-stdout; echo hello-stderr 1>&2"]), item)
    joined = "\n".join(item.log)
    assert "hello-stdout" in joined and "hello-stderr" in joined  # stderr merged into stdout


def test_custom_accept_exit_code():
    item = Item(None, {"item_name": "x"})
    run(ExternalProcess("exit4", ["sh", "-c", "exit 4"], accept_on_exit_code=[0, 4, 8]), item)
    # no exception => accepted


def test_failure_raises_after_retries():
    item = Item(None, {"item_name": "x"})
    try:
        run(ExternalProcess("fail", ["sh", "-c", "exit 1"], max_tries=2, retry_delay=0), item)
        assert False, "expected ExternalProcessError"
    except ExternalProcessError as e:
        assert e.exit_code == 1
    # max_tries=2 => exactly one retry
    assert sum("Retrying" in line for line in item.log) == 1


def test_retry_on_exit_code_filters():
    item = Item(None, {"item_name": "x"})
    try:
        run(
            ExternalProcess(
                "fail5", ["sh", "-c", "exit 5"], max_tries=5, retry_delay=0, retry_on_exit_code=[4]
            ),
            item,
        )
        assert False, "expected ExternalProcessError"
    except ExternalProcessError as e:
        assert e.exit_code == 5
    # 5 not in retry_on_exit_code => no retry despite max_tries=5
    assert not any("Retrying" in line for line in item.log)


def test_runs_in_inherited_cwd_not_item_dir():
    # Pipelines keep relative assets (lua scripts, data.txt) in the project dir, so the
    # subprocess must inherit the worker's cwd — NOT chdir into item_dir (that broke youtube:
    # wget-at couldn't open youtube.lua). Outputs still use absolute item_dir paths.
    import os
    import tempfile

    item = Item()
    item["item_dir"] = tempfile.mkdtemp()  # a DIFFERENT directory
    run(ExternalProcess("pwd", ["sh", "-c", "pwd"]), item)
    printed = [ln.strip() for ln in item.log if ln.strip().startswith("/")]
    assert printed and printed[-1] == os.getcwd()  # inherited cwd
    assert printed[-1] != item["item_dir"]  # did NOT cd into item_dir


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
