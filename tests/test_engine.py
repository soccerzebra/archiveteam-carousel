"""Engine tests: task sequencing, LimitConcurrent, Runner concurrency + failure handling.

Fully offline — synthetic tasks, no network, no third-party deps. Exercises the real
asyncio scheduling.
"""

import asyncio
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "worker"))

from runner import GracefulStop, Runner
from seesaw.context import RunContext
from seesaw.item import Item
from seesaw.pipeline import Pipeline
from seesaw.task import LimitConcurrent, SimpleTask, Task

CTX = RunContext()


class Record(SimpleTask):
    def __init__(self, name, log):
        super().__init__(name)
        self.log = log

    def process(self, item):
        self.log.append(self.name)


def test_pipeline_runs_tasks_in_order():
    log = []
    pipe = Pipeline(Record("A", log), Record("B", log), Record("C", log))
    asyncio.run(pipe.run_item(Item(None, {"item_name": "x"}), CTX))
    assert log == ["A", "B", "C"]


def test_canceled_item_stops_mid_pipeline():
    log = []

    class CancelHere(SimpleTask):
        def process(self, item):
            log.append("cancel")
            item.cancel()

    pipe = Pipeline(Record("A", log), CancelHere("x"), Record("C", log))
    asyncio.run(pipe.run_item(Item(None, {"item_name": "x"}), CTX))
    assert log == ["A", "cancel"]  # C never ran


def test_limit_concurrent_caps_simultaneous_inner_runs():
    gauge = {"cur": 0, "max": 0}

    class Slow(Task):
        async def run(self, item, ctx):
            gauge["cur"] += 1
            gauge["max"] = max(gauge["max"], gauge["cur"])
            await asyncio.sleep(0.01)
            gauge["cur"] -= 1

    limited = LimitConcurrent(2, Slow("slow"))  # one shared semaphore across all items
    pipe = Pipeline(limited)

    async def go():
        await asyncio.gather(
            *[pipe.run_item(Item(None, {"item_name": str(i)}), CTX) for i in range(6)]
        )

    asyncio.run(go())
    assert gauge["max"] == 2


def test_runner_concurrency_and_completion():
    """9 items, 3 workers: all complete, never more than 3 in flight at once."""
    produced = {"n": 0}
    gauge = {"cur": 0, "max": 0}
    TOTAL = 9

    class Claim(Task):
        async def run(self, item, ctx):
            # No await between the check and the increment, so it's atomic under asyncio.
            if produced["n"] >= TOTAL:
                raise GracefulStop
            produced["n"] += 1
            item["item_name"] = f"item{produced['n']}"

    class Work(Task):
        async def run(self, item, ctx):
            gauge["cur"] += 1
            gauge["max"] = max(gauge["max"], gauge["cur"])
            await asyncio.sleep(0.005)
            gauge["cur"] -= 1

    runner = Runner(
        Pipeline(Claim("claim"), Work("work")), concurrent_items=3, ctx=CTX, fail_delay=0
    )
    asyncio.run(runner.run())
    assert runner.completed == TOTAL
    assert gauge["max"] == 3


def test_runner_survives_item_failure():
    seen = []

    class Claim(Task):
        def __init__(self):
            super().__init__("claim")
            self.n = 0

        async def run(self, item, ctx):
            if self.n >= 3:
                raise GracefulStop
            self.n += 1
            item["item_name"] = f"item{self.n}"

    class MaybeFail(Task):
        async def run(self, item, ctx):
            seen.append(item["item_name"])
            if item["item_name"] == "item2":
                raise RuntimeError("boom")

    runner = Runner(Pipeline(Claim(), MaybeFail("mf")), concurrent_items=1, ctx=CTX, fail_delay=0)
    asyncio.run(runner.run())
    assert seen == ["item1", "item2", "item3"]  # kept going after item2 failed
    assert runner.completed == 2 and runner.failed == 1


def test_on_finish_fires_on_success_and_failure():
    finished = []

    class Claim(Task):
        def __init__(self):
            super().__init__("claim")
            self.n = 0

        async def run(self, item, ctx):
            if self.n >= 2:
                raise GracefulStop
            self.n += 1
            item["n"] = self.n
            item.on_finish += lambda it: finished.append((it["n"], it.state))

    class MaybeFail(Task):
        async def run(self, item, ctx):
            if item["n"] == 2:
                raise RuntimeError("boom")

    runner = Runner(Pipeline(Claim(), MaybeFail("mf")), concurrent_items=1, ctx=CTX, fail_delay=0)
    asyncio.run(runner.run())
    assert (1, Item.COMPLETED) in finished  # fired on success
    assert (2, Item.FAILED) in finished  # and on failure


def test_runner_seeds_data_dir_on_each_item():
    seen = {}

    class Capture(Task):
        async def run(self, item, ctx):
            seen["data_dir"] = item["data_dir"]  # projects' PrepareDirectories reads this
            raise GracefulStop

    ctx = RunContext(data_dir="/home/warrior/data")
    runner = Runner(Pipeline(Capture("cap")), concurrent_items=1, ctx=ctx)
    asyncio.run(runner.run())
    assert seen["data_dir"] == "/home/warrior/data"


def test_derive_phase_classifies_from_latest_line():
    from runner import derive_phase

    # Only the newest non-empty line decides; first phrase match wins.
    assert derive_phase(["Contacting tracker.", "Trying again in 107 seconds."]) == "waiting"
    assert derive_phase(["No free items available currently."]) == "waiting"
    assert derive_phase(["Sleeping for a bit..."]) == "sleeping"
    assert derive_phase(["WgetDownload returned 8", "Retrying WgetDownload after 2s."]) == "retrying"
    assert derive_phase(["Tracker rate limiting is active; slowing down."]) == "rate-limited"
    # A newer working line overrides an older rest line.
    assert derive_phase(["Trying again in 60 seconds.", "Downloading https://x/"]) == ""
    assert derive_phase(["", "   "]) == ""  # no usable line => working
    assert derive_phase([]) == ""


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
