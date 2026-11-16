"""Generate the realize-layer golden file from UPSTREAM seesaw.

Run deliberately (not in CI) whenever the scenarios change:

    python tests/golden/gen_realize.py

It imports upstream seesaw from reference/ (study-only, never shipped), evaluates the
shared scenarios, and writes realize_golden.json next to this file. The committed JSON —
not upstream seesaw — is the spec the parity test checks against, so CI never depends on
seesaw being importable.
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
UPSTREAM = REPO / "reference" / "seesaw-kit"

sys.path.insert(0, str(UPSTREAM))  # upstream seesaw (no Tornado needed for these modules)
sys.path.insert(0, str(REPO / "tests"))

import realize_scenarios
from seesaw.config import NumberConfigValue, StringConfigValue, realize
from seesaw.item import ItemInterpolation, ItemValue

if "worker" in {p.split("/")[-1] for p in sys.path}:
    raise SystemExit("refusing to run: our package is on the path, want upstream only")

results = realize_scenarios.run(
    realize, ItemValue, ItemInterpolation, NumberConfigValue, StringConfigValue
)
(HERE / "realize_golden.json").write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
print(f"wrote {HERE / 'realize_golden.json'} ({len(results)} cases) from upstream seesaw")
