"""WMS M9.2 in the bot: /downloads, the progress of many files, 取消 on one, 同时下载,
重试失败的, and 「X 下过了」 as a sentence."""

from __future__ import annotations

import asyncio
import time

import pytest
from test_wms_m83 import payload
from test_wms_m83_bot import ADMIN, Event, Rig, buttons_of, press, until
from test_wms_m92 import JUVR, CdnIO
from wms_fakes import FakeDrive

from pikpak_wms.ops import embed, outbound
from pikpak_wms.ops.stocktake import stocktake
from tgmd import handlers as handlers_module
from tgmd import i18n
from tgmd.botconfig import COMMAND_NAMES, commands


@pytest.fixture(autouse=True)
def chinese():
    previous = i18n.language()
    i18n.set_language("zh")
    yield
    i18n.set_language(previous)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "wms.yaml").write_text(
        "ratelimit: {requests_per_second: 100000, burst: 100000}\n"
        "schedule: {builtin: false}\n"
        f"outbound: {{downloader: local, local_dir: {tmp_path / 'media'}}}\n")
    monkeypatch.setattr(handlers_module, "has_downloadable_media", lambda _m: False)
    return Rig(FakeDrive(), tmp_path)


def serve_with(monkeypatch, io):
    real = outbound.make_deliver
    monkeypatch.setattr(embed.outbound, "make_deliver",
                        lambda ctx, progress=None, **kw: real(ctx, io=io, progress=progress, **kw))


def media_files(drive, names, size=2000):
    for name in names:
        drive.add(f"/Media/{name}", size=size)


async def plan_of(inbot, names) -> int:
    ctx = inbot.embedded.ctx
    await stocktake(ctx.client, ctx.store, full=True)
    from pikpak_wms.ops import plans

    return await plans.save(ctx, await outbound.plan_paths(ctx, [f"/Media/{n}" for n in names]))


# ============================================================== /downloads


class TestDownloadsCommand:
    async def test_it_shows_today_with_buttons_and_the_default(self, rig):
        inbot, handlers = await rig.boot()
        try:
            store = inbot.embedded.ctx.store
            await store.add_download(name="a.mkv", size=2000, status="done", plan_id=60,
                                     dest_path="/library/资源/整理/2026.10.2/a.mkv",
                                     avg_mib_s=3.3)
            await store.add_download(name="old.mkv", size=5, status="done",
                                     finished_at="2020-01-01T00:00:00+00:00")
            event = Event("/downloads")
            await handlers.on_downloads(event)
            text, kwargs = event.replies[0]
            assert "<b>下载记录</b> · 今天（1 条）" in text
            assert "a.mkv" in text and "已下载" in text and "3.3 MiB/s" in text
            assert "2026.10.2/a.mkv" in text and "old.mkv" not in text
            assert "之后新开的计划同时下载：不限" in text
            labels = [label for label, _ in buttons_of(kwargs)]
            assert labels == ["今天", "近7天", "失败的",
                              "同时 1", "同时 2", "同时 4", "✓ 同时 不限"]
        finally:
            await inbot.stop()

    async def test_search_and_the_buttons(self, rig):
        inbot, handlers = await rig.boot()
        try:
            store = inbot.embedded.ctx.store
            await store.add_download(name=JUVR, size=9, status="done")
            await store.add_download(name="x.mkv", size=9, status="failed", reason="503")
            await store.add_download(name="y.mkv", size=9, status="done",
                                     finished_at="2026-01-01T00:00:00+00:00")
            search = Event("/downloads juvr00309")
            await handlers.on_downloads(search)
            assert "包含「juvr00309」（1 条）" in search.replies[0][0]
            failed = await press(handlers, "wms:dl:failed")
            assert "失败的（1 条）" in failed.edits[0][0] and "x.mkv" in failed.edits[0][0]
            assert "503" in failed.edits[0][0] and JUVR not in failed.edits[0][0]
            week = await press(handlers, "wms:dl:week")
            assert "y.mkv" not in week.edits[0][0] and JUVR in week.edits[0][0]
            empty = Event("/downloads nothing-like-this")
            await handlers.on_downloads(empty)
            assert "没有下载记录" in empty.replies[0][0]
        finally:
            await inbot.stop()

    async def test_the_default_number_of_files_is_chosen_here(self, rig):
        inbot, handlers = await rig.boot()
        try:
            pressed = await press(handlers, "wms:par:0:2")
            assert pressed.answers[0][0] == "同时下载：2"
            assert "之后新开的计划同时下载：2" in pressed.edits[0][0]
            assert await inbot.embedded.parallel_files() == 2
            again = await press(handlers, "wms:par:0:0")
            assert again.answers[0][0] == "同时下载：不限"
            assert await inbot.embedded.parallel_files() == 0
        finally:
            await inbot.stop()

    async def test_only_admins_and_it_is_in_the_menu(self, rig):
        inbot, handlers = await rig.boot()
        try:
            event = Event("/downloads")
            event.sender_id = event.chat_id = 999
            await handlers.on_downloads(event)
            assert "管理员" in event.replies[0][0]
        finally:
            await inbot.stop()
        assert "downloads" in COMMAND_NAMES
        assert dict(commands("zh"))["downloads"] == "下载记录"
        assert dict(commands("en"))["downloads"] == "Download log"


# ====================================================== many files in a message


class TestManyFiles:
    async def test_the_message_lists_each_file_with_a_cancel_button(self, rig, monkeypatch):
        names = ("a.mkv", "b.mkv", "c.mkv")
        media_files(rig.drive, names)
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        serve_with(monkeypatch, io)
        inbot, handlers = await rig.boot()
        try:
            plan_id = await plan_of(inbot, names)
            pressed = await press(handlers, f"wms:apply:{plan_id}")
            # At once: the old message and its only button.
            assert buttons_of(pressed.edits[0][1]) == [("停止", f"wms:stop:{plan_id}".encode())]
            await until(lambda: len(io.begun) == 3)
            run = inbot.embedded.run_of(plan_id)
            text = inbot.progress_text(run)
            assert f"⏳ 计划 {plan_id} 执行中：已完成 0/3" in text
            assert "完成 0 · 失败 0 · 跳过 0 · 共 3" in text and "同时下载 不限" in text
            assert text.count("下载中") == 3 and "1. a.mkv" in text
            labels = [label for label, _ in buttons_of({"buttons": inbot.run_buttons(run)})]
            assert labels[:9] == ["⏸ 暂停 1 a.mkv", "■ 终止 1", "⬆ 优先 1", "⏸ 暂停 2 b.mkv",
                                  "■ 终止 2", "⬆ 优先 2", "⏸ 暂停 3 c.mkv", "■ 终止 3",
                                  "⬆ 优先 3"]
            assert labels[9:] == ["全部暂停", "全部开始", "整组优先", "同时 1", "同时 2",
                                  "同时 4", "✓ 同时 不限", "停止"]
            # The watcher puts these buttons on the message by itself.
            await until(lambda: any(len(buttons_of(k)) > 1 for _, k in pressed.edits))
            io.gate.set()
            await inbot.settle()
        finally:
            io.gate.set()
            await inbot.stop()

    async def test_many_files_are_paged(self, rig, monkeypatch):
        names = tuple(f"f{n:02d}.mkv" for n in range(8))
        media_files(rig.drive, names)
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        serve_with(monkeypatch, io)
        inbot, handlers = await rig.boot()
        try:
            plan_id = await plan_of(inbot, names)
            await press(handlers, f"wms:apply:{plan_id}")
            await until(lambda: len(io.begun) == 8)
            run = inbot.embedded.run_of(plan_id)
            first = inbot.progress_text(run)
            assert "第 1/2 页" in first and first.count("下载中") == 6 and "f07.mkv" not in first
            nav = [label for label, _ in buttons_of({"buttons": inbot.run_buttons(run)})]
            assert "下一页 ▶" in nav and "◀ 上一页" not in nav
            turned = await press(handlers, f"wms:page:{plan_id}:1")
            assert "第 2/2 页" in turned.edits[0][0] and "f07.mkv" in turned.edits[0][0]
            assert turned.edits[0][0].count("下载中") == 2
            back = [label for label, _ in buttons_of(turned.edits[0][1])]
            assert "◀ 上一页" in back and "下一页 ▶" not in back
            io.gate.set()
            await inbot.settle()
        finally:
            io.gate.set()
            await inbot.stop()

    async def test_cancel_one_file_then_retry_the_failed(self, rig, monkeypatch):
        names = ("a.mkv", "b.mkv")
        media_files(rig.drive, names)
        io = CdnIO(payload(2000))
        io.gate, io.hold_after = asyncio.Event(), 2
        serve_with(monkeypatch, io)
        inbot, handlers = await rig.boot()
        try:
            plan_id = await plan_of(inbot, names)
            pressed = await press(handlers, f"wms:apply:{plan_id}")
            await until(lambda: len(io.begun) == 2)
            await asyncio.sleep(0.05)
            cancel = await press(handlers, f"wms:cancel:{plan_id}:0")
            assert cancel.answers[0][0].startswith("已终止 a.mkv，已下载的部分保留")
            again = await press(handlers, f"wms:cancel:{plan_id}:0")
            assert again.answers[0][0] == "这个文件已经不在下载了" and again.answers[0][1]["alert"]
            io.gate.set()
            await inbot.settle()
            text, kwargs = pressed.edits[-1]
            assert "已停止 1" in text
            assert buttons_of(kwargs) == [
                ("重试失败的", f"wms:retry:{plan_id}".encode()),
                ("🗑 删除 1 a.mkv 已下载部分", f"wms:td:{plan_id}:0".encode())]
            media = rig.tmp_path / "media"
            assert (media / "a.mkv.part").exists() and (media / "b.mkv").exists()
            # 重试失败的: a new plan of the stopped file, shown in the same message.
            io.gate = None
            retry = await press(handlers, f"wms:retry:{plan_id}")
            assert retry.answers[0][0] == "开始执行"
            await inbot.settle()
            assert (media / "a.mkv").read_bytes() == payload(2000)
            assert "执行 1" in retry.edits[-1][0]
            nothing = await press(handlers, f"wms:retry:{plan_id}")
            assert nothing.answers[0][0] == "没有失败或被取消的文件" or nothing.answers
        finally:
            io.gate = None
            await inbot.stop()

    async def test_the_number_at_once_can_be_changed_on_the_message(self, rig, monkeypatch):
        names = ("a.mkv", "b.mkv", "c.mkv")
        media_files(rig.drive, names)
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        serve_with(monkeypatch, io)
        inbot, handlers = await rig.boot()
        try:
            plan_id = await plan_of(inbot, names)
            await press(handlers, f"wms:apply:{plan_id}")
            await until(lambda: len(io.begun) == 3)
            pressed = await press(handlers, f"wms:par:{plan_id}:1")
            assert pressed.answers[0][0] == "同时下载：1"
            assert inbot.embedded.run_of(plan_id).control.limit == 1
            assert await inbot.embedded.parallel_files(plan_id) == 1
            io.gate.set()
            await inbot.settle()
        finally:
            io.gate.set()
            await inbot.stop()

    async def test_a_stale_button_is_harmless(self, rig):
        inbot, handlers = await rig.boot()
        try:
            for data in ("wms:cancel:9:0", "wms:page:9:1", "wms:cancel:x", "wms:par:9"):
                event = await press(handlers, data)
                assert event.edits == []
            assert (await press(handlers, "wms:retry:9")).answers
        finally:
            await inbot.stop()


# ====================================================== 「X 下过了」 in the bot


class TestRemarks:
    async def _sentence(self, inbot, text, user=ADMIN):
        return await inbot.nl_message(user, text)

    def _drive(self, rig):
        media_files(rig.drive, ("a.mkv", JUVR, "c.mkv"))

    async def test_the_sentence_of_nl_4_makes_a_plan_without_the_name(self, rig):
        self._drive(rig)
        inbot, _handlers = await rig.boot()
        try:
            text, buttons = await self._sentence(
                inbot, "把保存的视频下载，juvr00309 下过了")
            assert "juvr00309" in text and JUVR not in text         # only as the exclusion
            assert "a.mkv" in text and "c.mkv" in text
            assert [b[0] for b in buttons_of({"buttons": buttons})] == [
                "确认执行", "修改", "取消"]
            marked = await inbot.embedded.downloads(status=["marked"])
            assert [r["name"] for r in marked] == [JUVR]
            (plan,) = await inbot.embedded.open_plans()
            assert plan["actions"] == 2
        finally:
            await inbot.stop()

    async def test_a_remark_after_a_plan_takes_the_files_out_of_that_plan(self, rig):
        self._drive(rig)
        inbot, _handlers = await rig.boot()
        try:
            text, _ = await self._sentence(inbot, "下载 /Media 里的视频")
            assert JUVR in text
            reply, buttons = await self._sentence(inbot, "juvr00309 下过了")
            assert reply.startswith("已从计划 1 中去掉 juvr00309（1 个文件）")
            assert JUVR not in reply.split("\n", 1)[1] and "a.mkv" in reply
            assert [b[0] for b in buttons_of({"buttons": buttons})] == ["确认执行", "修改", "取消"]
            (plan,) = await inbot.embedded.open_plans()
            assert plan["actions"] == 2                              # no new plan, same one
            assert [r["name"] for r in await inbot.embedded.downloads(status=["marked"])] == [JUVR]
            # The buttons still belong to that plan.
            confirm = buttons_of({"buttons": buttons})[0][1].decode()
            assert confirm == "wms:nl:apply:1"
        finally:
            await inbot.stop()

    async def test_a_plain_exclusion_changes_the_plan_but_marks_nothing(self, rig):
        self._drive(rig)
        inbot, _handlers = await rig.boot()
        try:
            await self._sentence(inbot, "下载 /Media 里的视频")
            reply, _ = await self._sentence(inbot, "不要 c.mkv")
            assert reply.startswith("已从计划 1 中去掉 c.mkv（1 个文件）")
            assert await inbot.embedded.downloads(status=["marked"]) == []
            missing, _ = await self._sentence(inbot, "不要 zzz999")
            assert missing == "计划 1 里没有 zzz999。"
        finally:
            await inbot.stop()

    async def test_without_a_pending_plan_it_is_remembered_and_no_plan_is_made(self, rig):
        self._drive(rig)
        inbot, _handlers = await rig.boot()
        try:
            reply, buttons = await self._sentence(inbot, "juvr00309 下过了")
            assert reply == "记下了：juvr00309 已下载，之后不会再下" and buttons is None
            assert await inbot.embedded.open_plans() == []
            # The index is not loaded yet: the name itself is remembered, and works as a fragment.
            assert [r["name"] for r in await inbot.embedded.downloads(status=["marked"])] == [
                "juvr00309"]
            # A bare 「不要 X」 has nothing to act on, and says so.
            none, _ = await self._sentence(inbot, "不要 c.mkv")
            assert none == "没有可以去掉 c.mkv 的待执行计划。"
            assert await inbot.embedded.open_plans() == []
        finally:
            await inbot.stop()

    async def test_a_plan_older_than_half_an_hour_or_of_someone_else_is_left_alone(self, rig):
        self._drive(rig)
        inbot, _handlers = await rig.boot()
        try:
            await self._sentence(inbot, "下载 /Media 里的视频")
            record = await inbot.embedded.load_proposal(1)
            await inbot.embedded.save_proposal({**record, "at": time.time() - 31 * 60}, 1)
            reply, _ = await self._sentence(inbot, "juvr00309 下过了")
            assert reply.startswith("记下了")
            (plan,) = await inbot.embedded.open_plans()
            assert plan["actions"] == 3                              # untouched
            await inbot.embedded.save_proposal({**record, "at": time.time()}, 1)
            reply, _ = await self._sentence(inbot, "juvr00309 下过了", user=ADMIN + 1)
            assert reply.startswith("记下了")
            assert (await inbot.embedded.open_plans())[0]["actions"] == 3
        finally:
            await inbot.stop()

    async def test_the_last_file_out_of_a_plan_discards_it(self, rig):
        media_files(rig.drive, (JUVR,))
        inbot, _handlers = await rig.boot()
        try:
            await self._sentence(inbot, "下载 /Media 里的视频")
            reply, buttons = await self._sentence(inbot, "juvr00309 下过了")
            assert "已从计划 1 中去掉" in reply and "计划 1 里没有别的了，已丢弃。" in reply
            assert buttons is None and await inbot.embedded.open_plans() == []
        finally:
            await inbot.stop()


class TestRetryAndSummary:
    async def test_a_plan_finished_before_m9_2_can_be_retried_by_path(self, rig, monkeypatch):
        media_files(rig.drive, ("a.mkv", "b.mkv"))
        inbot, _handlers = await rig.boot()
        try:
            plan_id = await plan_of(inbot, ("a.mkv", "b.mkv"))
            ctx = inbot.embedded.ctx
            # What plan 66 left: failed entries that name the path but no file id.
            await ctx.store.update_plan(plan_id, status="applied", progress=2, result={
                "applied": 1, "failed": [{"path": "/Media/b.mkv", "action": "outbound",
                                          "error": "503"}]})
            retry_id = await inbot.embedded.retry_failed(plan_id)
            assert retry_id is not None and retry_id != plan_id
            row = (await inbot.embedded.open_plans())[0]
            assert row["actions"] == 1
            lines = "\n".join(await inbot.embedded.plan_lines(retry_id))
            assert "b.mkv" in lines and "a.mkv" not in lines
            await ctx.store.update_plan(plan_id, status="applied", progress=2, result={})
            assert await inbot.embedded.retry_failed(plan_id) is None
        finally:
            await inbot.stop()

    async def test_the_count_follows_the_files_not_the_first_unfinished(self, rig, monkeypatch):
        names = ("a.mkv", "b.mkv", "c.mkv")
        media_files(rig.drive, names)
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        io.hold_after = 1
        serve_with(monkeypatch, io)
        inbot, handlers = await rig.boot()
        try:
            plan_id = await plan_of(inbot, names)
            await press(handlers, f"wms:apply:{plan_id}")
            await until(lambda: len(io.begun) == 3)
            run = inbot.embedded.run_of(plan_id)
            assert inbot.embedded.cancel_file(plan_id, 2)
            await until(lambda: run.control.tracks[2].state == "cancelled")
            text = inbot.progress_text(run)
            assert "已完成 1/3" in text and "已取消 1" in text
            io.gate.set()
            await inbot.settle()
        finally:
            io.gate.set()
            await inbot.stop()
