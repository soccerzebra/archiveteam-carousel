"""Parity test: OUR realize layer must reproduce upstream's golden outputs exactly.

CI runs this; it depends only on the committed golden JSON, never on upstream seesaw.
Regenerate the golden with tests/golden/gen_realize.py when scenarios change.
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "worker"))   # our clean-room `seesaw`
sys.path.insert(0, str(HERE))

from seesaw.config import realize, NumberConfigValue, StringConfigValue  # noqa: E402
from seesaw.item import ItemValue, ItemInterpolation  # noqa: E402
import realize_scenarios  # noqa: E402

GOLDEN = json.loads((HERE / "golden" / "realize_golden.json").read_text())


def test_realize_layer_matches_golden():
    ours = realize_scenarios.run(
        realize, ItemValue, ItemInterpolation, NumberConfigValue, StringConfigValue
    )
    assert ours == GOLDEN, "clean-room realize layer diverged from the seesaw golden"


if __name__ == "__main__":
    test_realize_layer_matches_golden()
    print("PASS: realize layer matches golden")
