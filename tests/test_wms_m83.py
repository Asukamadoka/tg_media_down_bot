"""WMS M8.3: background runs (G), the library layout (H), and the rest of the brief."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from wms_fakes import FakeDrive, provider_for

from pikpak_wms.config import Config
from pikpak_wms.core.client import WmsClient
from pikpak_wms.core.errors import WmsError
from pikpak_wms.core.ratelimit import TokenBucket
from pikpak_wms.i18n import set_language
from pikpak_wms.ops import fetch, library, outbound, plans
from pikpak_wms.ops.context import Context
from pikpak_wms.ops.runs import Runs
from pikpak_wms.ops.stocktake import stocktake
from pikpak_wms.store.db import Store

SH = ZoneInfo("Asia/Shanghai")


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language(None)


async def _no_sleep(_seconds: float) -> None:
    return None


class World:
    def __init__(self, drive: FakeDrive, ctx: Context) -> None:
        self.drive, self.ctx = drive, ctx

    async def sync(self) -> None:
        await stocktake(self.ctx.client, self.ctx.store, full=True)


@pytest.fixture
async def world(tmp_path):
    drive = FakeDrive()
    config = Config()
    async with Store(tmp_path / "wms.sqlite3") as store:
        client = WmsClient(provider_for(drive), limiter=TokenBucket(1e9, 1_000_000),
                           sleep=_no_sleep)
        yield World(drive, Context(config=config, client=client, store=store))


# ------------------------------------------------------------- G7: ranges


class FakeIO:
    """A server holding ``data``: serves ranges, can expire links, can drop a connection."""

    def __init__(self, data: bytes, *, ranges: bool = True, chunk: int = 1000,
                 any_url: bool = False) -> None:
        self.data, self.ranges, self.chunk = data, ranges, chunk
        self.any_url = any_url  # the fake drive's own links, whatever they are
        self.valid = "link-1"
        self.requests: list[tuple[int, int | None]] = []
        self.expire_after: int | None = None  # chunks served before the link dies
        self.fail_after: int | None = None
        self._served = 0

    async def probe(self, url):
        if not self.any_url and url != self.valid:
            raise fetch.LinkExpired("403")
        return len(self.data) if self.ranges else None

    async def stream(self, url, start, end):
        self.requests.append((start, end))
        end = len(self.data) if end is None else end
        for at in range(start, end, self.chunk):
            if not self.any_url and url != self.valid:
                raise fetch.LinkExpired("403")
            if self.expire_after is not None and self._served >= self.expire_after:
                self.valid = "link-2"
                self.expire_after = None
                raise fetch.LinkExpired("403")
            if self.fail_after is not None and self._served >= self.fail_after:
                raise ConnectionResetError("boom")
            self._served += 1
            yield self.data[at:min(at + self.chunk, end)]


def payload(size: int) -> bytes:
    return bytes((i * 7 + i // 251) % 256 for i in range(size))


class Links:
    """``url_for`` that hands out whatever the server currently accepts."""

    def __init__(self, io: FakeIO) -> None:
        self.io, self.calls = io, 0

    async def __call__(self) -> str:
        self.calls += 1
        return self.io.valid


class TestRangedDownload:
    async def test_connections_split_the_file_and_the_pieces_fit_together(self, tmp_path):
        data = payload(20 * 1024 * 1024)
        io = FakeIO(data, chunk=1024 * 1024)
        part = tmp_path / "a.mkv.part"
        seen = []
        size = await fetch.download(Links(io), io, part, connections=4,
                                    progress=lambda got, total: seen.append((got, total)))
        assert size == len(data) and part.read_bytes() == data
        assert len({start for start, _ in io.requests}) == 4  # four ranges at once
        assert seen[-1] == (len(data), len(data))
        assert not fetch.state_path(part).exists()

    async def test_a_server_without_ranges_is_read_in_one_stream(self, tmp_path):
        data = payload(50_000)
        io = FakeIO(data, ranges=False)
        part = tmp_path / "b.part"
        assert await fetch.download(Links(io), io, part, connections=8) == len(data)
        assert part.read_bytes() == data and io.requests == [(0, None)]

    async def test_a_small_file_does_not_get_eight_connections(self, tmp_path):
        data = payload(100_000)
        io = FakeIO(data)
        await fetch.download(Links(io), io, tmp_path / "c.part", connections=8)
        assert len(io.requests) == 1

    async def test_an_expired_link_is_fetched_again_and_the_range_carries_on(self, tmp_path):
        data = payload(8 * 1024 * 1024)
        io = FakeIO(data, chunk=256 * 1024)
        io.expire_after = 6
        links = Links(io)
        part = tmp_path / "d.part"
        assert await fetch.download(links, io, part, connections=2) == len(data)
        assert part.read_bytes() == data
        assert links.calls >= 2  # the first link, and a new one

    async def test_a_link_that_keeps_expiring_gives_up(self, tmp_path):
        io = FakeIO(payload(10_000))

        class Never:
            async def __call__(self):
                return "link-1"

        async def probe(url):
            return 10_000

        async def stream(url, start, end):
            raise fetch.LinkExpired("403")
            yield b""

        io.probe, io.stream = probe, stream
        with pytest.raises(fetch.LinkExpired):
            await fetch.download(Never(), io, tmp_path / "e.part", connections=1)

    async def test_stopping_keeps_the_part_and_the_next_run_resumes(self, tmp_path):
        data = payload(8 * 1024 * 1024)
        io = FakeIO(data, chunk=256 * 1024)
        io.fail_after = 9
        part = tmp_path / "f.part"
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)

        with pytest.raises(ConnectionResetError):
            await fetch.download(Links(io), io, part, connections=2, sleep=sleep)
        assert part.exists() and fetch.state_path(part).exists()
        saved = json.loads(fetch.state_path(part).read_text())["ranges"]
        done = sum(r[2] for r in saved)
        assert 0 < done < len(data)

        healthy = FakeIO(data, chunk=256 * 1024)
        assert await fetch.download(Links(healthy), healthy, part, connections=2) == len(data)
        assert part.read_bytes() == data
        fetched = sum((end or len(data)) - start for start, end in healthy.requests)
        assert fetched == len(data) - done  # only what was missing

    async def test_cancelling_leaves_a_resumable_part(self, tmp_path):
        data = payload(4 * 1024 * 1024)
        io = FakeIO(data, chunk=64 * 1024)
        gate = asyncio.Event()
        original = io.stream

        async def slow(url, start, end):
            async for piece in original(url, start, end):
                yield piece
                if io._served > 10:  # noqa: SLF001
                    gate.set()
                    await asyncio.sleep(10)

        io.stream = slow
        part = tmp_path / "g.part"
        task = asyncio.create_task(fetch.download(Links(io), io, part, connections=2))
        await asyncio.wait_for(gate.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fetch.state_path(part).exists()
        again = FakeIO(data, chunk=64 * 1024)
        await fetch.download(Links(again), again, part, connections=2)
        assert part.read_bytes() == data

    async def test_an_old_single_stream_part_counts_as_a_finished_prefix(self, tmp_path):
        data = payload(6 * 1024 * 1024)
        part = tmp_path / "h.part"
        part.write_bytes(data[:2 * 1024 * 1024])  # what the old code left
        io = FakeIO(data, chunk=256 * 1024)
        await fetch.download(Links(io), io, part, connections=2)
        assert part.read_bytes() == data
        assert min(start for start, _ in io.requests) == 2 * 1024 * 1024

    def test_gcid_matches_the_definition(self, tmp_path):
        data = payload(700_000)
        path = tmp_path / "x"
        path.write_bytes(data)
        block = 0x40000
        outer = hashlib.sha1()
        for at in range(0, len(data), block):
            outer.update(hashlib.sha1(data[at:at + block]).digest())
        assert fetch.gcid(path) == outer.hexdigest().upper()


class TestOutboundLocalOverRanges:
    async def _one(self, world, tmp_path, size=3000):
        world.drive.add("/Media/a.mkv", size=size)
        await world.sync()
        world.ctx.config.outbound.local_dir = tmp_path / "media"
        return await outbound.plan_paths(world.ctx, ["/Media/a.mkv"], to="PikPak")

    async def test_the_file_arrives_and_progress_is_reported(self, world, tmp_path):
        plan = await self._one(world, tmp_path)
        data = payload(3000)
        seen = []
        deliver = outbound.make_deliver(
            world.ctx, downloader="local", io=FakeIO(data, any_url=True),
            progress=lambda name, got, total: seen.append((name, got, total)))
        await plans.save_and_apply(world.ctx, plan, deliver=deliver)
        assert (tmp_path / "media" / "PikPak" / "a.mkv").read_bytes() == data
        assert seen[0] == ("a.mkv", 0, 3000) and seen[-1][1] == 3000

    async def test_a_wrong_size_is_refused_and_removed(self, world, tmp_path):
        plan = await self._one(world, tmp_path, size=5000)
        io = FakeIO(payload(3000), any_url=True)
        deliver = outbound.make_deliver(world.ctx, downloader="local", io=io)
        _, report = await plans.save_and_apply(world.ctx, plan, deliver=deliver)
        assert "3000 of 5000" in report.failed[0]["error"]
        assert not list((tmp_path / "media" / "PikPak").glob("a.mkv*"))

    async def test_a_network_error_fails_that_file_and_keeps_the_part(
            self, world, tmp_path, monkeypatch):
        monkeypatch.setenv("OUTBOUND_RETRY_MINUTES", "0")  # no patience: fail at the first error
        plan = await self._one(world, tmp_path)
        io = FakeIO(payload(3000), chunk=500, any_url=True)
        io.fail_after = 2

        async def sleep(_s):
            return None

        deliver = outbound.make_deliver(world.ctx, downloader="local", io=io)
        import pikpak_wms.ops.fetch as module

        original = module.asyncio.sleep
        module.asyncio.sleep = sleep  # no waiting between retries
        try:
            _, report = await plans.save_and_apply(world.ctx, plan, deliver=deliver)
        finally:
            module.asyncio.sleep = original
        assert report.failed and "download failed" in report.failed[0]["error"]
        assert (tmp_path / "media" / "PikPak" / "a.mkv.part").exists()


# --------------------------------------------------------- G: background runs


class SlowIO(FakeIO):
    """Holds each stream open until released, so a run can be watched and stopped."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data, chunk=500, any_url=True)
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def stream(self, url, start, end):
        async for piece in super().stream(url, start, end):
            self.started.set()
            await self.release.wait()
            yield piece


async def _outbound_plan(world, tmp_path, names=("a.mkv",), size=2000):
    for name in names:
        world.drive.add(f"/Media/{name}", size=size)
    await world.sync()
    world.ctx.config.outbound.local_dir = tmp_path / "media"
    world.ctx.config.outbound.downloader = "local"
    plan = await outbound.plan_paths(world.ctx, [f"/Media/{n}" for n in names])
    return await plans.save(world.ctx, plan)


class TestRuns:
    async def test_starting_returns_at_once_and_the_result_follows(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path)
        io = SlowIO(payload(2000))
        runs = Runs(world.ctx, asyncio.Lock())
        run = await runs.start(plan_id, make_deliver=lambda progress: outbound.make_deliver(
            world.ctx, io=io, progress=progress))
        assert run.active and await runs.label(plan_id) == "running"
        await asyncio.wait_for(io.started.wait(), 5)
        assert run.file == "a.mkv" and run.size == 2000
        io.release.set()
        await asyncio.wait_for(run.finished.wait(), 5)
        assert run.report.applied == 1 and not run.error
        assert await runs.label(plan_id) == ""
        assert (await plans.get(world.ctx, plan_id))["status"] == plans.APPLIED

    async def test_a_second_start_is_refused_while_running(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path)
        io = SlowIO(payload(2000))
        runs = Runs(world.ctx, asyncio.Lock())

        def make(progress):
            return outbound.make_deliver(world.ctx, io=io, progress=progress)

        await runs.start(plan_id, make_deliver=make)
        with pytest.raises(WmsError) as refused:
            await runs.start(plan_id, make_deliver=make)
        assert refused.value.key == "plan.running"
        io.release.set()
        await runs.stop_all()

    async def test_stop_keeps_what_was_done_and_the_place(self, world, tmp_path, monkeypatch):
        monkeypatch.setenv("OUTBOUND_PARALLEL_FILES", "1")  # one after another
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", "b.mkv"))
        hold = asyncio.Event()
        reached = asyncio.Event()
        calls = []

        async def deliver(node, to, via=None):
            calls.append(node.name)
            if len(calls) == 2 and not hold.is_set():
                reached.set()
                await hold.wait()  # the second file is "downloading"
            return {"downloader": "local"}

        runs = Runs(world.ctx, asyncio.Lock())
        run = await runs.start(plan_id, deliver=deliver)
        await asyncio.wait_for(reached.wait(), 5)
        stopped = await runs.stop(plan_id)
        row = await plans.get(world.ctx, plan_id)
        assert stopped is run and run.stopped
        assert row["status"] == plans.PENDING and row["progress"] == 1
        assert await runs.label(plan_id) == "stopped"
        assert len(await world.ctx.store.audit_entries(limit=10)) == 1  # the first is audited
        # Continue: only the second is left to do.
        hold.set()
        again = await runs.start(plan_id, deliver=deliver)
        await asyncio.wait_for(again.finished.wait(), 5)
        assert again.report.applied == 1
        assert (await plans.get(world.ctx, plan_id))["status"] == plans.APPLIED
        assert await runs.label(plan_id) == ""

    async def test_a_restart_turns_running_marks_into_interrupted(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", "b.mkv"))
        io = SlowIO(payload(2000))
        runs = Runs(world.ctx, asyncio.Lock())
        run = await runs.start(plan_id, make_deliver=lambda progress: outbound.make_deliver(
            world.ctx, io=io, progress=progress))
        await asyncio.wait_for(io.started.wait(), 5)
        await runs.stop_all()  # the process is going away: the mark stays
        assert run.stopped
        fresh = Runs(world.ctx, asyncio.Lock())  # the next process
        found = await fresh.recover()
        assert found == [{"id": plan_id, "done": 0, "total": 2}]
        assert await fresh.label(plan_id) == "interrupted"
        assert await fresh.recover() == []  # told once
        # It can be continued.
        io.release.set()
        again = await fresh.start(plan_id, make_deliver=lambda progress: outbound.make_deliver(
            world.ctx, io=io, progress=progress))
        await asyncio.wait_for(again.finished.wait(), 5)
        assert again.report.applied == 2
        assert await fresh.label(plan_id) == ""

    async def test_an_error_leaves_the_plan_open_for_a_retry(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path)
        runs = Runs(world.ctx, asyncio.Lock())

        async def boom(node, to, via=None):
            raise RuntimeError("disk on fire")

        run = await runs.start(plan_id, deliver=boom)
        await asyncio.wait_for(run.finished.wait(), 5)
        assert (await plans.get(world.ctx, plan_id))["status"] in plans.OPEN
        assert run.report is None and "disk on fire" in run.error

    async def test_progress_is_saved_as_the_run_goes(self, world, tmp_path, monkeypatch):
        monkeypatch.setenv("OUTBOUND_PARALLEL_FILES", "1")  # one after another
        names = tuple(f"{n}.mkv" for n in "abc")
        plan_id = await _outbound_plan(world, tmp_path, names=names)
        gate = asyncio.Event()
        count = {"n": 0}

        async def deliver(node, to, via=None):
            count["n"] += 1
            if count["n"] == 3:
                gate.set()
                await asyncio.sleep(10)
            return {"downloader": "local"}

        runs = Runs(world.ctx, asyncio.Lock())
        run = await runs.start(plan_id, deliver=deliver)
        await asyncio.wait_for(gate.wait(), 5)
        assert (await plans.get(world.ctx, plan_id))["progress"] == 2
        await runs.stop(plan_id)
        assert run.stopped


# ----------------------------------------------------------- H: the library


class TestLibraryLayout:
    @pytest.mark.parametrize(("when", "expected"), [
        (datetime(2026, 9, 30, 12, tzinfo=SH), "资源/整理/2026/2026.9/2026.9.30"),
        (datetime(2026, 10, 1, 0, 5, tzinfo=SH), "资源/整理/2026/2026.10/2026.10.1"),
        (datetime(2026, 12, 31, 23, 59, tzinfo=SH), "资源/整理/2026/2026.12/2026.12.31"),
        (datetime(2027, 1, 1, 0, 0, tzinfo=SH), "资源/整理/2027/2027.1/2027.1.1"),
    ])
    def test_the_template_has_no_zero_padding(self, when, expected):
        assert library.expand_layout(library.DEFAULT_LAYOUT, when) == expected

    @pytest.mark.parametrize("raw", [
        "资源库/电影/日剧", "/电影/日剧", "电影/日剧", "/library/电影/日剧", "/资源库/电影/日剧/"])
    def test_a_named_place_is_always_inside_the_library(self, raw):
        assert library.resolve_user_path(raw, library_dir="/library") == "电影/日剧"

    @pytest.mark.parametrize("raw", ["../x", "电影/../../x", "/etc/passwd", "/volume9/x",
                                     "/media/PikPak", "C:/x", "~/x", "/tmp"])
    def test_a_place_outside_is_refused(self, raw):
        with pytest.raises(WmsError) as refused:
            library.resolve_user_path(raw, library_dir="/library")
        assert refused.value.key == "library.outside"

    def test_the_library_root_is_a_valid_place(self):
        assert library.resolve_user_path("资源库", library_dir="/library") == ""

    def test_missing_folders_are_listed_outermost_first(self, tmp_path):
        (tmp_path / "电影").mkdir()
        assert library.missing_dirs(tmp_path, "电影/日剧/2026") == ["电影/日剧", "电影/日剧/2026"]


@pytest.fixture
async def library_world(tmp_path):
    drive = FakeDrive()
    config = Config()
    config.outbound.library_dir = tmp_path / "lib"
    config.outbound.downloader = "local"
    (tmp_path / "lib").mkdir()
    async with Store(tmp_path / "wms.sqlite3") as store:
        client = WmsClient(provider_for(drive), limiter=TokenBucket(1e9, 1_000_000),
                           sleep=_no_sleep)
        yield World(drive, Context(config=config, client=client, store=store))


class TestDefaultDestination:
    async def _plan(self, w, to=""):
        w.drive.add("/Media/a.mkv", size=3)
        await w.sync()
        return await outbound.plan_paths(w.ctx, ["/Media/a.mkv"], to=to)

    async def test_the_plan_names_the_library_folder_not_the_container_path(self, library_world):
        plan = await self._plan(library_world)
        (action,) = plan.actions
        line = action.describe()
        assert "资源库/资源/整理/" in line and "/library" not in line and "/media" not in line

    async def test_a_named_place_is_shown_and_a_new_folder_is_announced(self, library_world):
        plan = await self._plan(library_world, to="/电影/日剧")
        assert plan.actions[0].describe().endswith("资源库/电影/日剧")
        assert any("资源库/电影/日剧" in n for n in plan.note_lines())

    async def test_a_place_outside_the_library_is_refused_when_planning(self, library_world):
        with pytest.raises(WmsError) as refused:
            await self._plan(library_world, to="../../etc")
        assert refused.value.key == "library.outside"

    async def test_the_day_is_the_day_the_download_runs(self, library_world, tmp_path):
        w = library_world
        plan = await self._plan(w)
        plan_id = await plans.save(w.ctx, plan)
        # Planned at 23:59 on 30 September; run at 00:01 on 1 October (Shanghai).
        clock = lambda: datetime(2026, 9, 30, 16, 1, tzinfo=UTC)  # noqa: E731 - 00:01 +08:00
        io = FakeIO(payload(3), any_url=True)
        deliver = outbound.make_deliver(w.ctx, io=io, clock=clock)
        await plans.apply(w.ctx, plan_id, deliver=deliver)
        folder = tmp_path / "lib" / "资源" / "整理" / "2026" / "2026.10" / "2026.10.1"
        assert (folder / "a.mkv").exists()
        entry = (await w.ctx.store.audit_entries())[0]
        assert entry["after"]["library_path"] == "资源库/资源/整理/2026/2026.10/2026.10.1"

    async def test_a_named_place_receives_the_file(self, library_world, tmp_path):
        w = library_world
        plan = await self._plan(w, to="资源库/电影/日剧")
        deliver = outbound.make_deliver(w.ctx, io=FakeIO(payload(3), any_url=True))
        await plans.save_and_apply(w.ctx, plan, deliver=deliver)
        assert (tmp_path / "lib" / "电影" / "日剧" / "a.mkv").exists()

    async def test_no_write_permission_is_said_plainly(self, library_world, tmp_path):
        w = library_world
        plan = await self._plan(w)
        (tmp_path / "lib").chmod(0o555)
        try:
            deliver = outbound.make_deliver(w.ctx, io=FakeIO(payload(3), any_url=True))
            _, report = await plans.save_and_apply(w.ctx, plan, deliver=deliver)
        finally:
            (tmp_path / "lib").chmod(0o755)
        if report.applied:  # running as root: nothing to refuse
            pytest.skip("the directory is writable anyway")
        assert "No permission to write 资源库/资源" in report.failed[0]["error"]

    async def test_without_a_library_the_old_layout_stays(self, world, tmp_path):
        world.drive.add("/Media/a.mkv", size=3)
        await world.sync()
        world.ctx.config.outbound.local_dir = tmp_path / "m"
        plan = await outbound.plan_paths(world.ctx, ["/Media/a.mkv"], to="PikPak")
        assert plan.actions[0].describe().endswith("PikPak")
        assert "shown" not in plan.actions[0].after


def test_the_default_day_follows_the_configured_zone():
    config = Config()
    late = datetime(2026, 9, 30, 20, 0, tzinfo=UTC)  # 1 October in Shanghai
    assert outbound.library.default_dir(config, late) == "资源/整理/2026/2026.10/2026.10.1"
    earlier = late - timedelta(hours=5)
    assert library.default_dir(config, earlier) == "资源/整理/2026/2026.9/2026.9.30"


_ = Path
