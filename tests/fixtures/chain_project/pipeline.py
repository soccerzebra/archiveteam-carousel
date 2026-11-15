# Full-chain fixture: a complete claim -> download -> stats -> upload -> done pipeline that
# uses only `curl` (no wget-at), so it runs hermetically against a local HTTP server.
# `downloader`, `base_url`, and `data_root` are injected by the loader (base_url/data_root
# via extra_context), exactly as the warrior injects `downloader` for real projects.
import os

from seesaw.project import Project
from seesaw.pipeline import Pipeline
from seesaw.item import ItemValue, ItemInterpolation
from seesaw.task import SimpleTask
from seesaw.externalprocess import ExternalProcess
from seesaw.tracker import (
    GetItemFromTracker, PrepareStatsForTracker, UploadWithTracker, SendDoneToTracker,
)

VERSION = "1.0"
TRACKER = base_url + "/demo"

project = Project(title="Chain demo")


class PrepareDirectories(SimpleTask):
    def __init__(self):
        super().__init__("PrepareDirectories")

    def process(self, item):
        item_dir = os.path.join(data_root, item["item_name"])
        os.makedirs(os.path.join(item_dir, "data"), exist_ok=True)
        item["item_dir"] = item_dir
        item["data_dir"] = os.path.join(item_dir, "data")
        item["warc_file_base"] = "demo-" + item["item_name"]


pipeline = Pipeline(
    GetItemFromTracker(TRACKER + "/multi=1/", downloader, VERSION),
    PrepareDirectories(),
    ExternalProcess(
        "Download",
        ["curl", "-s", "-o", ItemInterpolation("%(data_dir)s/%(warc_file_base)s.warc.zst"),
         base_url + "/origin"],
        max_tries=1,
    ),
    PrepareStatsForTracker(
        defaults={"downloader": downloader, "version": VERSION},
        file_groups={"data": [ItemInterpolation("%(data_dir)s/%(warc_file_base)s.warc.zst")]},
        id_function=lambda item: item["warc_file_base"],
    ),
    UploadWithTracker(
        TRACKER, downloader=downloader, version=VERSION,
        files=[ItemInterpolation("%(data_dir)s/%(warc_file_base)s.warc.zst")],
    ),
    SendDoneToTracker(tracker_url=TRACKER, stats=ItemValue("stats")),
)
