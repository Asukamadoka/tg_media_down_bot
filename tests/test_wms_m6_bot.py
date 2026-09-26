"""WMS M6 in the bot: /do, plain sentences from admins, and the three buttons."""

from __future__ import annotations

import pytest
from wms_fakes import FakeDrive

from tgmd import handlers as handlers_module
from tgmd import i18n, wms
from tgmd.config import Config
from tgmd.handlers import BotHandlers

ADMIN, MEMBER = 4242, 777
GB = 1024**3


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
    def __init__(self, text="", user_id=ADMIN, *, data=b"", private=True):
        self.raw_text = text
        self.sender_id = self.chat_id = user_id
        self.is_private = private
        self.message = object()
        self.data = data
        self.replies: list[tuple[str, dict]] = []
        self.answers: list[tuple[str | None, dict]] = []
        self.edits: list[tuple[str, dict]] = []

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return self

    async def answer(self, message=None, **kwargs):
        self.answers.append((message, kwargs))

    async def edit(self, text, **kwargs):
        self.edits.append((text, kwargs))


@pytest.fixture
async def bot(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "wms.yaml").write_text("ratelimit: {requests_per_second: 100000, burst: 100000}\n")
    monkeypatch.setattr(handlers_module, "has_downloadable_media", lambda _m: False)
    drive = FakeDrive()
    drive.add("/Inbox/lost.S01E01.mkv", size=2 * GB)
    drive.add("/Inbox/cover.jpg", size=1)
    config = Config()
    config.access.admin_user_ids = [ADMIN]
    config.access.allowed_user_ids = [MEMBER]
    config.wms.enabled = True
    inbot = wms.WmsInBot(config, FakeService(drive))
    await inbot.start()
    handlers = BotHandlers(bot=object(), config=config, db=object(), queue=object(),
                           pikpak=object(), portal=object())
    handlers.attach_wms(inbot, None)
    try:
        yield handlers, inbot, drive, config
    finally:
        await inbot.stop()


def buttons_of(kwargs) -> list[tuple[str, bytes]]:
    markup = kwargs.get("buttons")
    if markup is None:
        return []
    return [(b.text, b.type.data) for row in markup.rows for b in row.buttons]


async def send(handlers, text, user=ADMIN, *, private=True) -> Event:
    event = Event(text, user, private=private)
    if text.startswith("/do"):
        await handlers.on_do(event)
    else:
        await handlers.on_message(event)
    return event


async def press(handlers, data: str, user=ADMIN) -> Event:
    event = Event(user_id=user, data=data.encode())
    await handlers.handle_wms_button(event)
    return event


class TestDo:
    async def test_the_users_sentence_gets_a_plan_with_three_buttons(self, bot):
        handlers, _inbot, _drive, _config = bot
        event = await send(handlers, "/do 下载所有大于1GB的视频")
        text, kwargs = event.replies[0]
        assert text.startswith("计划如下（由 rules 理解）。现在还什么都没改。")
        assert "理解为：下载到 NAS" in text and "命中 1 个文件，共 2.0 GiB" in text
        assert [label for label, _ in buttons_of(kwargs)] == ["确认执行", "修改", "取消"]
        assert buttons_of(kwargs)[0][1] == b"wms:nl:apply:1"

    async def test_plain_text_from_an_admin_is_a_sentence(self, bot):
        handlers, _inbot, drive, _config = bot
        event = await send(handlers, "把/Inbox里的图片移到/Media/图片")
        text, kwargs = event.replies[0]
        assert "移动    /Inbox/cover.jpg  →  /Media/图片/cover.jpg" in text
        confirm = buttons_of(kwargs)[0][1].decode()
        pressed = await press(handlers, confirm)
        assert "执行 2" in pressed.edits[0][0]  # the folder, then the move
        assert pressed.edits[0][1]["buttons"] is None
        assert drive.id_at("/Media/图片/cover.jpg")
        # The same button again: the proposal is used up.
        again = await press(handlers, confirm)
        assert again.edits == [] and "过期" in again.answers[0][0]

    async def test_members_and_groups_keep_the_old_behaviour(self, bot):
        handlers, _inbot, _drive, _config = bot
        member = await send(handlers, "下载所有大于1GB的视频", MEMBER)
        group = await send(handlers, "下载所有大于1GB的视频", private=False)
        assert "计划如下" not in member.replies[0][0]
        assert group.replies == []

    async def test_with_wms_off_plain_text_is_just_text(self, bot):
        handlers, inbot, _drive, _config = bot
        await inbot.stop()
        event = await send(handlers, "下载所有大于1GB的视频")
        assert "计划如下" not in event.replies[0][0]
        do = await send(handlers, "/do 下载所有大于1GB的视频")
        assert "WMS_ENABLED" in do.replies[0][0]

    async def test_do_alone_explains_itself(self, bot):
        handlers, *_ = bot
        event = await send(handlers, "/do")
        assert "<code>/do 下载今天转存到网盘的所有大于1GB的视频</code>" in event.replies[0][0]

    async def test_a_pikpak_failure_is_reported_not_raised(self, bot):
        handlers, _inbot, drive, _config = bot
        from pikpakapi.PikpakException import PikpakException

        drive.fail_next.extend([PikpakException("invalid_grant")] * 3)
        event = await send(handlers, "把/Inbox里的图片移到/Temp")
        assert event.replies[0][0].startswith("仓储：")

    async def test_not_understood(self, bot):
        handlers, *_ = bot
        event = await send(handlers, "今天天气怎么样")
        assert event.replies[0][0].startswith("没听懂")


class TestConversation:
    async def test_a_question_then_the_answer_is_merged(self, bot):
        handlers, *_ = bot
        asked = await send(handlers, "把/Inbox里的图片移动")
        assert "移到哪里" in asked.replies[0][0]
        answered = await send(handlers, "移到/Media/图片")
        text, kwargs = answered.replies[0]
        assert "移动到 /Media/图片" in text and buttons_of(kwargs)

    async def test_edit_then_cancel(self, bot):
        handlers, inbot, _drive, _config = bot
        first = await send(handlers, "把/Inbox里的图片移到/Temp")
        edit = buttons_of(first.replies[0][1])[1]
        pressed = await press(handlers, edit[1].decode())
        assert "把/Inbox里的图片移到/Temp" in pressed.edits[0][0]
        second = await send(handlers, "大于1GB的")
        # Merged: the size joins the original sentence; the earlier plan is dropped.
        assert "大小不小于 1.0 GiB" in second.replies[0][0]
        assert (await inbot.embedded.status())["open_plans"] == 0  # nothing ≥1GB there
        third = await send(handlers, "把/Inbox里的图片移到/Temp")
        cancel_data = buttons_of(third.replies[0][1])[2][1].decode()
        cancelled = await press(handlers, cancel_data)
        assert cancelled.edits[0][0] == "已取消，什么都没改。"
        assert (await inbot.embedded.status())["open_plans"] == 0

    async def test_someone_elses_proposal_cannot_be_pressed(self, bot):
        handlers, _inbot, _drive, config = bot
        config.access.admin_user_ids.append(99)
        first = await send(handlers, "把/Inbox里的图片移到/Temp")
        confirm = buttons_of(first.replies[0][1])[0][1].decode()
        other = await press(handlers, confirm, user=99)
        assert other.edits == [] and "过期" in other.answers[0][0]


class TestScheduled:
    async def test_confirming_a_schedule_writes_the_rule_and_schedules_it(self, bot, tmp_path):
        handlers, inbot, _drive, _config = bot
        event = await send(handlers, "每天晚上11点删除/Temp里超过7天的文件")
        text, kwargs = event.replies[0]
        assert "将写入规则文件的规则" in text and "0 23 * * *" in text
        pressed = await press(handlers, buttons_of(kwargs)[0][1].decode())
        assert "已写入" in pressed.edits[0][0]
        rules = (tmp_path / "rules.yaml").read_text(encoding="utf-8")
        assert "每天晚上11点删除/Temp里超过7天的文件" in rules  # the sentence, as a comment
        assert any(job.startswith("rule:nl-") for job in inbot.embedded.scheduled())


class TestScheduledPlansReachTheAdmins:
    async def test_a_waiting_plan_is_announced_once(self, bot):
        handlers, inbot, *_ = bot
        sent = []

        async def notify(chat, text, buttons=None):
            sent.append((chat, text, buttons))

        inbot.attach_notifier(notify)
        await inbot.job_finished(await inbot.embedded.run_job("stocktake"))
        assert sent == []  # no plan, nothing to say

        await send(handlers, "每天晚上11点把/Inbox里的图片移到/Media/图片")
        await press(handlers, "wms:nl:apply:1")
        (job,) = [name for name in inbot.embedded.scheduled() if name.startswith("rule:")]
        # What the scheduler does at 23:00: run the rule, report the result.
        scheduler = inbot.embedded._scheduler  # noqa: SLF001 - the scheduler's own hook
        result = await scheduler.run_rule(job.removeprefix("rule:"))
        assert result.plan_id is not None and result.report is None  # planned, not applied
        (chat, text, buttons), = sent
        assert chat == ADMIN and "生成了一份计划" in text
        assert buttons_of({"buttons": buttons})[0][1] == f"wms:apply:{result.plan_id}".encode()
        await inbot.job_finished(result)  # the same plan again
        assert len(sent) == 1
