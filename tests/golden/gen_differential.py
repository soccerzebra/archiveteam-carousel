"""Generate the differential golden from the UPSTREAM warrior (ArchiveTeam/seesaw-kit).

Upstream needs Tornado 4 (its tracker/externalprocess/pipeline modules import
``tornado.stack_context`` etc., removed in Tornado 5+). So run this with an interpreter
that has tornado<5 installed — NOT the project's main venv:

    python3.9 -m venv /tmp/tv && /tmp/tv/bin/pip install 'tornado>=4,<5'
    /tmp/tv/bin/python tests/golden/gen_differential.py

It imports upstream seesaw from reference/, runs the shared scenario, and writes
differential_golden.json. The committed golden — not upstream — is what the test checks
against, so CI never needs Tornado 4.
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "reference" / "seesaw-kit"))
sys.path.insert(0, str(REPO / "tests"))

import differential_scenarios
from seesaw.config import NumberConfigValue, realize
from seesaw.externalprocess import CurlUpload, RsyncUpload, WgetDownload
from seesaw.item import ItemInterpolation, ItemValue
from seesaw.tracker import (
    GetItemFromTracker,
    PrepareStatsForTracker,
    SendDoneToTracker,
    UploadWithTracker,
)

api = {
    "realize": realize,
    "NumberConfigValue": NumberConfigValue,
    "ItemValue": ItemValue,
    "ItemInterpolation": ItemInterpolation,
    "GetItemFromTracker": GetItemFromTracker,
    "UploadWithTracker": UploadWithTracker,
    "PrepareStatsForTracker": PrepareStatsForTracker,
    "SendDoneToTracker": SendDoneToTracker,
    "RsyncUpload": RsyncUpload,
    "WgetDownload": WgetDownload,
    "CurlUpload": CurlUpload,
}

import seesaw

result = {"_upstream_seesaw_version": seesaw.__version__, **differential_scenarios.outputs(api)}
(HERE / "differential_golden.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
print(f"wrote differential_golden.json from upstream seesaw {seesaw.__version__}")
