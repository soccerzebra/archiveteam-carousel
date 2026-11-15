"""Byte-for-byte differential: OUR orchestration outputs must equal the upstream warrior's.

Checks against tests/golden/differential_golden.json, captured once from ArchiveTeam's
seesaw via tests/golden/gen_differential.py (needs Tornado 4; see that file). CI depends
only on the committed golden, never on upstream.

What this pins to be identical to upstream: the tracker /request, /upload and /done
(stats) bodies, the realized wget and rsync argv, and the rsync file-list. What it does
NOT compare: the WARC payload (wget-at embeds timestamps/UUIDs, so it is not reproducible
byte-for-byte even across two upstream runs).
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "worker"))
sys.path.insert(0, str(HERE))

from seesaw.config import realize, NumberConfigValue  # noqa: E402
from seesaw.item import ItemValue, ItemInterpolation  # noqa: E402
from seesaw.tracker import (  # noqa: E402
    GetItemFromTracker, UploadWithTracker, PrepareStatsForTracker, SendDoneToTracker)
from seesaw.externalprocess import RsyncUpload, WgetDownload, CurlUpload  # noqa: E402
import differential_scenarios  # noqa: E402

OUR_API = {
    "realize": realize, "NumberConfigValue": NumberConfigValue,
    "ItemValue": ItemValue, "ItemInterpolation": ItemInterpolation,
    "GetItemFromTracker": GetItemFromTracker, "UploadWithTracker": UploadWithTracker,
    "PrepareStatsForTracker": PrepareStatsForTracker, "SendDoneToTracker": SendDoneToTracker,
    "RsyncUpload": RsyncUpload, "WgetDownload": WgetDownload, "CurlUpload": CurlUpload,
}

GOLDEN = json.loads((HERE / "golden" / "differential_golden.json").read_text())


def test_orchestration_output_matches_upstream_byte_for_byte():
    ours = differential_scenarios.outputs(OUR_API)
    expected = {k: v for k, v in GOLDEN.items() if not k.startswith("_")}
    assert ours == expected, "our warrior's orchestration output diverged from upstream"


def test_each_field_individually():  # nicer failure messages than the whole-dict compare
    ours = differential_scenarios.outputs(OUR_API)
    for field in ("request_body", "upload_body", "rsync_argv", "rsync_stdin", "wget_argv", "stats"):
        assert ours[field] == GOLDEN[field], f"{field} differs from upstream"


if __name__ == "__main__":
    test_orchestration_output_matches_upstream_byte_for_byte()
    test_each_field_individually()
    print(f"PASS: identical to upstream seesaw {GOLDEN.get('_upstream_seesaw_version')}")
