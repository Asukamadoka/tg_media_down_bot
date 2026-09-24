"""WMS M5: /wms in the bot, and shelving what lands in PikPak.

CC_BRIEF §5 M5: after a magnet, a share link or Telegram media goes into
PikPak, the rules produce a plan; by default the admin gets it with a
confirm button, or it is applied at once with WMS_AUTO_SHELVE=apply.
"""

from __future__ import annotations

import asyncio

import pytest
from wms_fakes import FakeDrive

from tgmd import i18n, wms
from tgmd.config import Config, ConfigError, load_config
from tgmd.handlers import BotHandlers
from tgmd.tasks import Job, JobKind, JobQueue, JobState, saved_to_pikpak

ADMIN, MEMBER = 4242, 777
BIG = 200 * 1024**2
RULES = """
rules:
  - name: shows
    scope: /Inbox
    match:
      kind: file
      name_regex: '(?P<show>.+?)\\.S(?P<s>\\d{2})E(?P<e>\\d{2})'
    actions:
      - rename: {template: '{show|title}.S{s}E{e}.{ext}'}
      - move: {to: '/Media/{show|title}/S{s}'}
"""


@pytest.fixture(autouse=True)
def english():
    previous = i18n.language()
    i18n.set_language("en")
    yield
    i18n.set_language(previous)


class FakeService:
    def __init__(self, drive, *, sessions=()):
        self.drive = drive
        self.sessions = set(sessions)

    async def has_user_session(self, user_id):
        return user_id in self.sessions

    async def client(self, user_id=None):
        return self.drive


class Sent:
    def __init__(self):
        self.messages: list[tuple[int, str, object]] = []

    async def __call__(self, chat_id, text, buttons=None):
        self.messages.append((chat_id, text, buttons))


@pytest.fixture
async def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "wms.yaml").write_text("ratelimit: {requests_per_second: 100000, burst: 100000}\n")
    (tmp_path / "rules.yaml").write_text(RULES)
    drive = FakeDrive()
    config = Config()
    config.access.admin_user_ids = [ADMIN]
    config.access.allowed_user_ids = [MEMBER]
    config.wms.enabled = True
    inbot = wms.WmsInBot(config, FakeService(drive))
    inbot.shelve_delay = 0.05
    sent = Sent()
    inbot.attach_notifier(sent)
    await inbot.start()
    try:
        yield config, drive, inbot, sent
    finally:
        await inbot.stop()


def job(*, user=ADMIN, chat=ADMIN, kind=JobKind.URL, mode="pikpak", state=JobState.DONE):
    return Job(id=1, user_id=user, chat_id=chat, mode=mode, kind=kind, label="x",
               url="magnet:?xt=urn:btih:abc", state=state)


# -------------------------------------------------------------------- hook


class TestHook:
    def test_which_jobs_count_as_saved_to_pikpak(self):
        assert saved_to_pikpak(job(kind=JobKind.URL, mode="telegram"))
        assert saved_to_pikpak(job(kind=JobKind.SHARE, mode="local"))
        assert saved_to_pikpak(job(kind=JobKind.MESSAGE, mode="pikpak", state=JobState.PARTIAL))
        assert not saved_to_pikpak(job(kind=JobKind.MESSAGE, mode="telegram"))
        assert not saved_to_pikpak(job(kind=JobKind.URL, state=JobState.FAILED))

    async def test_the_queue_calls_it_after_a_pikpak_job_and_survives_its_errors(
        self, caplog
    ):
        called = []

        async def hook(done):
            called.append(done.id)
            raise RuntimeError("shelving broke")

        queue = JobQueue(config=Config(), db=object(), bot=object(), resolver=object(),
                         downloader=object(), bot_downloader=object(), delivery=object(),
                         pikpak=object(), after_pikpak=hook)

        async def succeed(this, _reporter):
            this.state = JobState.DONE

        queue._run_url_job = succeed  # noqa: SLF001 - the runner is not what is tested
        await queue._run(job())  # noqa: SLF001
        assert called == [1]
        assert "after-PikPak hook failed" in caplog.text


# ---------------------------------------------------------------- shelving


async def settle(inbot) -> None:
    for _ in range(100):
        task = inbot._shelve_task  # noqa: SLF001
        if task is None or task.done():
            return
        await asyncio.sleep(0.02)


class TestShelving:
    async def test_a_batch_becomes_one_plan_with_a_confirm_button(self, world):
        _config, drive, inbot, sent = world
        drive.add("/Inbox/lost.S01E01.mkv", size=BIG)
        drive.add("/Inbox/lost.S01E02.mkv", size=BIG)
        await inbot.pikpak_saved(job(chat=100))
        await inbot.pikpak_saved(job(chat=200))
        await settle(inbot)
        assert sorted(chat for chat, _, _ in sent.messages) == [100, 200]
        _, text, buttons = sent.messages[0]
        assert text.startswith("📦 New files in PikPak")
        assert "Lost.S01E01.mkv" in text
        data = [b.type.data for row in buttons.rows for b in row.buttons]
        assert data == [b"wms:apply:1", b"wms:discard:1"]
        # Nothing was changed: it is only a plan.
        assert drive.id_at("/Inbox/lost.S01E01.mkv")

    async def test_apply_mode_shelves_at_once(self, world):
        config, drive, inbot, sent = world
        config.wms.auto_shelve = "apply"
        drive.add("/Inbox/lost.S01E01.mkv", size=BIG)
        await inbot.pikpak_saved(job())
        await settle(inbot)
        assert drive.id_at("/Media/Lost/S01/Lost.S01E01.mkv")
        (_, text, buttons), = sent.messages
        assert "were shelved" in text and "3 applied" in text and buttons is None

    async def test_nothing_to_shelve_sends_nothing(self, world):
        config, drive, inbot, sent = world
        config.pikpak.folder = "/Inbox"
        drive.add("/Inbox/readme.txt", size=1)
        await inbot.pikpak_saved(job())
        await settle(inbot)
        assert sent.messages == []

    async def test_a_landing_folder_no_rule_covers_is_pointed_out_once(self, world):
        config, drive, inbot, sent = world
        config.pikpak.folder = "/TelegramMedia"
        drive.add("/TelegramMedia/lost.S01E01.mkv", size=BIG)
        await inbot.pikpak_saved(job())
        await settle(inbot)
        (_, text, _), = sent.messages
        assert "/TelegramMedia" in text and "PIKPAK_FOLDER" in text
        await inbot.pikpak_saved(job())
        await settle(inbot)
        assert len(sent.messages) == 1

    def test_scope_coverage(self):
        assert wms.covers("/", "/TelegramMedia")
        assert wms.covers("/Inbox", "/Inbox/sub")
        assert not wms.covers("/Inbox", "/Inboxes")

    async def test_off_and_other_peoples_drives_are_left_alone(self, world):
        config, drive, inbot, sent = world
        drive.add("/Inbox/lost.S01E01.mkv", size=BIG)
        inbot.pikpak.sessions = {MEMBER}  # a member with their own drive
        await inbot.pikpak_saved(job(user=MEMBER))
        config.wms.auto_shelve = "off"
        await inbot.pikpak_saved(job())
        await settle(inbot)
        assert sent.messages == [] and inbot._shelve_task is None  # noqa: SLF001

    def test_the_setting(self, monkeypatch):
        assert load_config(None).wms.auto_shelve == "plan"
        monkeypatch.setenv("WMS_AUTO_SHELVE", "Apply")
        assert load_config(None).wms.auto_shelve == "apply"
        monkeypatch.setenv("WMS_AUTO_SHELVE", "sometimes")
        config = load_config(None)
        config.telegram.api_id, config.telegram.api_hash = 1, "h"
        config.telegram.bot_token = "1:x"
        with pytest.raises(ConfigError, match="WMS_AUTO_SHELVE"):
            config.validate()


# -------------------------------------------------------------------- /wms


class Event:
    def __init__(self, text="/wms", user_id=ADMIN, *, data=b""):
        self.raw_text = text
        self.sender_id = self.chat_id = user_id
        self.is_private = True
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


def bot_for(config, inbot) -> BotHandlers:
    handlers = BotHandlers(bot=object(), config=config, db=object(), queue=object(),
                           pikpak=object(), portal=object())
    handlers.attach_wms(inbot, None)
    return handlers


async def say(handlers, text, user=ADMIN) -> list[tuple[str, dict]]:
    event = Event(text, user)
    await handlers.on_wms(event)
    return event.replies


class TestCommands:
    async def test_stocktake_plan_apply(self, world):
        config, drive, inbot, _ = world
        drive.add("/Inbox/lost.S01E01.mkv", size=BIG)
        handlers = bot_for(config, inbot)

        replies = await say(handlers, "/wms stocktake")
        assert "Incremental stocktake" in replies[-1][0]

        replies = await say(handlers, "/wms plan")
        text, kwargs = replies[-1]
        assert "Plan 1 (organize): 3 action(s)" in text and kwargs["buttons"] is not None

        replies = await say(handlers, "/wms plan 1")
        assert "Plan 1" in replies[-1][0]

        replies = await say(handlers, "/wms apply 1")
        assert "Plan 1: 3 applied" in replies[-1][0]
        assert drive.id_at("/Media/Lost/S01/Lost.S01E01.mkv")

        replies = await say(handlers, "/wms apply 1")
        assert "Warehouse:" in replies[-1][0] and "applied" in replies[-1][0]

    async def test_undo_asks_first(self, world):
        config, drive, inbot, _ = world
        drive.add("/Inbox/lost.S01E01.mkv", size=BIG)
        handlers = bot_for(config, inbot)
        await say(handlers, "/wms plan")
        await say(handlers, "/wms apply 1")
        entries = await inbot.embedded.audit()
        move = next(e for e in entries if e["action"] == "move")
        text, kwargs = (await say(handlers, f"/wms undo {move['id']}"))[-1]
        assert "would be undone" in text
        (button,) = kwargs["buttons"].rows[0].buttons
        assert button.type.data == f"wms:undo:{move['id']}".encode()
        assert drive.id_at("/Media/Lost/S01/Lost.S01E01.mkv")  # not yet

    async def test_rules_help_and_bad_ids(self, world):
        config, _drive, inbot, _ = world
        handlers = bot_for(config, inbot)
        text = (await say(handlers, "/wms rules"))[-1][0]
        assert "<b>shows</b> [organize] /Inbox: rename, move" in text
        assert "rules.yaml" in text
        assert "/wms stocktake" in (await say(handlers, "/wms help"))[-1][0]
        assert "is not an id" in (await say(handlers, "/wms apply seven"))[-1][0]

    async def test_members_and_off(self, world):
        config, _drive, inbot, _ = world
        handlers = bot_for(config, inbot)
        assert "Only admins" in (await say(handlers, "/wms plan", MEMBER))[-1][0]
        await inbot.stop()
        assert "WMS_ENABLED" in (await say(handlers, "/wms plan"))[-1][0]


class TestButtons:
    async def press(self, handlers, data, user=ADMIN) -> Event:
        event = Event(user_id=user, data=data)
        await handlers.handle_wms_button(event)
        return event

    async def test_apply_edits_the_message_and_drops_the_buttons(self, world):
        config, drive, inbot, _ = world
        drive.add("/Inbox/lost.S01E01.mkv", size=BIG)
        handlers = bot_for(config, inbot)
        await say(handlers, "/wms plan")
        event = await self.press(handlers, b"wms:apply:1")
        (text, kwargs), = event.edits
        assert "Plan 1: 3 applied" in text and kwargs["buttons"] is None
        # A second press (an old message) is refused with the reason.
        again = await self.press(handlers, b"wms:apply:1")
        assert again.edits == [] and "applied" in again.answers[0][0]

    async def test_discard_and_undo(self, world):
        config, drive, inbot, _ = world
        drive.add("/Inbox/lost.S01E01.mkv", size=BIG)
        handlers = bot_for(config, inbot)
        await say(handlers, "/wms plan")
        event = await self.press(handlers, b"wms:discard:1")
        assert "Plan 1 discarded" in event.edits[0][0]

        drive.add("/Inbox/lost.S01E02.mkv", size=BIG)
        await say(handlers, "/wms plan")
        await self.press(handlers, b"wms:apply:2")
        rename = next(e for e in await inbot.embedded.audit() if e["action"] == "rename")
        event = await self.press(handlers, f"wms:undo:{rename['id']}".encode())
        # The rename was followed by a move, so undoing it alone is refused.
        assert event.edits == [] and "changed" in event.answers[0][0]
        move = next(e for e in await inbot.embedded.audit() if e["action"] == "move"
                    and "E02" in e["what"])
        event = await self.press(handlers, f"wms:undo:{move['id']}".encode())
        assert event.edits[0][0].startswith("Undone:")

    async def test_only_admins_may_press(self, world):
        config, _drive, inbot, _ = world
        handlers = bot_for(config, inbot)
        event = await self.press(handlers, b"wms:apply:1", user=MEMBER)
        assert "Only admins" in event.answers[0][0] and event.answers[0][1]["alert"]

    async def test_garbage_data_is_ignored(self, world):
        config, _drive, inbot, _ = world
        handlers = bot_for(config, inbot)
        event = await self.press(handlers, b"wms:apply:NaN")
        assert event.answers == [(None, {})] and event.edits == []


# ---------------------------------------------------------- end to end


async def test_a_magnet_link_ends_up_where_the_rules_say(world, tmp_path):
    """M5's acceptance, minus the phone: a magnet goes through the real job
    queue into (fake) PikPak, and the rules shelve what arrived."""
    from test_tasks import FakePikPak, Harness

    from tgmd.db import Database
    from tgmd.pikpak import OfflineTask

    config, drive, inbot, sent = world
    config.wms.auto_shelve = "apply"

    class Arrives(FakePikPak):
        async def offline_download(self, url, *, folder=None, name=None, user_id=None):
            file_id = drive.add("/Inbox/lost.S01E01.mkv", size=BIG)
            return OfflineTask(task_id="t", file_id=file_id, name="lost.S01E01.mkv")

    db = Database(tmp_path / "bot.sqlite3")
    await db.connect()
    try:
        harness = Harness(tmp_path, db, pikpak=Arrives(), after_pikpak=inbot.pikpak_saved)
        await harness.queue.start()
        job = await harness.job("pikpak", JobKind.URL, url="magnet:?xt=urn:btih:abc")
        job.user_id = ADMIN
        row = await harness.run(job)
        assert row["status"] == "done"
        await settle(inbot)
        await harness.queue.stop()
    finally:
        await db.close()

    assert drive.id_at("/Media/Lost/S01/Lost.S01E01.mkv")
    assert "were shelved" in sent.messages[0][1]
