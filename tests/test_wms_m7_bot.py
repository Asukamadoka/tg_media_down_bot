"""WMS M7 in the bot (docs/wms/M7 §6, §7): /wms organize | dedupe | big |
protect | plans, their buttons, the scheduled announcements, sentences,
and the three side fixes (direct media off, a revoked session, the openai
backend).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon.errors import AuthKeyDuplicatedError
from test_app import FakeClient, make_config
from wms_fakes import FakeDrive

from pikpak_wms.nl.query import Clarification
from pikpak_wms.nl.translator import OpenAITranslator, TranslationError
from tgmd import clients as clients_module
from tgmd import handlers as handlers_module
from tgmd import i18n, wms
from tgmd.app import Application
from tgmd.config import Config
from tgmd.handlers import BotHandlers
from tgmd.setup import USER_SESSION_KEY

ADMIN = 4242
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
    def __init__(self, text="", user_id=ADMIN, *, data=b""):
        self.raw_text = text
        self.sender_id = self.chat_id = user_id
        self.is_private = True
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
    (tmp_path / "wms.yaml").write_text(
        "ratelimit: {requests_per_second: 100000, burst: 100000}\n"
        "schedule: {builtin: false}\n"
    )
    monkeypatch.setattr(handlers_module, "has_downloadable_media", lambda _m: False)
    drive = FakeDrive()
    drive.add("/Telegram/Cos 花絮 EP1.mp4", size=10)
    drive.add("/Telegram/Cos 花絮 EP2.mp4", size=10)
    drive.add("/Telegram/whatever.mov", size=10)
    drive.add("/Cos/keep.mp4", size=10)
    drive.add("/Cos/Huge/a.mkv", size=60 * GB)
    drive.add("/Other/x.mov", size=10)
    drive.add("/Other/y.mov", size=10)
    config = Config()
    config.access.admin_user_ids = [ADMIN]
    config.wms.enabled = True
    inbot = wms.WmsInBot(config, FakeService(drive))
    await inbot.start()
    handlers = BotHandlers(bot=object(), config=config, db=object(), queue=object(),
                           pikpak=object(), portal=object())
    handlers.attach_wms(inbot, None)
    try:
        yield handlers, inbot, drive
    finally:
        await inbot.stop()


def buttons_of(kwargs) -> list[tuple[str, bytes]]:
    markup = kwargs.get("buttons")
    if markup is None:
        return []
    return [(b.text, b.type.data) for row in markup.rows for b in row.buttons]


async def wms_command(handlers, text) -> Event:
    event = Event(text)
    await handlers.on_wms(event)
    return event


async def press(handlers, data) -> Event:
    event = Event(data=data if isinstance(data, bytes) else data.encode())
    await handlers.handle_wms_button(event)
    return event


def exists(drive, path) -> bool:
    try:
        drive.id_at(path)
    except KeyError:
        return False
    return True


class TestTheAcceptanceFlow:
    """M7 §8: /wms organize inbox → a plan → confirm → audited → undo one."""

    async def test_inbox_confirm_audit_undo(self, bot):
        handlers, inbot, drive = bot
        event = await wms_command(handlers, "/wms organize inbox")
        text, kwargs = event.replies[-1]
        assert "organize-inbox" in text
        assert [label for label, _ in buttons_of(kwargs)] == ["确认执行", "查看明细", "丢弃"]
        apply_data = buttons_of(kwargs)[0][1]

        detail = await press(handlers, buttons_of(kwargs)[1][1])
        assert "/Telegram/Cos 花絮 EP1.mp4" in detail.replies[0][0]

        pressed = await press(handlers, apply_data)
        assert "执行" in pressed.edits[0][0] and pressed.edits[0][1]["buttons"] is None
        assert exists(drive, "/Cos/Cos 花絮/Cos 花絮 EP1.mp4")
        audit = await inbot.embedded.audit(limit=20)
        moved = [e for e in audit if e["action"] == "move"]
        assert moved, "the confirmed moves are in the audit"

        entry = moved[0]
        preview = await wms_command(handlers, f"/wms undo {entry['id']}")
        assert "撤销" in preview.replies[0][0] or "undone" in preview.replies[0][0]
        undone = await press(handlers, f"wms:undo:{entry['id']}")
        assert undone.edits, undone.answers
        assert exists(drive, entry["before"]["path"])


class TestCommands:
    async def test_organize_tree_lists_one_plan_per_folder(self, bot):
        handlers, _inbot, _drive = bot
        event = await wms_command(handlers, "/wms organize tree")
        text, kwargs = event.replies[-1]
        assert "organize-tree" in text and "/Cos" in text and "/Other" in text
        labels = [label for label, _ in buttons_of(kwargs)]
        assert labels[0].startswith("✅ #") and labels[1].startswith("📄 #")
        detail = await press(handlers, buttons_of(kwargs)[1][1])
        assert [label for label, _ in buttons_of(detail.replies[0][1])] == ["确认执行", "丢弃"]

    async def test_organize_tree_for_one_folder(self, bot):
        handlers, _inbot, _drive = bot
        event = await wms_command(handlers, "/wms organize tree /Other")
        text, _kwargs = event.replies[-1]
        assert "organize-tree:/Other" in text and "/Cos" not in text

    async def test_organize_needs_a_kind(self, bot):
        handlers, *_ = bot
        event = await wms_command(handlers, "/wms organize")
        assert "/wms organize tree" in event.replies[0][0]

    async def test_big_report_with_trash_buttons(self, bot):
        handlers, inbot, drive = bot
        event = await wms_command(handlers, "/wms big")
        text, kwargs = event.replies[-1]
        assert "/Cos/Huge/a.mkv" in text and "不点 🗑" in text
        buttons = buttons_of(kwargs)
        assert buttons[0][0] == "🗑 1" and buttons[0][1].startswith(b"wms:bt:")
        pressed = await press(handlers, buttons[0][1])
        assert "已移入回收站" in pressed.replies[0][0]
        assert not exists(drive, "/Cos/Huge/a.mkv")
        audit = await inbot.embedded.audit(limit=5)
        assert audit[0]["action"] == "trash" and audit[0]["rule_name"] == "big-report"
        again = await press(handlers, buttons[0][1])
        assert again.replies == [] and "已经不在" in again.answers[0][0]

    async def test_a_protected_item_cannot_be_trashed_from_the_report(self, bot):
        handlers, _inbot, drive = bot
        report = await wms_command(handlers, "/wms big")
        first = buttons_of(report.replies[-1][1])[0][1]
        await wms_command(handlers, "/wms protect add /Cos")
        pressed = await press(handlers, first)
        assert pressed.replies == [] and "受白名单保护" in pressed.answers[0][0]
        assert exists(drive, "/Cos/Huge/a.mkv")

    async def test_protect(self, bot):
        handlers, *_ = bot
        listed = await wms_command(handlers, "/wms protect ls")
        assert "/小千" in listed.replies[0][0] and "分享过的内容（0 项）" in listed.replies[0][0]
        added = await wms_command(handlers, "/wms protect add /My Stuff")
        assert "/My Stuff" in added.replies[0][0]
        removed = await wms_command(handlers, "/wms protect rm /My Stuff")
        assert "/My Stuff" not in removed.replies[0][0]
        bad = await wms_command(handlers, "/wms protect add relative")
        assert "用法" in bad.replies[0][0]

    async def test_dedupe_and_plans(self, bot):
        handlers, _inbot, drive = bot
        drive.add("/Other/copy1.mkv", size=5, hash="X")
        drive.add("/Other/copy2.mkv", size=5, hash="X")
        event = await wms_command(handlers, "/wms dedupe")
        assert "dedupe" in event.replies[-1][0]
        assert [label for label, _ in buttons_of(event.replies[-1][1])] == [
            "确认执行", "查看明细", "丢弃"]
        listed = await wms_command(handlers, "/wms plans")
        text, kwargs = listed.replies[0]
        assert "等待确认的计划" in text and "dedupe" in text
        await press(handlers, buttons_of(kwargs)[0][1])  # ✅ applies it from the list
        assert not (exists(drive, "/Other/copy1.mkv") and exists(drive, "/Other/copy2.mkv"))
        empty = await wms_command(handlers, "/wms plans")
        assert empty.replies[0][0] == "没有等待确认的计划。"


class TestAnnouncements:
    async def test_a_batch_and_a_report_and_an_applied_dedupe(self, bot):
        _handlers, inbot, drive = bot
        sent = []

        async def notify(chat, text, buttons=None):
            sent.append((chat, text, buttons))

        inbot.attach_notifier(notify)
        tree = await inbot.embedded.run_job("organize-tree")
        await inbot.job_finished(tree)
        assert len(sent) == 1 and "#" in sent[0][1]
        await inbot.job_finished(tree)  # the same plans: not again
        assert len(sent) == 1

        report = await inbot.embedded.run_job("big-report")
        await inbot.job_finished(report)
        assert "大文件与大目录" in sent[-1][1] and sent[-1][2] is not None

        drive.add("/Other/c1.mkv", size=5, hash="Y")
        drive.add("/Other/c2.mkv", size=5, hash="Y")
        applied = await inbot.embedded.run_job("dedupe", apply=True)
        await inbot.job_finished(applied)
        assert "已执行" in sent[-1][1] and sent[-1][2] is None


class TestSentences:
    async def test_the_everyday_names(self, bot):
        _handlers, inbot, _drive = bot
        text, buttons = await inbot.nl_message(ADMIN, "整理一下 Telegram")
        assert "organize-inbox" in text and buttons is not None
        text, buttons = await inbot.nl_message(ADMIN, "看看最大的文件")
        assert "/Cos/Huge/a.mkv" in text
        assert buttons_of({"buttons": buttons})[0][1].startswith(b"wms:bt:")
        text, _buttons = await inbot.nl_message(ADMIN, "把大文件单独放一起")
        assert "/大文件/Cos/Huge" in text
        text, _buttons = await inbot.nl_message(ADMIN, "每周去重")
        assert "本来就按时自动运行" in text


# ------------------------------------------------------------------ §7.1


class TestDirectMediaIsRefused:
    async def test_auto_is_refused_at_startup(self, tmp_path, monkeypatch, caplog):
        bot, user = FakeClient(), FakeClient(user_id=9, username="reader", is_bot=False)

        async def fake_start_clients(config, stored_session=None, *, on_rejected=None):
            return bot, user

        monkeypatch.setattr("tgmd.app.start_clients", fake_start_clients)
        config = make_config(tmp_path)
        config.telegram.direct_media = "auto"
        app = Application(config)
        await app.start()
        try:
            assert app.route is None
            assert config.telegram.direct_media == "off"
            assert "TG_DIRECT_MEDIA=auto is refused" in caplog.text
        finally:
            await app.stop()


# ------------------------------------------------------------------ §7.2


class TestARevokedSession:
    async def test_start_clients_says_where_the_rejected_session_came_from(self, monkeypatch):
        config = SimpleNamespace(telegram=SimpleNamespace(
            bot_token="123:AAAA", user_session="", user_session_file=None))

        class User:
            async def connect(self):
                raise AuthKeyDuplicatedError(request=None)

            async def disconnect(self):
                return None

        class Bot:
            async def start(self, bot_token):
                return self

            async def get_me(self):
                return SimpleNamespace(id=123, username="b", first_name="b", bot=True)

        heard = []

        async def on_rejected(source, reason):
            heard.append((source, reason))

        monkeypatch.setattr(clients_module, "build_bot_client", lambda _c: Bot())
        monkeypatch.setattr(clients_module, "build_user_client", lambda _c, _s=None: User())
        monkeypatch.setattr(clients_module, "user_session_source",
                            lambda _c, stored: (object(), "an in-chat login") if stored else None)
        _bot, reader = await clients_module.start_clients(config, "dead",
                                                          on_rejected=on_rejected)
        assert reader is None
        assert heard == [("an in-chat login", "AuthKeyDuplicatedError")]

    async def test_the_dead_stored_session_is_dropped_and_the_admins_told(self, tmp_path):
        app = Application(make_config(tmp_path, admins=[1, 2]))
        await app.db.connect()
        try:
            await app.db.kv_set(USER_SESSION_KEY, "dead-session")
            await app._session_rejected("an in-chat login", "AuthKeyDuplicatedError")  # noqa: SLF001
            assert await app.db.kv_get(USER_SESSION_KEY) is None
            sent = []

            async def send(chat, text, buttons=None):
                if chat == 2:
                    raise RuntimeError("the admin never opened the chat")
                sent.append((chat, text))

            app._send_html = send  # noqa: SLF001
            await app._tell_admins_session_rejected()  # noqa: SLF001
            assert [chat for chat, _ in sent] == [1]
            assert "/setup telegram" in sent[0][1] and "AuthKeyDuplicatedError" in sent[0][1]
        finally:
            await app.db.close()

    async def test_an_env_session_is_left_in_place_but_named(self, tmp_path):
        app = Application(make_config(tmp_path, admins=[1]))
        await app.db.connect()
        try:
            await app.db.kv_set(USER_SESSION_KEY, "other")
            await app._session_rejected("TG_USER_SESSION", "not authorized")  # noqa: SLF001
            assert await app.db.kv_get(USER_SESSION_KEY) == "other"
            sent = []

            async def send(chat, text, buttons=None):
                sent.append(text)

            app._send_html = send  # noqa: SLF001
            await app._tell_admins_session_rejected()  # noqa: SLF001
            assert "TG_USER_SESSION" in sent[0]
        finally:
            await app.db.close()


# ------------------------------------------------------------------ §7.3


NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)


def wire(**overrides):
    answer = {"intent": "dedupe", "scope": {"path": "/", "recursive": True},
              "filters": {"created_after": None, "created_before": None, "min_size": None,
                          "max_size": None, "kinds": [], "extensions": [],
                          "name_contains": [], "name_regex": None},
              "action_args": {"dest": None, "template": None, "part": None},
              "schedule": None, "needs_clarification": None}
    answer.update(overrides)
    return answer


class FakeServer:
    """An OpenAI-compatible endpoint: scripted answers, requests recorded."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[tuple[str, dict, dict]] = []

    async def __call__(self, url, body, headers):
        self.requests.append((url, body, headers))
        return self.answers.pop(0)


def ok(content, finish="stop"):
    return 200, {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


class TestOpenAIBackend:
    def make(self, server, **kw):
        return OpenAITranslator(base_url=kw.pop("base_url", "http://lm:1234/v1/"),
                                model=kw.pop("model", "qwen2.5-7b"),
                                api_key=kw.pop("api_key", ""), post=server, **kw)

    async def test_the_schema_goes_in_response_format(self):
        server = FakeServer(ok(json.dumps(wire())))
        result = await self.make(server).translate("去重", NOW, UTC)
        assert result.intent == "dedupe"
        url, body, headers = server.requests[0]
        assert url == "http://lm:1234/v1/chat/completions"
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["schema"]["required"][0] == "intent"
        assert body["temperature"] == 0 and "Authorization" not in headers
        # Only the sentence and the time go out: nothing from the drive.
        assert body["messages"][1]["content"].endswith("Instruction: 去重")

    async def test_a_server_without_schemas_gets_json_mode(self):
        server = FakeServer(
            (400, {"error": {"message": "response_format json_schema unsupported"}}),
            ok("```json\n" + json.dumps(wire(intent="big_report")) + "\n```"),
            ok(json.dumps(wire())),
        )
        translator = self.make(server, api_key="sk-test")
        assert (await translator.translate("看看最大的文件", NOW, UTC)).intent == "big_report"
        second = server.requests[1][1]
        assert second["response_format"] == {"type": "json_object"}
        assert "JSON Schema" in second["messages"][0]["content"]
        assert server.requests[1][2]["Authorization"] == "Bearer sk-test"
        await translator.translate("去重", NOW, UTC)  # remembered: straight to json_object
        assert server.requests[2][1]["response_format"] == {"type": "json_object"}

    async def test_refusal_cut_off_and_errors(self):
        refused = FakeServer((200, {"choices": [{"message": {"refusal": "no"}}]}))
        assert isinstance(await self.make(refused).translate("x", NOW, UTC), Clarification)
        with pytest.raises(TranslationError):
            await self.make(FakeServer(ok("{", finish="length"))).translate("x", NOW, UTC)
        with pytest.raises(TranslationError):
            await self.make(FakeServer((500, {"error": {"message": "boom"}}))).translate(
                "x", NOW, UTC)
        with pytest.raises(TranslationError):
            await self.make(FakeServer(ok("not json"))).translate("x", NOW, UTC)
        with pytest.raises(TranslationError):
            await self.make(FakeServer(), base_url="").translate("x", NOW, UTC)

    def test_it_is_a_backend_choice(self, monkeypatch):
        from pikpak_wms.nl.translator import from_environment

        monkeypatch.setenv("NL_BACKEND", "openai")
        monkeypatch.setenv("NL_OPENAI_BASE_URL", "http://lm:1234/v1")
        monkeypatch.setenv("NL_OPENAI_MODEL", "m")
        chain = from_environment()
        assert chain.name == "rules+openai"
        assert chain.translators[1].base_url == "http://lm:1234/v1"
