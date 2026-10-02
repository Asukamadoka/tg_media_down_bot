"""WMS M9.2: the download log, skipping what is on the NAS, files in parallel, one file
cancelled, 503s that are not fatal, and the sentences about files already downloaded."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_wms_m83 import FakeIO, World, _no_sleep, _outbound_plan, payload
from wms_fakes import FakeDrive, provider_for

from pikpak_wms.config import Config
from pikpak_wms.core.client import WmsClient
from pikpak_wms.core.models import Action, ActionType, Plan
from pikpak_wms.core.ratelimit import TokenBucket
from pikpak_wms.i18n import set_language
from pikpak_wms.nl.guard import guard
from pikpak_wms.nl.query import Clarification, Filters, Query, Remark, from_wire, wire_schema
from pikpak_wms.nl.rules_parser import RulesTranslator
from pikpak_wms.nl.translator import Chain
from pikpak_wms.ops import downloads, fetch, nl, outbound, plans
from pikpak_wms.ops.context import Context
from pikpak_wms.ops.runs import Runs
from pikpak_wms.store.db import AUDIT_BACKFILLED, Store

SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=SH)
ABCD = "example.com@abcd00123.part1.mp4"


@pytest.fixture(autouse=True)
def chinese():
    set_language("zh")
    yield
    set_language(None)


async def until(condition, seconds=5.0):
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.005)


@pytest.fixture
async def world(tmp_path):
    drive = FakeDrive()
    async with Store(tmp_path / "wms.sqlite3") as store:
        client = WmsClient(provider_for(drive), limiter=TokenBucket(1e9, 1_000_000),
                           sleep=_no_sleep)
        yield World(drive, Context(config=Config(), client=client, store=store))


@pytest.fixture
async def lib(tmp_path):
    """A library mounted, downloads local: files go to 资源/整理/年/年.月/年.月.日."""
    drive = FakeDrive()
    config = Config()
    config.outbound.library_dir = tmp_path / "lib"
    config.outbound.downloader = "local"
    (tmp_path / "lib").mkdir()
    async with Store(tmp_path / "wms.sqlite3") as store:
        client = WmsClient(provider_for(drive), limiter=TokenBucket(1e9, 1_000_000),
                           sleep=_no_sleep)
        yield World(drive, Context(config=config, client=client, store=store))


def folder(tmp_path, day="2026.10.2"):
    year, month = day.split(".")[0], ".".join(day.split(".")[:2])
    return tmp_path / "lib" / "资源" / "整理" / year / month / day


def on(year, month, day):
    return lambda: datetime(year, month, day, 12, 0, tzinfo=SH)


class CdnIO(FakeIO):
    """Every file the same bytes, told apart by URL: hold a file, count open streams,
    answer a stream with an error."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data, any_url=True, chunk=500)
        self.gate: asyncio.Event | None = None
        self.hold_after = 1
        self.begun: dict[str, asyncio.Event] = {}
        self.open = self.peak = 0
        self.failures: dict[str, list[Exception]] = {}

    def started(self, file_id: str) -> bool:
        return any(url.endswith(file_id) for url in self.begun)

    async def stream(self, url, start, end):
        self.open += 1
        self.peak = max(self.peak, self.open)
        try:
            if self.failures.get(url):
                raise self.failures[url].pop(0)
            sent = 0
            async for piece in super().stream(url, start, end):
                yield piece
                sent += 1
                self.begun.setdefault(url, asyncio.Event()).set()
                if self.gate is not None and sent >= self.hold_after:
                    await self.gate.wait()
        finally:
            self.open -= 1


# ================================================================ A. the log


class TestBackfill:
    async def test_applied_outbounds_in_the_audit_become_done_rows_once(self, tmp_path):
        path = tmp_path / "w.sqlite3"
        async with Store(path) as store:
            await store.record(Action(
                ActionType.OUTBOUND, "f1",
                before={"name": "a.mkv", "size": 100, "path": "/M/a.mkv", "hash": ""},
                after={"to": "", "downloader": "local", "path": "/library/资源/整理/a.mkv",
                       "fetch": {"avg_mib_s": 3.3, "links": "web", "peak_connections": 8}}),
                dry_run=False, plan_id=60)
            await store.record(Action(  # handed to aria2: nothing landed here
                ActionType.OUTBOUND, "f2", before={"name": "b.mkv", "size": 5},
                after={"downloader": "aria2", "gid": "1"}), dry_run=False, plan_id=60)
            await store.record(Action(  # a dry run changed nothing
                ActionType.OUTBOUND, "f3", before={"name": "c.mkv", "size": 5},
                after={"path": "/x/c.mkv"}), dry_run=True)
        # The database as the version before M9.2 left it: no rows, no flag.
        with sqlite3.connect(path) as raw:
            raw.execute("DELETE FROM downloads")
            raw.execute("DELETE FROM meta WHERE key = ?", (AUDIT_BACKFILLED,))
        for _ in range(2):  # the second open must add nothing
            async with Store(path) as store:
                (row,) = await store.downloads()
                assert (row["file_id"], row["name"], row["size"], row["status"]) == (
                    "f1", "a.mkv", 100, "done")
                assert row["source"] == "backfill" and row["plan_id"] == 60
                assert row["dest_path"] == "/library/资源/整理/a.mkv" and row["avg_mib_s"] == 3.3

    async def test_the_library_is_scanned_by_name_and_size(self, lib, tmp_path):
        lib.drive.add("/Media/a.mkv", size=2000)
        lib.drive.add("/Media/b.mkv", size=3000)
        lib.drive.add("/Media/c.mkv", size=10)
        # Empty index: nothing to match yet, so the first scan waits.
        assert await downloads.scan_once(lib.ctx) == 0
        await lib.sync()
        where = folder(tmp_path)
        where.mkdir(parents=True)
        (where / "a.mkv").write_bytes(payload(2000))          # in the index, same size
        (where / "b.mkv").write_bytes(payload(10))            # the name is known, not the size
        (where / "unknown.mkv").write_bytes(payload(7))       # not in the index at all
        (where / "c.mkv.part").write_bytes(payload(5))        # unfinished: never counted
        assert await downloads.scan_once(lib.ctx) == 1
        (row,) = await lib.ctx.store.downloads()
        assert row["name"] == "a.mkv" and row["source"] == "scan" and row["status"] == "done"
        assert row["file_id"] == lib.drive.id_at("/Media/a.mkv")
        assert row["dest_path"] == str(where / "a.mkv")
        assert await downloads.scan_library(lib.ctx) == 0       # again: nothing new
        assert await downloads.scan_once(lib.ctx) == 0          # and the flag is set


class TestRows:
    async def test_every_attempt_is_a_row(self, world, tmp_path):
        names = ("done.mkv", "fail.mkv", "there.mkv")
        plan_id = await _outbound_plan(world, tmp_path, names=names, size=2000)
        media = tmp_path / "media"
        media.mkdir()
        (media / "there.mkv").write_bytes(payload(2000))
        io = CdnIO(payload(2000))
        io.failures[f"https://download.example/{world.drive.id_at('/Media/fail.mkv')}"] = [
            ValueError("bad file")] * 99
        deliver = outbound.make_deliver(world.ctx, io=io, plan_id=plan_id)
        report = await plans.apply(world.ctx, plan_id, deliver=deliver)
        assert (report.applied, report.skipped.get("exists"), len(report.failed)) == (1, 1, 1)
        rows = {r["name"]: r for r in await world.ctx.store.downloads()}
        assert rows["done.mkv"]["status"] == "done" and rows["done.mkv"]["plan_id"] == plan_id
        assert rows["done.mkv"]["dest_path"] == str(media / "done.mkv")
        assert rows["done.mkv"]["source"] == "plan" and rows["done.mkv"]["size"] == 2000
        assert rows["fail.mkv"]["status"] == "failed" and "bad file" in rows["fail.mkv"]["reason"]
        assert rows["there.mkv"]["status"] == "skipped_exists"

    async def test_a_rerun_over_a_skipped_file_does_not_log_it_twice(self, world, tmp_path):
        await _outbound_plan(world, tmp_path, names=("a.mkv",), size=2000)
        media = tmp_path / "media"
        media.mkdir()
        (media / "a.mkv").write_bytes(payload(2000))
        deliver = outbound.make_deliver(world.ctx, io=CdnIO(payload(2000)))
        node = await world.ctx.store.node_at("/Media/a.mkv")
        for _ in range(3):
            await deliver(node, "", "local")
        assert len(await world.ctx.store.downloads()) == 1

    async def test_the_listing_filters(self, world):
        store = world.ctx.store
        await store.add_download(name="x.mkv", status="done",
                                 finished_at="2026-10-01T10:00:00+00:00")
        await store.add_download(name="y.mkv", status="failed",
                                 finished_at="2026-10-02T10:00:00+00:00")
        assert [r["name"] for r in await store.downloads()] == ["y.mkv", "x.mkv"]
        assert [r["name"] for r in await store.downloads(status=["failed"])] == ["y.mkv"]
        assert [r["name"] for r in await store.downloads(name="X.MK")] == ["x.mkv"]
        assert [r["name"] for r in await store.downloads(since="2026-10-02T00:00:00+00:00")] == [
            "y.mkv"]

    async def test_marking_remembers_files_and_unknown_names(self, world):
        world.drive.add(f"/Media/{ABCD}", size=500)
        world.drive.add("/Media/other.mp4", size=500)
        await world.sync()
        marked = await downloads.mark(world.ctx, ["abcd00123", "gone-name"], user_id=7)
        assert [n.name for n in marked] == [ABCD]
        rows = {r["name"]: r for r in await world.ctx.store.downloads()}
        assert rows[ABCD]["status"] == "marked" and rows[ABCD]["file_id"]
        assert rows[ABCD]["user_id"] == 7 and rows["gone-name"]["file_id"] == ""
        await downloads.mark(world.ctx, ["ABCD00123", "gone-name"])       # again: no new rows
        assert len(await world.ctx.store.downloads()) == 2
        node = await world.ctx.store.node_at("/Media/other.mp4")
        assert not await downloads.is_known(world.ctx, node)
        world.drive.add("/Media/Gone-Name-1.mp4", size=5)
        await world.sync()
        assert await downloads.is_known(world.ctx, await world.ctx.store.node_at(
            "/Media/Gone-Name-1.mp4"))                                      # by the fragment


# ========================================================= B. skip what is there


class TestSkipping:
    async def test_the_same_size_at_the_destination_is_skipped(self, lib, tmp_path):
        lib.drive.add("/Media/a.mkv", size=2000)
        await lib.sync()
        node = await lib.ctx.store.node_at("/Media/a.mkv")
        where = folder(tmp_path)
        where.mkdir(parents=True)
        (where / "a.mkv").write_bytes(b"x" * 2000)
        io = CdnIO(payload(2000))
        deliver = outbound.make_deliver(lib.ctx, io=io, clock=on(2026, 10, 2))
        result = await deliver(node, "", "local")
        assert result["skipped"] == "exists" and io.requests == []
        assert (where / "a.mkv").read_bytes() == b"x" * 2000        # never touched

    async def test_a_different_content_is_never_overwritten(self, lib, tmp_path, monkeypatch):
        monkeypatch.setenv("OUTBOUND_VERIFY", "hash")
        data = payload(2000)
        digest = fetch.gcid(_write(tmp_path / "ref", data), 2000)
        lib.drive.add("/Media/a.mkv", size=2000, hash=digest)
        await lib.sync()
        node = await lib.ctx.store.node_at("/Media/a.mkv")
        where = folder(tmp_path)
        where.mkdir(parents=True)
        (where / "a.mkv").write_bytes(b"y" * 2000)                  # same size, other bytes
        deliver = outbound.make_deliver(lib.ctx, io=CdnIO(data), clock=on(2026, 10, 2))
        result = await deliver(node, "", "local")
        assert result["path"] == str(where / "a (2).mkv") and "skipped" not in result
        assert (where / "a (2).mkv").read_bytes() == data
        assert (where / "a.mkv").read_bytes() == b"y" * 2000
        # The same bytes under the same name: skipped, as with size alone.
        (where / "a (2).mkv").unlink()
        (where / "a.mkv").write_bytes(data)
        assert (await deliver(node, "", "local"))["skipped"] == "exists"

    async def test_another_size_under_the_same_name_gets_a_new_name(self, lib, tmp_path):
        lib.drive.add("/Media/a.mkv", size=2000)
        await lib.sync()
        node = await lib.ctx.store.node_at("/Media/a.mkv")
        where = folder(tmp_path)
        where.mkdir(parents=True)
        (where / "a.mkv").write_bytes(b"short")
        deliver = outbound.make_deliver(lib.ctx, io=CdnIO(payload(2000)), clock=on(2026, 10, 2))
        result = await deliver(node, "", "local")
        assert result["path"].endswith("a (2).mkv")
        assert (where / "a.mkv").read_bytes() == b"short"

    async def test_known_at_another_path_is_skipped_unless_turned_off(
            self, lib, tmp_path, monkeypatch):
        lib.drive.add("/Media/a.mkv", size=2000)
        await lib.sync()
        node = await lib.ctx.store.node_at("/Media/a.mkv")
        io = CdnIO(payload(2000))
        now = outbound.make_deliver(lib.ctx, io=io, clock=on(2026, 10, 2))
        first = await now(node, "", "local")
        assert (folder(tmp_path) / "a.mkv").exists() and not first.get("skipped")
        requests = len(io.requests)
        # The next day the destination is another folder; the log knows the file.
        later = outbound.make_deliver(lib.ctx, io=io, clock=on(2026, 10, 3))
        again = await later(node, "", "local")
        assert again["skipped"] == "known" and again["known_at"] == str(folder(tmp_path) / "a.mkv")
        assert len(io.requests) == requests and not folder(tmp_path, "2026.10.3").exists()
        rows = await lib.ctx.store.downloads(status=["skipped_exists"])
        assert rows[0]["reason"] == str(folder(tmp_path) / "a.mkv")
        # ... unless the file is gone from where the log says it is.
        (folder(tmp_path) / "a.mkv").unlink()
        assert not (await later(node, "", "local")).get("skipped")
        # ... or the check is switched off.
        monkeypatch.setenv("OUTBOUND_SKIP_KNOWN", "false")
        (folder(tmp_path, "2026.10.3") / "a.mkv").unlink()
        node_again = outbound.make_deliver(lib.ctx, io=io, clock=on(2026, 10, 3))
        (folder(tmp_path) / "a.mkv").parent.mkdir(parents=True, exist_ok=True)
        (folder(tmp_path) / "a.mkv").write_bytes(b"z" * 2000)
        assert not (await node_again(node, "", "local")).get("skipped")

    async def test_a_name_said_to_be_downloaded_is_skipped(self, lib, tmp_path):
        lib.drive.add(f"/Media/{ABCD}", size=2000)
        await lib.sync()
        await downloads.mark(lib.ctx, ["abcd00123"])
        node = await lib.ctx.store.node_at(f"/Media/{ABCD}")
        io = CdnIO(payload(2000))
        result = await outbound.make_deliver(lib.ctx, io=io, clock=on(2026, 10, 2))(
            node, "", "local")
        assert result["skipped"] == "known" and io.requests == []

    async def test_the_plan_lists_them_up_front_and_leaves_them_out(self, lib, tmp_path):
        for name in ("a.mkv", "b.mkv", "c.mkv"):
            lib.drive.add(f"/Media/{name}", size=2000)
        await lib.sync()
        where = folder(tmp_path)
        where.mkdir(parents=True)
        (where / "a.mkv").write_bytes(b"x" * 2000)
        await downloads.mark(lib.ctx, ["b.mkv"])
        query = Query(intent="download", scope={"path": "/Media"},
                      filters=Filters(kinds=["video"]))
        proposal = await nl.make_proposal(
            lib.ctx, query, now=datetime(2026, 10, 2, 12, 0, tzinfo=SH))
        assert [a.before["name"] for a in proposal.plan.actions] == ["c.mkv"]
        text = "\n".join(nl.proposal_lines(proposal))
        assert "已下载过，将跳过（2 个，3.9 KiB）：a.mkv、b.mkv" in text
        assert proposal.plan_id is not None

    async def test_a_plan_with_everything_present_has_nothing_to_confirm(self, lib, tmp_path):
        lib.drive.add("/Media/a.mkv", size=2000)
        await lib.sync()
        where = folder(tmp_path)
        where.mkdir(parents=True)
        (where / "a.mkv").write_bytes(b"x" * 2000)
        query = Query(intent="download", filters=Filters(kinds=["video"]))
        proposal = await nl.make_proposal(
            lib.ctx, query, now=datetime(2026, 10, 2, 12, 0, tzinfo=SH))
        assert proposal.plan.is_empty and proposal.plan_id is None
        text = "\n".join(nl.proposal_lines(proposal))
        assert "已下载过，将跳过（1 个" in text and "没有需要下载的" in text
        assert await plans.listing(lib.ctx) == []


def _write(path, data: bytes):
    path.write_bytes(data)
    return path


# ============================================================ C. the sentences


class TestSentences:
    def parse(self, text):
        return RulesTranslator().parse(text, NOW, SH)

    def test_the_sentence_of_nl_4_keeps_its_intent_and_leaves_the_name_out(self):
        query = self.parse("把今天保存但还未下载过的视频下载，abcd00123 下过了")
        assert isinstance(query, Query) and query.intent == "download"
        assert query.filters.kinds == ["video"] and query.filters.not_downloaded
        assert query.filters.exclude_names == ["abcd00123"]
        assert query.filters.name_contains == [] and query.filters.name_regex is None
        assert query.filters.created_after == "2026-09-24T00:00:00+08:00"
        assert query.marked == ["abcd00123"]

    def test_the_sentence_of_nl_3_is_a_remark_never_a_plan(self):
        remark = self.parse("abcd00123 下过了")
        assert remark == Remark(names=["abcd00123"], downloaded=True)

    def test_the_sentence_of_plan_60(self):
        query = self.parse("下载今天保存的两个视频")
        assert query.intent == "download" and query.filters.limit == 2
        assert query.filters.kinds == ["video"] and not query.filters.not_downloaded

    @pytest.mark.parametrize("text", [
        "未下载的视频下载", "下载还没下的视频", "把没下载过的视频下载", "下载还未下载过的视频",
        "下载相对NAS新的视频",
    ])
    def test_the_phrases_for_not_downloaded_yet(self, text):
        query = self.parse(text)
        assert isinstance(query, Query) and query.filters.not_downloaded, text
        assert query.filters.kinds == ["video"] and query.intent == "download"

    @pytest.mark.parametrize(("text", "names", "downloaded"), [
        ("abcd00123 已经下了", ["abcd00123"], True),
        ("abcd00123已下载", ["abcd00123"], True),
        ("「example abcd」下载过了", ["example abcd"], True),
        ("a1b2c3 和 x9y8z7 下过了", ["a1b2c3", "x9y8z7"], True),
        ("不要 abcd00123", ["abcd00123"], False),
        ("不要下载 abcd00123", ["abcd00123"], False),
        ("跳过 abcd00123", ["abcd00123"], False),
        ("abcd00123 不用", ["abcd00123"], False),
        ("示例影像 不用下载了", ["示例影像"], False),
    ])
    def test_remarks_on_their_own(self, text, names, downloaded):
        assert self.parse(text) == Remark(names=names, downloaded=downloaded)

    @pytest.mark.parametrize("text", ["那些下过了", "视频下过了", "不要重复的视频", "不要 删除"])
    def test_words_that_only_point_or_describe_are_not_names(self, text):
        assert not isinstance(self.parse(text), Remark)

    def test_an_exclusion_inside_a_request(self):
        query = self.parse("下载今天的视频，除了 abcd00123")
        assert query.intent == "download" and query.filters.exclude_names == ["abcd00123"]
        assert query.marked == []                            # not said to be downloaded

    def test_a_name_that_is_a_whole_file_is_not_the_file_to_fetch(self):
        query = self.parse("下载今天的视频，example.com@abcd00123.part1.mp4 下过了")
        assert query.filters.name_equals is None
        assert query.filters.exclude_names == [ABCD]

    def test_not_downloaded_alone_still_asks_how_much(self):
        # No time, size, kind or place: the whole drive minus what is there is still too much.
        result = self.parse("下载还没下的")
        assert isinstance(result, Clarification) and result.question == "nl.ask.download_all"

    def test_an_old_negation_is_still_a_refusal(self):
        assert self.parse("不要删除 /Inbox 里的视频") == Clarification(question="nl.ask.negated")

    async def test_the_guard_a_model_is_never_asked_about_a_remark(self):
        class Model:
            name = "fake"
            calls = 0

            async def translate(self, text, now, tz):
                Model.calls += 1
                return Query(intent="download", filters=Filters(name_contains=["abcd00123"]))

        chain = Chain([Model()])
        result = await chain.translate("abcd00123 下过了", NOW, SH)
        assert isinstance(result, Remark) and Model.calls == 0

    async def test_a_models_inverted_answer_is_put_right(self):
        class Model:
            name = "fake"

            async def translate(self, text, now, tz):
                # What nl:4 got: the name the sentence says to leave out, selected.
                return Query(intent="download", filters=Filters(
                    name_regex="(?i)abcd00123", created_after="2026-09-24T00:00:00+08:00",
                    kinds=["video"]))

        chain = Chain([Model()])
        sentence = "帮忙把今天新存的那几部视频下一下，abcd00123 下过了"
        result = await chain.translate(sentence, NOW, SH)
        assert isinstance(result, Query)
        assert result.filters.name_regex is None and result.filters.name_contains == []
        assert result.filters.exclude_names == ["abcd00123"] and result.marked == ["abcd00123"]

    def test_the_wire_format_carries_the_new_fields(self):
        filters = wire_schema()["properties"]["filters"]
        assert "not_downloaded" in filters["required"] and "exclude_names" in filters["required"]
        query = from_wire({
            "intent": "download", "scope": {"path": "/", "recursive": True},
            "filters": {"kinds": ["video"], "not_downloaded": True, "exclude_names": ["x1"],
                        "limit": None},
            "action_args": {}, "schedule": None, "needs_clarification": None})
        assert query.filters.not_downloaded and query.filters.exclude_names == ["x1"]
        assert query.canonical()["filters"]["not_downloaded"] is True

    def test_a_model_that_invents_an_exclusion_loses_it(self):
        query = Query(intent="download", filters=Filters(kinds=["video"], exclude_names=["zzz"]))
        from pikpak_wms.nl.guard import ground

        ground("下载视频", query)
        assert query.filters.exclude_names == []
        assert guard("下载视频", query) is query


class TestNotDownloaded:
    async def _drive(self, w):
        for name, size in (("a.mp4", 500), ("b.mp4", 600), ("c.mp4", 700), ("d.mp4", 800)):
            w.drive.add(f"/Media/{name}", size=size)
        await w.sync()

    async def test_the_filter_leaves_out_what_the_log_knows(self, world, tmp_path):
        w = world
        w.ctx.config.outbound.local_dir = tmp_path / "media"
        w.ctx.config.outbound.downloader = "local"
        await self._drive(w)
        store = w.ctx.store
        a = await store.node_at("/Media/a.mp4")
        await store.add_download(name="a.mp4", size=500, file_id=a.file_id, status="done",
                                 dest_path="/x/a.mp4")
        await store.add_download(name="b.mp4", size=600, file_id="gone-id", status="done",
                                 dest_path="/x/b.mp4")              # same name and size, new id
        await downloads.mark(w.ctx, ["c.mp"])                       # a fragment of c.mp4
        await store.add_download(name="d.mp4", size=800, file_id="d", status="failed")
        query = Query(intent="download", scope={"path": "/Media"},
                      filters=Filters(kinds=["video"], not_downloaded=True))
        proposal = await nl.make_proposal(w.ctx, query, now=NOW)
        assert [n.name for n in proposal.matches] == ["d.mp4"]      # a failure is not on the NAS
        assert [a.before["name"] for a in proposal.plan.actions] == ["d.mp4"]
        assert "只含 NAS 上还没有的文件（对照下载记录）" in "\n".join(nl.proposal_lines(proposal))

    async def test_the_newest_n_are_counted_among_those_not_yet_downloaded(self, world, tmp_path):
        w = world
        w.ctx.config.outbound.local_dir = tmp_path / "media"
        w.ctx.config.outbound.downloader = "local"
        await self._drive(w)
        d = await w.ctx.store.node_at("/Media/d.mp4")                # the newest
        await w.ctx.store.add_download(name="d.mp4", size=800, file_id=d.file_id, status="done")
        query = Query(intent="download", scope={"path": "/Media"},
                      filters=Filters(not_downloaded=True, limit=1))
        proposal = await nl.make_proposal(w.ctx, query, now=NOW)
        assert [n.name for n in proposal.matches] == ["c.mp4"]

    async def test_an_exclusion_and_its_marked_row(self, world, tmp_path):
        w = world
        w.ctx.config.outbound.local_dir = tmp_path / "media"
        w.ctx.config.outbound.downloader = "local"
        w.drive.add(f"/Media/{ABCD}", size=900)
        w.drive.add("/Media/other.mp4", size=100)
        await w.sync()
        query = RulesTranslator().parse(
            "把保存的视频下载，abcd00123 下过了", NOW, SH)
        assert isinstance(query, Query)
        proposal = await nl.make_proposal(w.ctx, query, now=NOW, user_id=42)
        assert [n.name for n in proposal.matches] == ["other.mp4"]
        rows = await w.ctx.store.downloads(status=["marked"])
        assert [(r["name"], r["user_id"]) for r in rows] == [(ABCD, 42)]
        assert "记下了：abcd00123 已下载" in "\n".join(nl.proposal_lines(proposal))
        assert "排除名字含「abcd00123」的文件" in "\n".join(nl.proposal_lines(proposal))

    async def test_a_scheduled_rule_is_not_fixed_to_a_list(self, world, tmp_path):
        w = world
        w.ctx.config.outbound.local_dir = tmp_path / "media"
        await self._drive(w)
        query = Query(intent="download", scope={"path": "/Media"},
                      schedule={"cron": "0 3 * * *"}, filters=Filters(not_downloaded=True))
        proposal = await nl.make_proposal(w.ctx, query, now=NOW)
        assert proposal.kind == "rule"
        assert "file_ids" not in proposal.rules[0].match.model_dump(exclude_none=True)


class TestPendingPlans:
    async def test_names_are_taken_out_of_an_open_plan(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", ABCD, "c.mkv"))
        gone = await plans.remove_matching(world.ctx, plan_id, ["abcd00123"])
        assert gone == [ABCD]
        row = await plans.get(world.ctx, plan_id)
        assert [a.before["name"] for a in row["plan"].actions] == ["a.mkv", "c.mkv"]
        assert row["status"] == "pending" and row["fingerprint"] == plans.fingerprint(row["plan"])
        assert await plans.remove_matching(world.ctx, plan_id, ["nothing"]) == []

    async def test_a_plan_left_empty_is_discarded(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path, names=(ABCD,))
        assert await plans.remove_matching(world.ctx, plan_id, ["ABCD"]) == [ABCD]
        assert (await plans.get(world.ctx, plan_id))["status"] == "discarded"

    async def test_what_is_already_done_is_not_touched(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", ABCD))
        await world.ctx.store.update_plan(plan_id, status="partial", progress=1,
                                          result={"settled": {"1": "done"}})
        assert await plans.remove_matching(world.ctx, plan_id, ["a.mkv"]) == []


# ======================================================== D. files in parallel


class TestParallelFiles:
    async def _run(self, world, tmp_path, io, names, **kw):
        plan_id = await _outbound_plan(world, tmp_path, names=names, size=2000)
        runs = Runs(world.ctx, asyncio.Lock())
        run = await runs.start(plan_id, make_deliver=lambda progress: outbound.make_deliver(
            world.ctx, io=io, progress=progress, **kw))
        return plan_id, runs, run

    async def test_all_files_at_once_by_default(self, world, tmp_path):
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        names = ("a.mkv", "b.mkv", "c.mkv")
        _plan_id, _runs, run = await self._run(world, tmp_path, io, names)
        await until(lambda: len(io.begun) == 3)
        assert run.control.counts()["active"] == 3 and run.control.limit == 0
        io.gate.set()
        await asyncio.wait_for(run.finished.wait(), 5)
        assert run.report.applied == 3
        assert all((tmp_path / "media" / n).read_bytes() == payload(2000) for n in names)

    async def test_the_limit_holds_the_rest_back_and_can_change_while_running(
            self, world, tmp_path, monkeypatch):
        monkeypatch.setenv("OUTBOUND_PARALLEL_FILES", "2")
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        names = ("a.mkv", "b.mkv", "c.mkv", "d.mkv")
        plan_id, runs, run = await self._run(world, tmp_path, io, names)
        await until(lambda: len(io.begun) == 2)
        await asyncio.sleep(0.05)
        counts = run.control.counts()
        assert (counts["active"], counts["queued"]) == (2, 2) and len(io.begun) == 2
        await runs.set_parallel(plan_id, 0)                     # 不限, while it runs
        await until(lambda: len(io.begun) == 4)
        io.gate.set()
        await asyncio.wait_for(run.finished.wait(), 5)
        assert run.report.applied == 4
        assert await runs.parallel_for(plan_id) == 0

    async def test_the_choice_of_a_plan_beats_the_default_beats_the_environment(
            self, world, tmp_path, monkeypatch):
        monkeypatch.setenv("OUTBOUND_PARALLEL_FILES", "8")
        runs = Runs(world.ctx, asyncio.Lock())
        assert await runs.parallel_for(1) == 8
        await runs.set_parallel(None, 4)
        assert await runs.parallel_for(1) == 4
        await runs.set_parallel(1, 1)
        assert await runs.parallel_for(1) == 1 and await runs.parallel_for(2) == 4

    async def test_the_connection_budget_is_shared_by_every_file(self, tmp_path):
        data = payload(8 * 1024 * 1024)
        pool = fetch.ConnectionPool(6)
        io = CdnIO(data)
        io.chunk = 512 * 1024

        async def one(n):
            return await fetch.download(
                _Url(f"https://x/{n}"), io, tmp_path / f"f{n}.part", connections=4,
                max_connections=4, pool=pool)

        sizes = await asyncio.gather(*(one(n) for n in range(4)))      # 16 asked for, 6 allowed
        assert sizes == [len(data)] * 4
        assert all((tmp_path / f"f{n}.part").read_bytes() == data for n in range(4))
        assert 1 <= io.peak <= 6 and pool.used == 0

    async def test_a_cancelled_wait_gives_the_slot_back(self):
        pool = fetch.ConnectionPool(1)
        await pool.acquire()
        waiting = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        pool.release()
        await asyncio.wait_for(pool.acquire(), 1)                 # nobody is stuck in the queue
        assert pool.used == 1


class _Url:
    def __init__(self, url: str) -> None:
        self.url = url

    async def __call__(self):
        return self.url


class TestTrouble:
    class Busy(Exception):
        status = 503

    class Clock:
        def __init__(self) -> None:
            self.now = 0.0
            self.pauses: list[float] = []

        def __call__(self) -> float:
            return self.now

        async def sleep(self, seconds: float) -> None:
            self.pauses.append(seconds)
            self.now += seconds
            await asyncio.sleep(0)

    async def test_a_burst_of_503s_backs_off_relinks_and_then_succeeds(self, tmp_path):
        data = payload(4000)
        io = CdnIO(data)
        io.failures["u"] = [self.Busy() for _ in range(5)]
        clock = self.Clock()
        calls = {"n": 0}

        async def url_for():
            calls["n"] += 1
            return "u"

        size = await fetch.download(url_for, io, tmp_path / "a.part", connections=1, clock=clock,
                                    sleep=clock.sleep, retry_seconds=1800)
        assert size == len(data) and (tmp_path / "a.part").read_bytes() == data
        assert clock.pauses[:5] == [5, 10, 20, 40, 80]               # exponential, from 5 s
        assert calls["n"] >= 3                                       # asked again after 2 and 4

    async def test_the_pause_stops_at_two_minutes(self, tmp_path):
        io = CdnIO(payload(4000))
        io.failures["u"] = [self.Busy() for _ in range(8)]
        clock = self.Clock()
        await fetch.download(_Url("u"), io, tmp_path / "b.part", connections=1, clock=clock,
                             sleep=clock.sleep, retry_seconds=10_000)
        assert max(clock.pauses) == 120 and clock.pauses[-1] == 120

    async def test_connection_errors_are_not_fatal_either(self, tmp_path):
        io = CdnIO(payload(4000))
        io.failures["u"] = [ConnectionResetError("reset"), TimeoutError("slow"),
                            OSError("Cannot connect to host dl-a10b-1.mypikpak.com:443")]
        clock = self.Clock()
        size = await fetch.download(_Url("u"), io, tmp_path / "c.part", connections=1,
                                    clock=clock, sleep=clock.sleep, retry_seconds=1800)
        assert size == 4000 and clock.pauses[:2] == [5, 10]

    async def test_trouble_that_lasts_past_the_window_fails_the_file(self, tmp_path):
        io = CdnIO(payload(4000))
        io.failures["u"] = [self.Busy() for _ in range(999)]
        clock = self.Clock()
        part = tmp_path / "d.part"
        with pytest.raises(self.Busy):
            await fetch.download(_Url("u"), io, part, connections=1, clock=clock,
                                 sleep=clock.sleep, retry_seconds=60)
        assert sum(clock.pauses) >= 60 and part.exists()             # kept for the next run

    async def test_progress_resets_the_window(self, tmp_path):
        data = payload(4000)
        io = CdnIO(data)
        clock = self.Clock()
        # Trouble, then bytes, then trouble again: each stretch is shorter than the window.
        failures = [self.Busy(), self.Busy(), self.Busy()]
        original = io.stream

        async def stream(url, start, end):
            if failures and start >= 1000:
                raise failures.pop()
            async for piece in original(url, start, end):
                yield piece
                if start == 0:
                    raise self.Busy()                                 # drops once, after bytes

        io.stream = stream
        size = await fetch.download(_Url("u"), io, tmp_path / "e.part", connections=1,
                                    clock=clock, sleep=clock.sleep, retry_seconds=60)
        assert size == 4000

    async def test_a_plain_error_still_fails_after_a_few_tries(self, tmp_path):
        io = CdnIO(payload(4000))
        io.failures["u"] = [ValueError("no")] * 99
        clock = self.Clock()
        with pytest.raises(ValueError):
            await fetch.download(_Url("u"), io, tmp_path / "f.part", connections=1, clock=clock,
                                 sleep=clock.sleep, retry_seconds=1800)
        assert len(clock.pauses) <= fetch.RETRIES

    async def test_through_a_plan_503s_do_not_fail_the_file_but_a_long_outage_does(
            self, world, tmp_path, monkeypatch):
        monkeypatch.setenv("OUTBOUND_RETRY_MINUTES", "1")
        plan_id = await _outbound_plan(world, tmp_path, names=("ok.mkv", "dead.mkv"), size=2000)
        io = CdnIO(payload(2000))
        ok = f"https://download.example/{world.drive.id_at('/Media/ok.mkv')}"
        dead = f"https://download.example/{world.drive.id_at('/Media/dead.mkv')}"
        io.failures[ok] = [self.Busy(), self.Busy()]
        io.failures[dead] = [self.Busy() for _ in range(999)]
        clock = self.Clock()
        deliver = outbound.make_deliver(world.ctx, io=io, sleep=clock.sleep, fetch_clock=clock)
        report = await plans.apply(world.ctx, plan_id, deliver=deliver)
        assert report.applied == 1 and len(report.failed) == 1
        assert (tmp_path / "media" / "ok.mkv").exists()
        rows = {r["name"]: r["status"] for r in await world.ctx.store.downloads()}
        assert rows == {"ok.mkv": "done", "dead.mkv": "failed"}


# =================================================== D. cancel one, stop, resume


class TestCancelOneFile:
    async def test_cancelling_a_file_keeps_its_part_and_the_others_go_on(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", "b.mkv"), size=2000)
        io = CdnIO(payload(2000))
        io.gate, io.hold_after = asyncio.Event(), 2
        runs = Runs(world.ctx, asyncio.Lock())
        run = await runs.start(plan_id, make_deliver=lambda progress: outbound.make_deliver(
            world.ctx, io=io, progress=progress, plan_id=plan_id))
        await until(lambda: len(io.begun) == 2)
        await asyncio.sleep(0.05)                                  # two chunks each are in
        assert runs.cancel_file(plan_id, 0)
        await until(lambda: run.control.tracks[0].state == "cancelled")
        assert run.active and run.control.tracks[1].state == "active"   # b is still running
        io.gate.set()
        await asyncio.wait_for(run.finished.wait(), 5)
        report = run.report
        assert report.applied == 1 and [c["file_id"] for c in report.cancelled] == [
            world.drive.id_at("/Media/a.mkv")]
        media = tmp_path / "media"
        assert (media / "b.mkv").read_bytes() == payload(2000)
        assert not (media / "a.mkv").exists() and (media / "a.mkv.part").exists()
        assert fetch.state_path(media / "a.mkv.part").exists()
        rows = {r["name"]: r["status"] for r in await world.ctx.store.downloads()}
        assert rows == {"a.mkv": "cancelled", "b.mkv": "done"}
        row = await plans.get(world.ctx, plan_id)
        assert row["status"] == "applied" and len(row["result"]["cancelled"]) == 1
        assert not runs.cancel_file(plan_id, 0)                    # nothing runs now

    async def test_a_queued_file_can_be_cancelled_before_it_starts(
            self, world, tmp_path, monkeypatch):
        monkeypatch.setenv("OUTBOUND_PARALLEL_FILES", "1")
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", "b.mkv"), size=2000)
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        runs = Runs(world.ctx, asyncio.Lock())
        run = await runs.start(plan_id, make_deliver=lambda progress: outbound.make_deliver(
            world.ctx, io=io, progress=progress))
        await until(lambda: len(io.begun) == 1)
        assert run.control.tracks[1].state == "queued"
        assert runs.cancel_file(plan_id, 1)
        io.gate.set()
        await asyncio.wait_for(run.finished.wait(), 5)
        assert run.report.applied == 1 and len(run.report.cancelled) == 1
        assert not (tmp_path / "media" / "b.mkv.part").exists()    # it never began

    async def test_a_cancelled_file_resumes_from_its_part(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv",), size=2000)
        io = CdnIO(payload(2000))
        io.gate, io.hold_after = asyncio.Event(), 2
        runs = Runs(world.ctx, asyncio.Lock())
        run = await runs.start(plan_id, make_deliver=lambda progress: outbound.make_deliver(
            world.ctx, io=io, progress=progress))
        await until(lambda: io.started(world.drive.id_at("/Media/a.mkv")))
        await asyncio.sleep(0.05)
        assert runs.cancel_file(plan_id, 0)
        await asyncio.wait_for(run.finished.wait(), 5)
        (entry,) = (await plans.get(world.ctx, plan_id))["result"]["cancelled"]
        # Run it again, as 重试失败的 does: a new plan of the same action.
        action = (await plans.get(world.ctx, plan_id))["plan"].actions[0]
        again = Plan(source="outbound", generated_at="now", actions=[action])
        again_id = await plans.save(world.ctx, again)
        fresh = CdnIO(payload(2000))
        second = await runs.start(again_id, make_deliver=lambda progress: outbound.make_deliver(
            world.ctx, io=fresh, progress=progress))
        await asyncio.wait_for(second.finished.wait(), 5)
        assert entry["file_id"] == action.file_id and second.report.applied == 1
        assert (tmp_path / "media" / "a.mkv").read_bytes() == payload(2000)
        assert fresh.requests and min(start for start, _ in fresh.requests) >= 1000   # not from 0

    async def test_stop_and_resume_do_not_repeat_what_finished_ahead(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", "b.mkv"))
        release = asyncio.Event()
        calls: list[str] = []

        async def deliver(node, to, via=None):
            calls.append(node.name)
            if node.name == "a.mkv" and not release.is_set():
                await release.wait()
            return {"downloader": "local"}

        runs = Runs(world.ctx, asyncio.Lock())
        run = await runs.start(plan_id, deliver=deliver)
        await until(lambda: run.control.tracks.get(1) is not None
                    and run.control.tracks[1].state == "done")
        await runs.stop(plan_id)
        row = await plans.get(world.ctx, plan_id)
        assert row["progress"] == 0 and row["result"]["settled"] == {"1": "done"}
        release.set()
        again = await runs.start(plan_id, deliver=deliver)
        await asyncio.wait_for(again.finished.wait(), 5)
        assert calls == ["a.mkv", "b.mkv", "a.mkv"]               # b was not done twice
        row = await plans.get(world.ctx, plan_id)
        assert row["status"] == "applied" and row["result"]["applied"] == 2
        assert "settled" not in row["result"]

    async def test_a_download_that_was_already_there_counts_as_skipped_not_applied(
            self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", "b.mkv"), size=2000)
        media = tmp_path / "media"
        media.mkdir()
        (media / "a.mkv").write_bytes(payload(2000))
        deliver = outbound.make_deliver(world.ctx, io=CdnIO(payload(2000)))
        report = await plans.apply(world.ctx, plan_id, deliver=deliver)
        assert report.applied == 1 and report.skipped == {"exists": 1}
        assert "跳过 1" in report.summary()

    async def test_a_stopped_summary_mentions_what_was_cancelled(self, world):
        report = plans.ApplyReport(plan_id=3, applied=2,
                                   cancelled=[{"path": "/a", "file_id": "x"}])
        assert report.summary().endswith("，已停止 1")


# ===================================================================== misc


class TestSettings:
    def test_defaults_and_overrides(self, monkeypatch):
        config = Config().outbound
        assert (config.parallel_files, config.max_total_connections) == (0, 32)
        assert config.retry_minutes == 30 and config.skip_known is True
        monkeypatch.setenv("OUTBOUND_PARALLEL_FILES", "3")
        monkeypatch.setenv("OUTBOUND_MAX_TOTAL_CONNECTIONS", "10")
        monkeypatch.setenv("OUTBOUND_RETRY_MINUTES", "5")
        monkeypatch.setenv("OUTBOUND_SKIP_KNOWN", "false")
        config = Config().outbound
        assert (config.parallel_files, config.max_total_connections) == (3, 10)
        assert config.retry_minutes == 5 and config.skip_known is False
        monkeypatch.setenv("OUTBOUND_PARALLEL_FILES", "many")
        assert Config().outbound.parallel_files == 0


class TestDownloadsCommand:
    def _rows(self, tmp_path):
        async def fill():
            async with Store(tmp_path / "wms.sqlite3") as store:
                await store.add_download(name="a.mkv", size=2048, status="done",
                                         dest_path="/library/a.mkv", avg_mib_s=2.5,
                                         finished_at="2026-10-01T10:00:00+00:00")
                await store.add_download(name=ABCD, size=10, status="failed", reason="503",
                                         finished_at="2026-10-02T10:00:00+00:00")
                await store.add_download(name="old.mkv", size=1, status="done",
                                         finished_at="2025-01-01T00:00:00+00:00")

        asyncio.run(fill())

    def test_the_listing_and_its_filters(self, tmp_path, monkeypatch):
        import json

        from typer.testing import CliRunner

        from pikpak_wms.cli import main as cli

        self._rows(tmp_path)
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("WMS_LANG", "zh")
        set_language(None)
        monkeypatch.setattr(cli.state, "provider_factory",
                            lambda config: provider_for(FakeDrive()))
        runner = CliRunner()
        shown = runner.invoke(cli.app, ["downloads", "--since", "2026-10-01"])
        assert shown.exit_code == 0, shown.output
        assert "a.mkv" in shown.output and "已下载" in shown.output
        assert "old.mkv" not in shown.output
        assert "2.5 MiB/s" in shown.output and "/library/a.mkv" in shown.output
        data = json.loads(runner.invoke(cli.app, ["downloads", "--json"]).output)
        assert [r["name"] for r in data["downloads"]] == [ABCD, "a.mkv", "old.mkv"]
        failed = json.loads(runner.invoke(
            cli.app, ["downloads", "--status", "failed", "--json"]).output)
        assert [r["status"] for r in failed["downloads"]] == ["failed"]
        named = runner.invoke(cli.app, ["downloads", "--name", "ABCD00123"])
        assert ABCD in named.output and "a.mkv" not in named.output
        assert "下载记录是空的" in runner.invoke(
            cli.app, ["downloads", "--name", "nope"]).output
        assert "old.mkv" not in runner.invoke(cli.app, ["downloads", "--today"]).output
