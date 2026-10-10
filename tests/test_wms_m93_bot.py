"""M9.3 §A in the bot: the line and the buttons of each task, 暂停 / 开始 / 终止, the
confirm before a partial file is deleted, the plan-level buttons, the old 取消."""

from __future__ import annotations

import asyncio

from test_wms_m83 import payload
from test_wms_m83_bot import buttons_of, press, until
from test_wms_m92 import CdnIO
from test_wms_m92_bot import chinese, media_files, plan_of, rig, serve_with  # noqa: F401


async def running(rig, monkeypatch, names, *, hold_after=2, started=None):
    media_files(rig.drive, names)
    io = CdnIO(payload(2000))
    io.gate, io.hold_after = asyncio.Event(), hold_after
    serve_with(monkeypatch, io)
    inbot, handlers = await rig.boot()
    plan_id = await plan_of(inbot, names)
    pressed = await press(handlers, f"wms:apply:{plan_id}")
    await until(lambda: len(io.begun) == (len(names) if started is None else started))
    await asyncio.sleep(0.05)
    return inbot, handlers, io, plan_id, pressed


def labels(inbot, plan_id):
    run = inbot.embedded.run_of(plan_id)
    return [label for label, _ in buttons_of({"buttons": inbot.run_buttons(run)})]


class TestTheLines:
    async def test_each_task_has_a_line_and_the_states_have_their_names(self, rig, monkeypatch):
        inbot, _handlers, io, plan_id, _ = await running(rig, monkeypatch, ("a.mkv", "b.mkv"))
        try:
            run = inbot.embedded.run_of(plan_id)
            assert run.control.pause(0)
            await until(lambda: run.control.tracks[0].state == "paused")
            text = inbot.progress_text(run)
            assert "1. a.mkv · 已暂停" in text and "2. b.mkv · 下载中" in text
            assert "已暂停 1" in text
            assert text.index("2. b.mkv") < text.index("1. a.mkv")      # running ones first
            assert run.control.cancel(1)
            await until(lambda: run.control.tracks[1].state == "cancelled")
            text = inbot.progress_text(run)
            assert "2. b.mkv · 已终止" in text
        finally:
            io.gate.set()
            await inbot.stop()

    async def test_the_buttons_follow_the_state(self, rig, monkeypatch):
        inbot, _handlers, io, plan_id, _ = await running(rig, monkeypatch, ("a.mkv", "b.mkv"))
        try:
            assert labels(inbot, plan_id)[:6] == [
                "⏸ 暂停 1 a.mkv", "■ 终止 1", "⬆ 优先 1", "⏸ 暂停 2 b.mkv", "■ 终止 2", "⬆ 优先 2"]
            run = inbot.embedded.run_of(plan_id)
            run.control.pause(0)
            await until(lambda: run.control.tracks[0].state == "paused")
            assert "▶ 开始 1 a.mkv" in labels(inbot, plan_id)
            assert {"全部暂停", "全部开始"} <= set(labels(inbot, plan_id))
            assert "重试失败的" not in labels(inbot, plan_id)
            run.control.cancel(1)
            await until(lambda: run.control.tracks[1].state == "cancelled")
            now = labels(inbot, plan_id)
            assert "重试失败的" in now and "🗑 删除 2 b.mkv 已下载部分" in now
        finally:
            io.gate.set()
            await inbot.stop()


class TestThePresses:
    async def test_pause_then_start(self, rig, monkeypatch):
        inbot, handlers, io, plan_id, _ = await running(rig, monkeypatch, ("a.mkv", "b.mkv"))
        try:
            run = inbot.embedded.run_of(plan_id)
            paused = await press(handlers, f"wms:tp:{plan_id}:0")
            assert paused.answers[0][0] == "已暂停 a.mkv，已下载的部分保留"
            assert run.control.tracks[0].state == "paused"
            assert "已暂停" in paused.edits[-1][0]
            again = await press(handlers, f"wms:tp:{plan_id}:0")
            assert again.answers[0][0] == "这个文件现在不能这样操作"
            assert again.answers[0][1]["alert"]
            started = await press(handlers, f"wms:ts:{plan_id}:0")
            assert started.answers[0][0] == "已开始 a.mkv"
            io.gate.set()
            await inbot.settle()
            assert (rig.tmp_path / "media" / "a.mkv").read_bytes() == payload(2000)
        finally:
            io.gate.set()
            await inbot.stop()

    async def test_stop_keeps_the_part_until_the_delete_is_confirmed(self, rig, monkeypatch):
        inbot, handlers, io, plan_id, _ = await running(rig, monkeypatch, ("a.mkv", "b.mkv"))
        try:
            media = rig.tmp_path / "media"
            stopped = await press(handlers, f"wms:tx:{plan_id}:0")
            assert stopped.answers[0][0].startswith("已终止 a.mkv，已下载的部分保留")
            assert "已终止" in stopped.edits[-1][0]
            assert (media / "a.mkv.part").exists()
            ask = await press(handlers, f"wms:td:{plan_id}:0")
            assert "要删除 a.mkv 已下载的部分吗" in ask.edits[0][0]
            assert [label for label, _ in buttons_of(ask.edits[0][1])] == ["确认删除", "保留"]
            assert (media / "a.mkv.part").exists()                  # asking deletes nothing
            kept = await press(handlers, f"wms:tn:{plan_id}:0")
            assert (media / "a.mkv.part").exists() and kept.edits
            sure = await press(handlers, f"wms:ty:{plan_id}:0")
            assert sure.answers[0][0] == "已删除 a.mkv 已下载的部分"
            assert not (media / "a.mkv.part").exists()
            nothing = await press(handlers, f"wms:ty:{plan_id}:0")
            assert nothing.answers[0][1]["alert"]
        finally:
            io.gate.set()
            await inbot.stop()

    async def test_the_delete_button_is_still_there_when_the_plan_has_ended(
            self, rig, monkeypatch):
        inbot, handlers, io, plan_id, pressed = await running(
            rig, monkeypatch, ("a.mkv", "b.mkv"))
        try:
            await press(handlers, f"wms:tx:{plan_id}:0")
            io.gate.set()
            await inbot.settle()
            _text, kwargs = pressed.edits[-1]
            assert ("🗑 删除 1 a.mkv 已下载部分", f"wms:td:{plan_id}:0".encode()) in buttons_of(
                kwargs)
            sure = await press(handlers, f"wms:ty:{plan_id}:0")
            assert sure.answers[0][0] == "已删除 a.mkv 已下载的部分"
            assert not (rig.tmp_path / "media" / "a.mkv.part").exists()
        finally:
            io.gate.set()
            await inbot.stop()

    async def test_the_old_cancel_button_still_works(self, rig, monkeypatch):
        inbot, handlers, io, plan_id, _ = await running(rig, monkeypatch, ("a.mkv", "b.mkv"))
        try:
            old = await press(handlers, f"wms:cancel:{plan_id}:1")
            assert old.answers[0][0].startswith("已终止 b.mkv")
            assert inbot.embedded.run_of(plan_id).control.tracks[1].cancel_requested
        finally:
            io.gate.set()
            await inbot.stop()

    async def test_pause_all_start_all_and_retry(self, rig, monkeypatch):
        inbot, handlers, io, plan_id, _ = await running(
            rig, monkeypatch, ("a.mkv", "b.mkv", "c.mkv"))
        try:
            run = inbot.embedded.run_of(plan_id)
            paused = await press(handlers, f"wms:pall:{plan_id}")
            assert paused.answers[0][0] == "已暂停 3 个文件"
            await until(lambda: run.control.counts()["paused"] == 3)
            await press(handlers, f"wms:tx:{plan_id}:2")
            io.gate.set()
            started = await press(handlers, f"wms:sall:{plan_id}")
            assert started.answers[0][0] == "已开始 2 个文件"
            await inbot.settle()
            media = rig.tmp_path / "media"
            assert (media / "a.mkv").exists() and (media / "b.mkv").exists()
            assert not (media / "c.mkv").exists()
            retry = await press(handlers, f"wms:retry:{plan_id}")
            assert retry.answers[0][0] == "开始执行"
        finally:
            io.gate.set()
            await inbot.stop()

    async def test_stale_task_buttons_are_harmless(self, rig):
        inbot, handlers = await rig.boot()
        try:
            for data in ("wms:tp:9:0", "wms:ts:9:0", "wms:tx:9:0", "wms:td:9:0", "wms:ty:9:0",
                         "wms:pall:9", "wms:sall:9", "wms:tp:x"):
                event = await press(handlers, data)
                assert event.edits == []
        finally:
            await inbot.stop()
