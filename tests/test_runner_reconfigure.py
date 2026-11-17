"""Runner live-reconfigure tests: set_concurrency() grows and shrinks the slot pool at
runtime without restarting, and never interrupts an in-flight item. Fully offline."""

import asyncio
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "worker"))

from runner import GracefulStop, Runner
from seesaw.context import RunContext
from seesaw.pipeline import Pipeline
from seesaw.task import Task

CTX = RunContext()


def _make_runner(gauge, start):
    class Claim(Task):
        async def run(self, item, ctx):
            item["item_name"] = "x"

    class Work(Task):
        async def run(self, item, ctx):
            gauge["cur"] += 1
            gauge["max"] = max(gauge["max"], gauge["cur"])
            await asyncio.sleep(0.02)
            gauge["cur"] -= 1

    return Runner(Pipeline(Claim("c"), Work("w")), concurrent_items=start, ctx=CTX, fail_delay=0)


def test_set_concurrency_grows_and_shrinks_live():
    async def scenario():
        gauge = {"cur": 0, "max": 0}
        runner = _make_runner(gauge, start=1)
        task = asyncio.create_task(runner.run())

        await asyncio.sleep(0.05)  # settle at 1 slot
        runner.set_concurrency(4)
        await asyncio.sleep(0.12)  # ramp up to 4
        peak = gauge["max"]

        runner.set_concurrency(2)
        await asyncio.sleep(0.12)  # drain surplus slots
        active = len([i for i in runner._slots if i not in runner._retiring])

        runner.stop()
        await task
        return peak, active, runner.completed

    peak, active, completed = asyncio.run(scenario())
    assert peak >= 4  # grew to 4 concurrent items
    assert active == 2  # shrank back to 2 live slots
    assert completed > 0  # items kept completing throughout


def test_shrink_lets_the_retired_slots_item_finish():
    """Shrinking must let a retired slot complete its in-flight item, not abandon it."""

    async def scenario():
        finished = []
        release = asyncio.Event()
        state = {"started": 0, "claimed": 0}

        class Claim(Task):
            async def run(self, item, ctx):
                if state["claimed"] >= 2:  # exactly two items ever enter the pipeline
                    raise GracefulStop
                state["claimed"] += 1
                item["item_name"] = f"item{state['claimed']}"

        class Work(Task):
            async def run(self, item, ctx):
                state["started"] += 1
                await release.wait()  # hold both items open
                finished.append(item["item_name"])

        runner = Runner(Pipeline(Claim("c"), Work("w")), concurrent_items=2, ctx=CTX, fail_delay=0)
        task = asyncio.create_task(runner.run())

        while state["started"] < 2:  # both slots are mid-item
            await asyncio.sleep(0.005)
        runner.set_concurrency(1)  # retire one of the two in-flight slots
        await asyncio.sleep(0.02)
        assert finished == []  # neither item was killed by the shrink

        release.set()  # let both complete
        await task
        return sorted(finished)

    assert asyncio.run(scenario()) == ["item1", "item2"]
