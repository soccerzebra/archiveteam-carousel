"""Shared scenario for the byte-for-byte differential vs. the upstream warrior.

Extracts the orchestration OUTPUTS a warrior produces for a fixed project wiring + item:
the tracker request/upload bodies, the realized wget/rsync argv, the rsync file-list sent
on stdin, and the /done stats dict. These are the bytes our warrior sends and the commands
it execs — the real behavioral contract. (The WARC payload itself is produced by wget-at
and embeds timestamps/UUIDs, so it is inherently non-reproducible and not compared.)

The same function runs against UPSTREAM seesaw (to generate the golden) and against OURS
(in the test). ``api`` supplies each side's classes + realize, so neither import path is
hard-coded (both packages are named ``seesaw``).
"""

from __future__ import annotations

from typing import Any

ITEM = {
    "item_name": "a89f2c1b",
    "item_dir": "/data/demo/a89f2c1b",
    "data_dir": "/data/demo/a89f2c1b/data",
    "warc_file_base": "demo-20261027-000000-abcd",
    "dict_project": "demo",
    "dict_id": "7",
}
DOWNLOADER = "josh"
VERSION = "20261027.01"


def outputs(api: dict) -> dict[str, Any]:
    realize = api["realize"]
    II = api["ItemInterpolation"]
    GetItemFromTracker = api["GetItemFromTracker"]
    UploadWithTracker = api["UploadWithTracker"]
    PrepareStatsForTracker = api["PrepareStatsForTracker"]
    RsyncUpload = api["RsyncUpload"]
    WgetDownload = api["WgetDownload"]

    item = dict(ITEM)
    res: dict[str, Any] = {}

    res["request_body"] = GetItemFromTracker(
        "https://tracker.example/demo/multi=1/", DOWNLOADER, VERSION
    ).data(item)

    res["upload_body"] = UploadWithTracker(
        "https://tracker.example/demo",
        downloader=DOWNLOADER,
        version=VERSION,
        files=[II("%(data_dir)s/%(warc_file_base)s.warc.zst")],
    ).data(item)

    rsync = RsyncUpload(
        "rsync://tracker.example/demo/",
        [II("%(data_dir)s/%(warc_file_base)s.warc.zst")],
        target_source_path=II("%(data_dir)s/"),
        bwlimit="0",
        extra_args=["--recursive", "--min-size", "1", "--no-compress", "--compress-level", "0"],
    )
    res["rsync_argv"] = realize(rsync.args, item)
    res["rsync_stdin"] = rsync.stdin_data(item).decode("utf-8")

    wget = WgetDownload(
        [
            "wget-at",
            "-o",
            II("%(item_dir)s/wget.log"),
            "--warc-file",
            II("%(item_dir)s/%(warc_file_base)s"),
            "--warc-header",
            "x-wget-at-project-name: demo",
            "https://example.org/",
        ]
    )
    res["wget_argv"] = realize(wget.args, item)

    stats = PrepareStatsForTracker(
        defaults={"downloader": DOWNLOADER, "version": VERSION},
        file_groups={},  # no filesystem dependency; byte-summing is covered elsewhere
        id_function=lambda it: it["warc_file_base"],
    )
    stats.process(item)
    res["stats"] = item["stats"]

    return res
