"""WMS M8.3 §G in the bot: feedback at once, progress, no double runs, [stop], restarts."""

from __future__ import annotations

import asyncio

import pytest
from test_wms_m83 import FakeIO, SlowIO, payload
from wms_fakes import FakeDrive

from pikpak_wms.ops import embed, outbound, plans
from tgmd import handlers as handlers_module
from tgmd import i18n, wms
from tgmd.config import Config
from tgmd.handlers import BotHandlers

ADMIN = 4242


@pytest.fixture(autouse=True)
def chinese():
    previous = i18n.language()
    i18n.set_language("zh")
    yield
    i18n.set_language(previous)


class FakeService:
    def __init__(self, drive):
        self.drive = drive

    async def has_user_session(self, user_id):
        return False

    async def client(self, user_id=None):
        return self.drive


class Event:
    def __init__(self, text="", *, data=b""):
        self.raw_text = text
        self.sender_id = self.chat_id = ADMIN
        self.is_private = True
        self.message = object()
        self.data = data
        self.replies, self.answers, self.edits = [], [], []

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return self

    async def answer(self, message=None, **kwargs):
        self.answers.append((message, kwargs))

    async def edit(self, text, **kwargs):
        self.edits.append((text, kwargs))


def buttons_of(kwargs):
    markup = kwargs.get("buttons")
    return [] if markup is None else [(b.text, b.type.data) for r in markup.rows for b in r.buttons]


class Rig:
    """One 'process': a WmsInBot on the shared data directory, plus handlers."""

    def __init__(self, drive, tmp_path):
        self.drive, self.tmp_path = drive, tmp_path

    async def boot(self, notify=None):
        config = Config()
        config.access.admin_user_ids = [ADMIN]
        config.wms.enabled = True
        inbot = wms.WmsInBot(config, FakeService(self.drive))
        inbot.progress_every, inbot.min_gap, inbot.tick = 0.05, 0.0, 0.01
        if notify is not None:
            inbot.attach_notifier(notify)
        await inbot.start()
        handlers = BotHandlers(bot=object(), config=config, db=object(), queue=object(),
                               pikpak=object(), portal=object())
        handlers.attach_wms(inbot, None)
        return inbot, handlers


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "wms.yaml").write_text(
        "ratelimit: {requests_per_second: 100000, burst: 100000}\n"
        "schedule: {builtin: false}\n"
        f"outbound: {{downloader: local, local_dir: {tmp_path / 'media'}}}\n")
    monkeypatch.setattr(handlers_module, "has_downloadable_media", lambda _m: False)
    drive = FakeDrive()
    drive.add("/Media/a.mkv", size=2000)
    return Rig(drive, tmp_path)


def serve_with(monkeypatch, io):
    """The bot's own deliver, with the network replaced by ``io``."""
    real = outbound.make_deliver
    monkeypatch.setattr(embed.outbound, "make_deliver",
                        lambda ctx, progress=None, **kw: real(ctx, io=io, progress=progress))


async def waiting_plan(inbot) -> int:
    ctx = inbot.embedded.ctx
    from pikpak_wms.ops.stocktake import stocktake

    await stocktake(ctx.client, ctx.store, full=True)
    return await plans.save(ctx, await outbound.plan_paths(ctx, ["/Media/a.mkv"]))


async def press(handlers, data: str) -> Event:
    event = Event(data=data.encode())
    await handlers.handle_wms_button(event)
    return event


async def until(condition, seconds=5.0):
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.01)


class TestConfirmingARun:
    async def test_feedback_progress_refusal_stop_and_resume(self, rig, monkeypatch):
        io = SlowIO(payload(2000))
        serve_with(monkeypatch, io)
        inbot, handlers = await rig.boot()
        try:
            plan_id = await waiting_plan(inbot)
            pressed = await asyncio.wait_for(press(handlers, f"wms:apply:{plan_id}"), 1)
            # Answered at once, the message is a progress display with only [stop].
            assert pressed.answers[0][0] == "开始执行"
            assert f"计划 {plan_id} 执行中：已完成 0/1" in pressed.edits[0][0]
            assert buttons_of(pressed.edits[0][1]) == [("停止", f"wms:stop:{plan_id}".encode())]
            # The plan list says so.
            (row,) = await inbot.embedded.open_plans()
            assert row["status_text"] == "执行中"
            # Pressing again is refused, not repeated.
            again = await press(handlers, f"wms:apply:{plan_id}")
            assert "正在执行" in again.answers[0][0] and again.answers[0][1]["alert"]
            assert again.edits == []
            # The message follows the download.
            await asyncio.wait_for(io.started.wait(), 5)
            await until(lambda: any("a.mkv" in text for text, _ in pressed.edits))
            # [stop]
            stopped = await press(handlers, f"wms:stop:{plan_id}")
            assert "正在停止" in stopped.answers[0][0]
            await inbot.settle()
            text, kwargs = pressed.edits[-1]
            assert f"计划 {plan_id} 已停止于 0/1" in text
            assert [label for label, _ in buttons_of(kwargs)] == ["继续", "丢弃"]
            (row,) = await inbot.embedded.open_plans()
            assert row["status"] == "pending" and row["status_text"] == "已停止"
            # [continue] finishes it, from the part that is there.
            io.release.set()
            resumed = await press(handlers, f"wms:apply:{plan_id}")
            await inbot.settle()
            assert "已执行 1" in resumed.edits[-1][0] or "1" in resumed.edits[-1][0]
            assert (rig.tmp_path / "media" / "a.mkv").read_bytes() == payload(2000)
            # Nothing is running now: [stop] says so.
            late = await press(handlers, f"wms:stop:{plan_id}")
            assert late.answers[0][1]["alert"]
        finally:
            io.release.set()
            await inbot.stop()

    async def test_a_file_that_cannot_be_fetched_is_listed_with_the_reason(self, rig, monkeypatch):
        class Broken(FakeIO):
            async def probe(self, url):
                raise RuntimeError("no route to host")

        serve_with(monkeypatch, Broken(b"", any_url=True))
        inbot, handlers = await rig.boot()
        try:
            plan_id = await waiting_plan(inbot)
            pressed = await press(handlers, f"wms:apply:{plan_id}")
            await inbot.settle()
            text = pressed.edits[-1][0]
            assert "失败 1" in text and "no route to host" in text  # the action failed, listed
            # The action is counted as failed (as ever); asking again plans it afresh.
            assert (await plans.get(inbot.embedded.ctx, plan_id))["status"] == "applied"
        finally:
            await inbot.stop()


class TestRestarts:
    async def test_the_buttons_of_a_proposal_work_after_a_restart(self, rig):
        first, handlers = await rig.boot()
        sent = await handlers_send(handlers, "把/Media里的视频移到/Temp")
        confirm = buttons_of(sent.replies[0][1])[0][1].decode()
        await first.stop()  # the bot restarts: nothing is kept in memory
        second, handlers2 = await rig.boot()
        try:
            pressed = await press(handlers2, confirm)
            assert pressed.answers[0][0] == "开始执行"
            await second.settle()
            assert rig.drive.id_at("/Temp/a.mkv")
        finally:
            await second.stop()

    async def test_a_proposal_of_another_user_or_unknown_is_expired(self, rig):
        inbot, handlers = await rig.boot()
        try:
            pressed = await press(handlers, "wms:nl:apply:99")
            assert "过期" in pressed.answers[0][0]
        finally:
            await inbot.stop()

    async def test_a_cut_off_run_is_announced_with_a_continue_button(self, rig, monkeypatch):
        io = SlowIO(payload(2000))
        serve_with(monkeypatch, io)
        first, handlers = await rig.boot()
        plan_id = await waiting_plan(first)
        await press(handlers, f"wms:apply:{plan_id}")
        await asyncio.wait_for(io.started.wait(), 5)
        await first.stop()  # the process goes away mid-download
        sent = []

        async def notify(chat, text, buttons=None):
            sent.append((chat, text, buttons))

        second, handlers2 = await rig.boot(notify)
        try:
            ((chat, text, buttons),) = sent
            assert chat == ADMIN and f"计划 {plan_id} 因重启中断，已完成 0/1" in text
            assert buttons_of({"buttons": buttons})[0] == ("继续", f"wms:apply:{plan_id}".encode())
            (row,) = await second.embedded.open_plans()
            assert row["status_text"] == "已中断"
            io.release.set()
            await press(handlers2, f"wms:apply:{plan_id}")
            await second.settle()
            assert (rig.tmp_path / "media" / "a.mkv").exists()
        finally:
            io.release.set()
            await second.stop()


async def handlers_send(handlers, text) -> Event:
    event = Event(text)
    await handlers.on_message(event)
    return event
