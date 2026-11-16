# A minimal pipeline that can be driven end-to-end offline against a fake tracker:
# claim an item, compute stats, report done. No wget/rsync, so no real binaries or network.
from seesaw.item import ItemValue
from seesaw.pipeline import Pipeline
from seesaw.project import Project
from seesaw.task import SimpleTask
from seesaw.tracker import GetItemFromTracker, SendDoneToTracker

VERSION = "1"
TRACKER = "https://tracker.example/demo"

project = Project(title="Runnable demo")


class MakeStats(SimpleTask):
    def __init__(self):
        super().__init__("MakeStats")

    def process(self, item):
        item["stats"] = {
            "item": item["item_name"],
            "bytes": {"data": 0},
            "downloader": downloader,
            "version": VERSION,
        }


pipeline = Pipeline(
    GetItemFromTracker(TRACKER + "/multi=1/", downloader, VERSION),
    MakeStats(),
    SendDoneToTracker(tracker_url=TRACKER, stats=ItemValue("stats")),
)
