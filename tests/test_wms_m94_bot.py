"""M9.4 in the bot: the ⬆ marker, the 优先 buttons, 整组优先, the sentences, /downloads."""

from __future__ import annotations

import asyncio

from test_wms_m83_bot import Event, buttons_of, press
from test_wms_m92_bot import chinese, rig  # noqa: F401 - fixtures
from test_wms_m93_bot import running

from pikpak_wms.ops import plans


async def levels(inbot, plan_id):
    row = await plans.get(inbot.embedded.ctx, plan_id)
    return [a.after.get("priority") for a in row["plan"].actions], row["plan"].priority


class TestButtons:
    async def test_the_button_cycles_and_the_line_shows_the_arrow(self, rig, monkeypatch):
        inbot, handlers, io, plan_id, _ = await running(rig, monkeypatch, ("a.mkv", "b.mkv"))
        try:
            run = inbot.embedded.run_of(plan_id)
            assert "1. a.mkv" in inbot.progress_text(run)
            seen = []
            for _ in range(3):
                pressed = await press(handlers, f"wms:tr:{plan_id}:0")
                seen.append((pressed.answers[0][0], run.control.tracks[0].priority))
            assert seen == [("任务 1 的优先级：高", 1), ("任务 1 的优先级：置顶", 2),
                            ("任务 1 的优先级：普通", 0)]
            await press(handlers, f"wms:tr:{plan_id}:0")
            assert "1. ⬆ a.mkv" in inbot.progress_text(run)
            await press(handlers, f"wms:tr:{plan_id}:0")
            assert "1. ⬆⬆ a.mkv" in inbot.progress_text(run)
            assert (await levels(inbot, plan_id))[0] == [2, None]      # kept in the plan
        finally:
            io.gate.set()
            await inbot.stop()

    async def test_the_whole_group_button(self, rig, monkeypatch):
        inbot, handlers, io, plan_id, _ = await running(rig, monkeypatch, ("a.mkv", "b.mkv"))
        try:
            run = inbot.embedded.run_of(plan_id)
            pressed = await press(handlers, f"wms:pr:{plan_id}")
            assert pressed.answers[0][0] == "整组优先级：高"
            assert [t.priority for t in run.control.tracks.values()] == [1, 1]
            assert await levels(inbot, plan_id) == ([None, None], 1)
            labels = [b for b, _ in buttons_of({"buttons": inbot.run_buttons(run)})]
            assert "整组优先" in labels and "⬆ 优先 1" in labels
        finally:
            io.gate.set()
            await inbot.stop()

    async def test_stale_priority_buttons_are_harmless(self, rig):
        inbot, handlers = await rig.boot()
        try:
            for data in ("wms:tr:9:0", "wms:pr:9", "wms:tr:x"):
                assert (await press(handlers, data)).edits == []
        finally:
            await inbot.stop()


class TestSentencesInTheBot:
    async def test_a_phrase_sets_the_priority_and_makes_no_plan(self, rig, monkeypatch):
        inbot, _handlers, io, plan_id, _ = await running(
            rig, monkeypatch, ("abcd00123.mkv", "other.mkv"))
        try:
            before = len(await plans.listing(inbot.embedded.ctx, open_only=False, limit=100))
            run = inbot.embedded.run_of(plan_id)
            text, buttons = await inbot.nl_message(4242, "先下 abcd00123")
            assert "高" in text and "abcd00123.mkv" in text and buttons is None
            assert run.control.tracks[0].priority == 1
            text, _ = await inbot.nl_message(4242, "abcd00123 置顶")
            assert run.control.tracks[0].priority == 2
            await inbot.nl_message(4242, "优先下载 other")
            assert run.control.tracks[1].priority == 1
            after = len(await plans.listing(inbot.embedded.ctx, open_only=False, limit=100))
            assert after == before                              # no new plan, ever
            assert (await levels(inbot, plan_id))[0] == [2, 1]
        finally:
            io.gate.set()
            await inbot.stop()

    async def test_nothing_matching_says_so(self, rig):
        inbot, _handlers = await rig.boot()
        try:
            text, buttons = await inbot.nl_message(4242, "先下 nosuchfile")
            assert text == "没有找到正在排队或下载的 nosuchfile" and buttons is None
            assert not await plans.listing(inbot.embedded.ctx, open_only=False, limit=100)
        finally:
            await inbot.stop()

    async def test_a_waiting_plan_is_found_too(self, rig, monkeypatch):
        from test_wms_m92_bot import media_files, plan_of

        media_files(rig.drive, ("abcd00123.mkv",))
        inbot, _handlers = await rig.boot()
        try:
            plan_id = await plan_of(inbot, ("abcd00123.mkv",))
            text, _ = await inbot.nl_message(4242, "abcd00123 置顶")
            assert "置顶" in text
            assert (await levels(inbot, plan_id))[0] == [2]
        finally:
            await inbot.stop()


class TestDownloadsQueue:
    async def test_downloads_lists_the_queue_in_start_order(self, rig, monkeypatch):
        monkeypatch.setenv("OUTBOUND_PARALLEL_FILES", "1")
        inbot, handlers, io, plan_id, _ = await running(
            rig, monkeypatch, ("a.mkv", "b.mkv", "c.mkv"), started=1)
        try:
            run = inbot.embedded.run_of(plan_id)
            run.control.set_priority(2, 2)
            run.control.set_priority(1, 1)
            await asyncio.sleep(0.05)
            event = Event("/downloads")
            await handlers.on_downloads(event)
            text = event.replies[0][0]
            assert "排队中，按开始顺序（2）" in text
            assert text.index("1. ⬆⬆ c.mkv") < text.index("2. ⬆ b.mkv")
        finally:
            io.gate.set()
            await inbot.stop()
