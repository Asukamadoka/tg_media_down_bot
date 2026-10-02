"""M9.4: download priority: start order, weighted connection grants, soft pre-emption,
the minimum of one connection, the controls, the sentences, persistence."""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_wms_m2 import cli_world  # noqa: F401 - fixture
from test_wms_m83 import _outbound_plan, payload
from test_wms_m92 import CdnIO, chinese, until, world  # noqa: F401 - fixtures
from test_wms_m93 import start

from pikpak_wms.core.models import Plan
from pikpak_wms.nl.query import Prioritize, Remark
from pikpak_wms.nl.rules_parser import RulesTranslator
from pikpak_wms.nl.translator import Chain
from pikpak_wms.ops import fetch, outbound, plans, priority, taskreq
from pikpak_wms.ops.control import Control
from pikpak_wms.ops.runs import Runs


class Pool:
    """A connection pool and the order its waiters were granted in."""

    def __init__(self, capacity: int) -> None:
        self.pool = fetch.ConnectionPool(capacity)
        self.order: list[str] = []
        self.tasks: list[asyncio.Task] = []

    def wait(self, label: str, level: int = 0, order: tuple = (), first: bool = False):
        async def go():
            await self.pool.acquire(level, order, first)
            self.order.append(label)

        self.tasks.append(asyncio.create_task(go()))

    async def free(self, count: int) -> None:
        for _ in range(count):
            self.pool.release()
            await asyncio.sleep(0)
            await asyncio.sleep(0)


# ----------------------------------------------------------------- the pool


class TestPool:
    async def test_higher_priority_is_granted_first_then_older_plans_then_place(self):
        p = Pool(1)
        await p.pool.acquire()
        p.wait("normal-old", 0, ("2026-10-01", 0))
        p.wait("top-late-b", 2, ("2026-10-03", 5))
        p.wait("top-late-a", 2, ("2026-10-03", 1))
        p.wait("top-early", 2, ("2026-10-02", 9))
        await asyncio.sleep(0)
        await p.free(4)
        # The top class goes first, oldest plan first, then the plan's own order; the normal
        # task takes its (small) turn between them.
        tops = [x for x in p.order if x.startswith("top")]
        assert p.order[0] == "top-early" and tops == ["top-early", "top-late-a", "top-late-b"]

    async def test_grants_are_weighted_one_to_two_to_four(self):
        p = Pool(1)
        await p.pool.acquire()
        for n in range(8):
            p.wait(f"n{n}", 0, ("a", n))
            p.wait(f"h{n}", 1, ("a", n))
            p.wait(f"t{n}", 2, ("a", n))
        await asyncio.sleep(0)
        await p.free(14)
        first = p.order[:14]
        counts = {k: sum(1 for x in first if x[0] == k) for k in "nht"}
        assert counts == {"n": 2, "h": 4, "t": 8} or (
            counts["t"] >= 7 and 3 <= counts["h"] <= 5 and 1 <= counts["n"] <= 3), counts
        assert counts["t"] > counts["h"] > counts["n"]

    async def test_a_lower_priority_is_never_starved(self):
        p = Pool(1)
        await p.pool.acquire()
        p.wait("normal", 0, ("a", 0))
        for n in range(20):
            p.wait(f"t{n}", 2, ("a", n))
        await asyncio.sleep(0)
        await p.free(9)
        assert "normal" in p.order            # within about 5 grants, not after all twenty

    async def test_soft_pre_emption_only_when_a_connection_is_given_back(self):
        p = Pool(2)
        await p.pool.acquire()
        await p.pool.acquire()                # two normal streams, both mid-range
        p.wait("normal-next", 0, ("a", 1))
        p.wait("top", 2, ("b", 0))
        await asyncio.sleep(0)
        assert p.pool.used == 2 and p.order == []      # nothing is taken from an open stream
        await p.free(1)                       # one normal range ends
        assert p.order == ["top"] and p.pool.used == 2

    async def test_a_file_with_no_connection_is_served_before_the_weights(self):
        p = Pool(1)
        await p.pool.acquire()
        p.wait("top", 2, ("a", 0))
        p.wait("normal-with-none", 0, ("z", 9), first=True)
        await asyncio.sleep(0)
        await p.free(1)
        assert p.order == ["normal-with-none"]

    async def test_a_cancelled_waiter_leaves_no_trace(self):
        p = Pool(1)
        await p.pool.acquire()
        p.wait("a", 1)
        p.wait("b", 0)
        await asyncio.sleep(0)
        p.tasks[0].cancel()
        await asyncio.gather(p.tasks[0], return_exceptions=True)
        await p.free(1)
        assert p.order == ["b"]

    async def test_download_asks_with_its_priority_and_whether_it_holds_nothing(self, tmp_path):
        seen: list[tuple] = []

        class Spy(fetch.ConnectionPool):
            async def acquire(self, priority=0, order=(), first=False):
                seen.append((priority() if callable(priority) else priority, order, first))
                await super().acquire(priority, order, first)

        level = {"v": 1}
        io = CdnIO(payload(2000))

        class Url:
            url = "https://x/1"

        async def url_for():
            return Url.url

        await fetch.download(url_for, io, tmp_path / "f.part", connections=1, max_connections=1,
                             pool=Spy(4), priority=lambda: level["v"], order=("p", 3))
        assert seen and seen[0] == (1, ("p", 3), True)


# --------------------------------------------------------- start order, gate


class TestStartOrder:
    async def test_queued_tasks_start_by_priority_then_plan_age_then_place(
            self, world, tmp_path, monkeypatch):
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        _pid, _runs, run = await start(world, tmp_path, ("a.mkv", "b.mkv", "c.mkv", "d.mkv"),
                                       io, monkeypatch, 1)
        await until(lambda: len(io.begun) == 1)
        control = run.control
        control.set_priority(3, 2)                    # d: top
        control.set_priority(2, 1)                    # c: high
        assert [t.name for t in control.queued_order()] == ["d.mkv", "c.mkv", "b.mkv"]
        io.gate.set()
        await asyncio.wait_for(run.finished.wait(), 5)
        order = [t.name for t in sorted(control.tracks.values(), key=lambda t: t.started or 0)]
        assert order == ["a.mkv", "d.mkv", "c.mkv", "b.mkv"]

    async def test_a_priority_set_while_waiting_counts_at_the_next_free_slot(
            self, world, tmp_path, monkeypatch):
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        _pid, _runs, run = await start(world, tmp_path, ("a.mkv", "b.mkv", "c.mkv"), io,
                                       monkeypatch, 1)
        await until(lambda: len(io.begun) == 1)
        run.control.set_priority(2, 1)
        io.gate.set()
        await asyncio.wait_for(run.finished.wait(), 5)
        order = [t.name for t in sorted(run.control.tracks.values(), key=lambda t: t.started or 0)]
        assert order == ["a.mkv", "c.mkv", "b.mkv"]

    async def test_the_order_holds_across_running_plans(self, world, tmp_path, monkeypatch):
        monkeypatch.setenv("OUTBOUND_PARALLEL_FILES", "1")
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        world.drive.add("/Media/a1.mkv", size=2000)
        world.drive.add("/Media/a2.mkv", size=2000)
        world.drive.add("/Media/b1.mkv", size=2000)
        world.drive.add("/Media/b2.mkv", size=2000)
        await world.sync()
        world.ctx.config.outbound.local_dir = tmp_path / "media"
        world.ctx.config.outbound.downloader = "local"
        one = await plans.save(world.ctx, await outbound.plan_paths(
            world.ctx, ["/Media/a1.mkv", "/Media/a2.mkv"]))
        two = await plans.save(world.ctx, await outbound.plan_paths(
            world.ctx, ["/Media/b1.mkv", "/Media/b2.mkv"]))
        runs = Runs(world.ctx, asyncio.Lock())
        for pid in (one, two):
            await runs.start(pid, make_deliver=lambda progress, pid=pid: outbound.make_deliver(
                world.ctx, io=io, progress=progress, plan_id=pid))
        await until(lambda: len(io.begun) == 2)
        assert [(p, t.name) for p, t in runs.queue()] == [(one, "a2.mkv"), (two, "b2.mkv")]
        await runs.set_priority(two, 1, 2)                       # b2 on top
        assert [t.name for _p, t in runs.queue()] == ["b2.mkv", "a2.mkv"]
        io.gate.set()
        for pid in (one, two):
            await asyncio.wait_for(runs.get(pid).finished.wait(), 5)


# ------------------------------------------------------- controls and memory


class TestControls:
    async def test_the_button_cycles_normal_high_top_normal(self):
        assert [priority.next_level(n) for n in (0, 1, 2)] == [1, 2, 0]

    async def test_a_task_and_a_plan_keep_their_priority(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", "b.mkv", "c.mkv"))
        await priority.set_task(world.ctx, plan_id, 1, 2)
        row = await plans.get(world.ctx, plan_id)
        assert [priority.effective(row["plan"], a) for a in row["plan"].actions] == [0, 2, 0]
        await priority.set_plan(world.ctx, plan_id, 1)            # 整组优先 clears the overrides
        row = await plans.get(world.ctx, plan_id)
        assert row["plan"].priority == 1
        assert [priority.effective(row["plan"], a) for a in row["plan"].actions] == [1, 1, 1]
        # A task set back to normal beats a high plan.
        await priority.set_task(world.ctx, plan_id, 0, 0)
        row = await plans.get(world.ctx, plan_id)
        assert [priority.effective(row["plan"], a) for a in row["plan"].actions] == [0, 1, 1]

    async def test_a_new_run_starts_with_what_was_kept(self, world, tmp_path):
        """Persistence across a restart: a fresh Control reads it from the stored plan."""
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", "b.mkv"))
        await priority.set_plan(world.ctx, plan_id, 1)
        await priority.set_task(world.ctx, plan_id, 1, 2)
        io = CdnIO(payload(2000))
        control = Control(0)
        deliver = outbound.make_deliver(world.ctx, io=io, plan_id=plan_id,
                                        progress=_NoProgress(control))
        await plans.apply(world.ctx, plan_id, deliver=deliver, control=control)
        assert [control.tracks[i].priority for i in (0, 1)] == [1, 2]
        assert control.plan_priority == 1

    async def test_the_priority_survives_into_the_retry_plan(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv",))
        await priority.set_task(world.ctx, plan_id, 0, 2)
        row = await plans.get(world.ctx, plan_id)
        again = Plan(source="outbound", generated_at="x", actions=list(row["plan"].actions))
        assert priority.effective(again, again.actions[0]) == 2

    async def test_the_plan_form_remembers_its_priority(self):
        plan = Plan.from_dict(Plan(priority=2).to_dict())
        assert plan.priority == 2 and Plan.from_dict({}).priority == 0

    async def test_whole_plan_priority_reaches_every_task_of_a_run(
            self, world, tmp_path, monkeypatch):
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        plan_id, runs, run = await start(world, tmp_path, ("a.mkv", "b.mkv"), io, monkeypatch, 1)
        await until(lambda: len(io.begun) == 1)
        assert await runs.set_plan_priority(plan_id, 2) == 2
        assert [t.priority for t in run.control.tracks.values()] == [2, 2]
        row = await plans.get(world.ctx, plan_id)
        assert row["plan"].priority == 2
        io.gate.set()
        await asyncio.wait_for(run.finished.wait(), 5)

    async def test_numbers_are_the_places_in_the_plan_after_a_resume(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", "b.mkv", "c.mkv"))
        await world.ctx.store.update_plan(plan_id, status="partial", progress=1, result={})
        control = Control(0)
        io = CdnIO(payload(2000))
        deliver = outbound.make_deliver(world.ctx, io=io, plan_id=plan_id,
                                        progress=_NoProgress(control))
        await plans.apply(world.ctx, plan_id, deliver=deliver, control=control)
        assert [t.number for t in control.tracks.values()] == [2, 3]


class _NoProgress:
    def __init__(self, control: Control) -> None:
        self.control = control

    def __call__(self, name, received, size) -> None:
        pass

    def track(self, file_id):
        return self.control.track_for(file_id)


class TestRequests:
    async def test_the_command_line_asks_the_running_process(self, world, tmp_path, monkeypatch):
        monkeypatch.setattr(taskreq, "EVERY", 0.05)
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", "b.mkv"))
        control = Control(1)
        deliver = outbound.make_deliver(world.ctx, io=io, plan_id=plan_id,
                                        progress=_NoProgress(control))
        job = asyncio.create_task(plans.apply(world.ctx, plan_id, deliver=deliver,
                                              control=control))
        await until(lambda: len(io.begun) == 1)
        done, result = await taskreq.request(world.ctx, "priority", f"{plan_id}:2",
                                             level="top", wait=3)
        assert done and result == "ok" and control.tracks[1].priority == 2
        row = await plans.get(world.ctx, plan_id)                   # and kept in the plan
        assert row["plan"].actions[1].after["priority"] == 2
        done, result = await taskreq.request(world.ctx, "priority", f"{plan_id}:all",
                                             level="high", whole_plan=True, wait=3)
        assert done and result.startswith("ok")
        assert [t.priority for t in control.tracks.values()] == [1, 1]
        # A task this plan does not have is not taken by anyone (and expires).
        bad, _ = await taskreq.request(world.ctx, "priority", f"{plan_id}:9", level="high",
                                       wait=0.3)
        assert not bad
        io.gate.set()
        await asyncio.wait_for(job, 5)

    async def test_a_bad_level_is_refused_before_anything_is_posted(self, world):
        with pytest.raises(Exception, match="priority"):
            await taskreq.request(world.ctx, "priority", "5:1", level="urgent")
        with pytest.raises(Exception, match="priority"):
            priority.parse("urgent")
        assert priority.parse("TOP") == 2


# ---------------------------------------------------------------- sentences

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
TZ = ZoneInfo("Asia/Shanghai")


class TestSentences:
    @pytest.mark.parametrize(("text", "names", "level"), [
        ("先下 juvr00309", ["juvr00309"], "high"),
        ("先下载juvr00309", ["juvr00309"], "high"),
        ("优先下载 juvr00309", ["juvr00309"], "high"),
        ("请优先下载 juvr00309 和 savr01205", ["juvr00309", "savr01205"], "high"),
        ("juvr00309 置顶", ["juvr00309"], "top"),
        ("把 juvr00309 置顶", ["juvr00309"], "top"),
        ("置顶 juvr00309", ["juvr00309"], "top"),
        ("「4K688 juvr」置顶", ["4K688 juvr"], "top"),
    ])
    def test_the_phrases(self, text, names, level):
        assert RulesTranslator().parse(text, NOW, TZ) == Prioritize(names=names, level=level)

    @pytest.mark.parametrize("text", [
        "优先下载今天的视频", "先下载再整理", "下载 juvr00309", "视频置顶",
        "juvr00309 下过了 先下 x",
    ])
    def test_other_sentences_are_not_priorities(self, text):
        assert not isinstance(RulesTranslator().parse(text, NOW, TZ), Prioritize)

    async def test_a_model_is_never_asked_and_a_remark_is_still_a_remark(self):
        class Boom:
            name = "model"

            async def translate(self, *a):
                raise AssertionError("a priority must not reach a model")

        chain = Chain([Boom()])
        got = await chain.translate("先下 juvr00309", NOW, TZ)
        assert got == Prioritize(names=["juvr00309"], level="high")
        assert await chain.translate("juvr00309 下过了", NOW, TZ) == Remark(
            names=["juvr00309"], downloaded=True)


# ------------------------------------------------------------ command line


class TestCommandLine:
    def test_plan_priority_and_task_priority_and_plan_still_shows(self, cli_world, monkeypatch):
        from test_wms_m2 import invoke

        from pikpak_wms.cli import main as cli

        monkeypatch.setattr(cli, "TASK_WAIT", 0.1)
        assert invoke("stocktake").exit_code == 0
        assert invoke("organize").exit_code == 0
        shown = invoke("plan", "1")                      # the old form still works
        assert shown.exit_code == 0 and "organize" in shown.output
        done = invoke("plan", "priority", "1", "high")
        assert done.exit_code == 0 and "high" in done.output
        saved = invoke("task", "priority", "1:2", "top")  # nothing runs it: kept in the plan
        assert saved.exit_code == 0 and "top" in saved.output, saved.output
        bad = invoke("task", "priority", "1:2", "urgent")
        assert bad.exit_code != 0

        import asyncio

        from pikpak_wms.ops.context import open_context

        async def read():
            ctx = await open_context(cli.state.config, cli._provider(cli.state.config))  # noqa: SLF001
            try:
                return (await plans.get(ctx, 1))["plan"]
            finally:
                await ctx.close()

        plan = asyncio.run(read())
        assert plan.priority == 1 and plan.actions[1].after["priority"] == 2
