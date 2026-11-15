# A synthetic pipeline shaped like reference/urls-grab/pipeline.py, but with no external
# dependencies (no wget-at, requests, lupa, zstandard) so the LOADER + full seesaw surface
# can be exercised offline. `downloader` is injected by the loader, exactly as the warrior
# injects it for real projects.
from seesaw.config import NumberConfigValue
from seesaw.externalprocess import WgetDownload
from seesaw.item import ItemInterpolation, ItemValue
from seesaw.pipeline import Pipeline
from seesaw.project import Project
from seesaw.task import LimitConcurrent, SimpleTask
from seesaw.tracker import (
    GetItemFromTracker,
    PrepareStatsForTracker,
    SendDoneToTracker,
    UploadWithTracker,
)

VERSION = "20261101.01"
TRACKER_HOST = "tracker.example"
TRACKER_ID = "demo"


class PrepareDirectories(SimpleTask):
    def __init__(self):
        super().__init__("PrepareDirectories")

    def process(self, item):
        item["item_dir"] = "/data/%s" % item["item_name"]
        item["data_dir"] = "/data/%s/data" % item["item_name"]
        item["warc_file_base"] = "demo-%s" % item["item_name"]


project = Project(
    title="Demo project",
    project_html="<h2>demo</h2>",
)

pipeline = Pipeline(
    GetItemFromTracker(
        f"https://{TRACKER_HOST}/{TRACKER_ID}/multi=1/", downloader, VERSION
    ),
    PrepareDirectories(),
    WgetDownload(["echo", "would-grab"], max_tries=1, accept_on_exit_code=[0]),
    PrepareStatsForTracker(
        defaults={"downloader": downloader, "version": VERSION},
        file_groups={"data": [ItemInterpolation("%(item_dir)s/%(warc_file_base)s.warc.zst")]},
        id_function=lambda item: item["warc_file_base"],
    ),
    LimitConcurrent(
        NumberConfigValue(
            min=1,
            max=20,
            default="2",
            name="shared:rsync_threads",
            title="Rsync threads",
            description="max concurrent uploads",
        ),
        UploadWithTracker(
            "https://%s/%s" % (TRACKER_HOST, TRACKER_ID),
            downloader=downloader,
            version=VERSION,
            files=[ItemInterpolation("%(data_dir)s/%(warc_file_base)s.warc.zst")],
            rsync_target_source_path=ItemInterpolation("%(data_dir)s/"),
            rsync_extra_args=["--recursive"],
        ),
    ),
    SendDoneToTracker(
        tracker_url="https://%s/%s" % (TRACKER_HOST, TRACKER_ID),
        stats=ItemValue("stats"),
    ),
)
