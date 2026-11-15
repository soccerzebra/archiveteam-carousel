"""Shared differential-test scenarios for the realize/templating layer.

This is the single source of truth for what "compatible" means at the resolution layer.
Each scenario builds lazy objects and resolves them against an item, returning a
JSON-serializable result. The SAME scenarios are run two ways:

  * ``tests/golden/gen_realize.py`` runs them through UPSTREAM seesaw (reference/) to
    produce the committed golden file;
  * ``tests/test_realize_parity.py`` runs them through OUR seesaw and asserts equality.

The scenarios take the API as arguments so neither import path is hard-coded (the two
packages are both named ``seesaw`` and cannot be imported into one process).

Scenarios are drawn from real usage in reference/urls-grab/pipeline.py so parity here
means parity on the values a real project actually resolves.
"""

from __future__ import annotations

from typing import Any, Callable


def run(
    realize: Callable,
    ItemValue: type,
    ItemInterpolation: type,
    NumberConfigValue: type,
    StringConfigValue: type,
) -> dict[str, Any]:
    # A representative item, shaped like one mid-pipeline in urls-grab.
    item = {
        "item_dir": "/data/urls/abc123",
        "data_dir": "/data/urls/abc123/data",
        "warc_file_base": "urls-20261027-000000-abcd",
        "dict_project": "urls",
        "dict_id": "42",
        "item_name": "url:12345",
    }

    results: dict[str, Any] = {}

    # Plain passthroughs.
    results["passthrough_str"] = realize("literal", item)
    results["passthrough_int"] = realize(7, item)

    # ItemValue -> item[key].
    results["itemvalue"] = realize(ItemValue("warc_file_base"), item)

    # ItemInterpolation -> fmt % item (the upload file path in urls-grab).
    results["interp_upload_file"] = realize(
        ItemInterpolation("%(data_dir)s/%(warc_file_base)s.%(dict_project)s.%(dict_id)s.warc.zst"),
        item,
    )
    results["interp_stats_file"] = realize(
        ItemInterpolation("%(item_dir)s/%(warc_file_base)s.warc.zst"), item
    )

    # ConfigValue -> its current value (urls-grab's rsync_threads knob).
    results["number_default"] = realize(
        NumberConfigValue(min=1, max=20, default="2", name="shared:rsync_threads",
                          title="Rsync threads", description="max concurrent uploads"),
        item,
    )

    # Nested dict/list, like WgetDownload(env={...}) and files=[...].
    results["nested_env"] = realize(
        {
            "item_dir": ItemValue("item_dir"),
            "warc_file_base": ItemValue("warc_file_base"),
            "static": "x",
        },
        item,
    )
    results["nested_list"] = realize(
        [ItemInterpolation("%(data_dir)s/"), ItemValue("item_name"), "literal"], item
    )

    return results
