"""The cache channel as a place to ask for downloads (docs/wms/M7.2 B).

Seen on the NAS: links and videos posted in the cache channel got no answer,
because a channel post's sender is the channel and the allow list refused it
without a word. The channel also receives the reading account's forwards and
the bot's own uploads, so most of what lands there must never be taken for a
request, or the bot would feed on its own output.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon.tl.types import DocumentAttributeVideo, MessageFwdHeader

from tgmd.config import Config
from tgmd.handlers import BotHandlers
from tgmd.tasks import JobKind

ADMIN = 4242
OTHER_ADMIN = 5151
CACHE = -1003729105427
ELSEWHERE = -1009999999999
LINK = "https://t.me/somechannel/42"
MAGNET = "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01&dn=x"


class Rights:
    def __init__(self, *, admin=False, creator=False):
        self.is_admin = admin
        self.is_creator = creator


class FakeBot:
    def __init__(self, *, admin_runs_it: bool = True) -> None:
        self.admin_runs_it = admin_runs_it
        self.permission_checks = 0
        self.dms: list[tuple[int, str]] = []

    async def get_permissions(self, chat_id, who):
        self.permission_checks += 1
        if who == "me":
            return Rights(admin=True)
        return Rights(creator=self.admin_runs_it and who == ADMIN)

    async def send_message(self, chat_id, text, **_kwargs):
        self.dms.append((chat_id, text))

    async def get_me(self):
        return SimpleNamespace(first_name="Media Bot", username="media_bot")


class FakeQueue:
    def __init__(self) -> None:
        self.jobs = []

    async def submit(self, job):
        self.jobs.append(job)


class FakeDb:
    def __init__(self) -> None:
        self.recorded: list[tuple[int, str, str]] = []

    async def record_job(self, user_id, link, mode):
        self.recorded.append((user_id, link, mode))
        return len(self.recorded)

    async def get_user(self, user_id):
        return {"mode": "auto"} if user_id == ADMIN else None


def video_message(message_id: int = 77, **fields):
    document = SimpleNamespace(
        id=1, attributes=[DocumentAttributeVideo(duration=5, w=1, h=1)], dc_id=4
    )
    base = dict(
        id=message_id, media=object(), document=document, photo=None,
        file=SimpleNamespace(name="clip.mp4", size=64, mime_type="video/mp4",
                             duration=5, width=1, height=1),
        fwd_from=None, via_bot_id=None, out=False, post_author=None, noforwards=False,
    )
    base.update(fields)
    return SimpleNamespace(**base)


def text_message(message_id: int = 77, **fields):
    base = dict(id=message_id, media=None, document=None, photo=None, file=None,
                fwd_from=None, via_bot_id=None, out=False, post_author=None, noforwards=False)
    base.update(fields)
    return SimpleNamespace(**base)


class ChannelPost:
    def __init__(self, text: str = "", *, chat_id: int = CACHE, message=None,
                 protected: bool = False) -> None:
        self.raw_text = text
        self.sender_id = chat_id
        self.chat_id = chat_id
        self.is_private = False
        self.is_channel = True
        self.is_group = False
        self.message = message or text_message()
        self.protected = protected
        self.replies: list[str] = []

    async def reply(self, text, **_kwargs):
        self.replies.append(text)

    async def get_chat(self):
        return SimpleNamespace(id=self.chat_id, noforwards=self.protected)


@pytest.fixture
def config():
    config = Config()
    config.access.admin_user_ids = [ADMIN, OTHER_ADMIN]
    config.delivery.cache_chat_id = CACHE
    return config


def make(config, **bot_options):
    bot, queue, db = FakeBot(**bot_options), FakeQueue(), FakeDb()
    handlers = BotHandlers(bot=bot, config=config, db=db, queue=queue,
                           pikpak=object(), portal=object())
    return handlers, bot, queue


class TestRequests:
    async def test_a_link_is_queued_for_the_first_admin(self, config):
        handlers, _bot, queue = make(config)
        event = ChannelPost(f"please get {LINK}")
        await handlers.on_message(event)
        (job,) = queue.jobs
        assert job.kind is JobKind.MESSAGE
        assert job.user_id == ADMIN and job.mode == "auto"  # the admin's own mode
        # The answer goes under the message in the channel.
        assert job.chat_id == CACHE and job.reply_to == 77
        assert event.replies  # "queued ..."

    async def test_a_magnet_is_queued_too(self, config):
        handlers, _bot, queue = make(config)
        await handlers.on_message(ChannelPost(MAGNET))
        assert [job.kind for job in queue.jobs] == [JobKind.URL]

    async def test_a_web_address_is_not_a_request(self, config):
        handlers, _bot, queue = make(config)
        event = ChannelPost("see https://example.com/page")
        await handlers.on_message(event)
        assert queue.jobs == [] and event.replies == []

    async def test_the_admin_gets_a_short_note(self, config):
        handlers, bot, _queue = make(config)
        await handlers.on_message(ChannelPost(LINK))
        assert [chat for chat, _text in bot.dms] == [ADMIN]

    async def test_the_note_can_be_turned_off(self, config):
        config.delivery.channel_reply_dm = False
        handlers, bot, queue = make(config)
        await handlers.on_message(ChannelPost(LINK))
        assert queue.jobs and bot.dms == []

    async def test_a_posted_video_is_copied_when_it_can_be(self, config):
        handlers, _bot, queue = make(config)
        await handlers.on_message(ChannelPost(message=video_message()))
        (job,) = queue.jobs
        assert job.kind is JobKind.INBOUND and job.mode == "auto"
        assert job.forward_to == ADMIN  # instant, nothing downloaded
        assert job.chat_id == CACHE and job.reply_to == 77

    async def test_a_posted_video_in_a_protected_channel_is_downloaded(self, config):
        handlers, _bot, queue = make(config)
        await handlers.on_message(ChannelPost(message=video_message(), protected=True))
        (job,) = queue.jobs
        assert job.forward_to is None  # auto mode keeps it on the NAS


class TestNeverARequest:
    @pytest.mark.parametrize("message", [
        video_message(fwd_from=MessageFwdHeader(date=None)),   # the reading account's forward
        video_message(via_bot_id=123),
        video_message(out=True),                                 # the bot's own upload
        video_message(post_author="Media Bot"),                  # signed by the bot
        text_message(fwd_from=MessageFwdHeader(date=None)),
    ], ids=["forwarded", "via-bot", "own", "signed-by-bot", "forwarded-link"])
    async def test_ignored(self, config, message):
        handlers, bot, queue = make(config)
        event = ChannelPost(LINK if message.media is None else "", message=message)
        await handlers.on_message(event)
        assert queue.jobs == [] and event.replies == [] and bot.dms == []

    async def test_a_photo_without_text(self, config):
        handlers, _bot, queue = make(config)
        photo = text_message(media=object(), photo=object(),
                             file=SimpleNamespace(name="p.jpg", size=1, mime_type="image/jpeg",
                                                  duration=None, width=1, height=1))
        event = ChannelPost(message=photo)
        await handlers.on_message(event)
        assert queue.jobs == [] and event.replies == []

    async def test_words_without_a_link(self, config):
        handlers, _bot, queue = make(config)
        event = ChannelPost("just a note to self")
        await handlers.on_message(event)
        assert queue.jobs == [] and event.replies == []


class TestOnlyTheCacheChannel:
    async def test_another_channel_gets_no_answer(self, config):
        handlers, bot, queue = make(config)
        event = ChannelPost(LINK, chat_id=ELSEWHERE)
        await handlers.on_message(event)
        assert queue.jobs == [] and event.replies == []
        assert bot.permission_checks == 0

    async def test_no_cache_channel_no_requests(self, config):
        config.delivery.cache_chat_id = None
        handlers, _bot, queue = make(config)
        event = ChannelPost(LINK)
        await handlers.on_message(event)
        assert queue.jobs == [] and event.replies == []

    async def test_not_run_by_an_admin_says_why(self, config):
        handlers, _bot, queue = make(config, admin_runs_it=False)
        event = ChannelPost(LINK)
        await handlers.on_message(event)
        assert queue.jobs == []
        assert len(event.replies) == 1 and "admin" in event.replies[0]

    async def test_the_check_is_remembered_for_ten_minutes(self, config, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr("tgmd.handlers.time.monotonic", lambda: clock[0])
        handlers, bot, queue = make(config)
        await handlers.on_message(ChannelPost(LINK))
        checks = bot.permission_checks
        await handlers.on_message(ChannelPost(LINK))
        assert bot.permission_checks == checks
        clock[0] += 601
        await handlers.on_message(ChannelPost(LINK))
        assert bot.permission_checks > checks
        assert len(queue.jobs) == 3
