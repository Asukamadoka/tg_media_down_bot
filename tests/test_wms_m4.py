"""WMS M4: the warehouse panel, a Telegram Mini App for admins.

Driven over a real socket like the PikPak login Mini App: only a
Telegram-signed admin gets in, every button goes through the same plan,
apply and undo pipeline as the command line, and permanent deletion is not
reachable from it at all.
"""

from __future__ import annotations

import json
import re
import socket
import time

import aiohttp
import pytest
from wms_fakes import FakeDrive

from pikpak_wms.core.models import ActionType
from pikpak_wms.ops import organize, plans
from pikpak_wms.rules.schema import parse_rules
from tgmd import i18n, wms
from tgmd.config import Config, HttpConfig
from tgmd.handlers import BotHandlers
from tgmd.miniapp import sign_init_data
from tgmd.webserver import FileServer
from tgmd.wms_panel import API_PATH, PAGE_PATH, WmsPanel, render_panel

BOT_TOKEN = "123456789:AAHfiqksKZ8wmoyzYeb1n1pbDVHQHKQ1abc"
ADMIN, MEMBER = 4242, 777
BIG = 200 * 1024**2
SHOWS = {
    "name": "shows", "scope": "/Inbox",
    "match": {"kind": "file", "name_regex": r"(?P<show>.+?)\.S(?P<s>\d{2})E(?P<e>\d{2})"},
    "actions": [{"rename": {"template": "{show|title}.S{s}E{e}.{ext}"}},
                {"move": {"to": "/Media/{show|title}/S{s}"}}],
}


@pytest.fixture(autouse=True)
def english():
    previous = i18n.language()
    i18n.set_language("en")
    yield
    i18n.set_language(previous)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def signed(user_id: int) -> str:
    return sign_init_data(
        {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id})}, BOT_TOKEN
    )


class FakeService:
    def __init__(self, drive):
        self.drive = drive

    async def has_user_session(self, user_id):
        return False

    async def client(self, user_id=None):
        return self.drive


class Setup:
    def __init__(self, config, drive, inbot, panel, base):
        self.config, self.drive, self.inbot, self.panel, self.base = (
            config, drive, inbot, panel, base)

    async def call(self, action: str, *, user: int = ADMIN, init: str | None = None, **extra):
        body = {"initData": signed(user) if init is None else init, "action": action, **extra}
        async with aiohttp.ClientSession() as session, session.post(
            self.base + API_PATH, json=body
        ) as response:
            return response.status, await response.json()

    async def make_plan(self) -> int:
        ctx = self.inbot.embedded.ctx
        await self.inbot.embedded.run_job("stocktake")
        plan = await organize.organize(ctx, parse_rules({"rules": [SHOWS]}))
        return await plans.save(ctx, plan)


@pytest.fixture
async def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "wms.yaml").write_text("ratelimit: {requests_per_second: 100000, burst: 100000}\n")
    drive = FakeDrive()
    drive.add("/Inbox/lost.S01E01.mkv", size=BIG)
    port = free_port()
    config = Config()
    config.telegram.bot_token = BOT_TOKEN
    config.access.admin_user_ids = [ADMIN]
    config.access.allowed_user_ids = [MEMBER]
    config.wms.enabled = True
    config.http = HttpConfig(enabled=True, host="127.0.0.1", port=port,
                             public_base_url=f"http://127.0.0.1:{port}")
    inbot = wms.WmsInBot(config, FakeService(drive))
    panel = WmsPanel(config, inbot)
    server = FileServer(config.http, "secret", routes=[panel])
    await server.start()
    await inbot.start()
    try:
        yield Setup(config, drive, inbot, panel, f"http://127.0.0.1:{port}")
    finally:
        await inbot.stop()
        await server.stop()


# ------------------------------------------------------------------- access


class TestAccess:
    async def test_only_a_telegram_signed_admin_gets_in(self, setup):
        status, body = await setup.call("plans", init="user=%7B%22id%22%3A4242%7D&hash=forged")
        assert status == 401 and not body["ok"]
        status, body = await setup.call("plans", user=MEMBER)
        assert status == 403 and "admins" in body["error"]
        status, body = await setup.call("plans")
        assert status == 200 and body["ok"]

    async def test_with_wms_off_the_panel_says_so(self, setup):
        await setup.inbot.stop()
        status, body = await setup.call("plans")
        assert status == 503 and "WMS_ENABLED" in body["error"]

    async def test_nonsense_is_a_bad_request(self, setup):
        assert (await setup.call("launch-missiles"))[0] == 400
        assert (await setup.call("plan", id="x"))[0] == 400


# ------------------------------------------------------------------ the loop


class TestPanel:
    async def test_list_view_apply_audit_undo(self, setup):
        plan_id = await setup.make_plan()

        _, body = await setup.call("plans")
        (listed,) = body["plans"]
        assert listed["id"] == plan_id and listed["status"] == "pending"
        assert listed["header"].startswith(f"Plan {plan_id} (organize): 3 action(s)")
        assert "1 plan(s) waiting" in body["status"]

        _, body = await setup.call("plan", id=plan_id)
        assert any("mkdir   /Media/Lost/S01" in line for line in body["lines"])

        _, body = await setup.call("apply", id=plan_id)
        assert body["summary"].startswith(f"Plan {plan_id}: 3 applied")
        assert setup.drive.id_at("/Media/Lost/S01/Lost.S01E01.mkv")
        _, body = await setup.call("plans")
        assert body["plans"] == []

        _, body = await setup.call("audit")
        move = next(e for e in body["entries"] if e["what"].startswith("move"))
        _, preview = await setup.call("undo_preview", id=move["id"])
        assert preview["applied"] is False and "/Inbox" in preview["what"]
        _, done = await setup.call("undo", id=move["id"])
        assert done["applied"] is True
        assert setup.drive.id_at("/Inbox/Lost.S01E01.mkv")
        status, again = await setup.call("undo", id=move["id"])
        assert status == 409 and "already undone" in again["error"]

    async def test_discard(self, setup):
        plan_id = await setup.make_plan()
        _, body = await setup.call("discard", id=plan_id)
        assert body["ok"]
        status, body = await setup.call("apply", id=plan_id)
        assert status == 409 and "discarded" in body["error"]

    async def test_permanent_deletion_is_not_reachable_from_the_panel(self, setup):
        setup.config.wms.enabled = True
        ctx = setup.inbot.embedded.ctx
        ctx.config.runtime.allow_permanent_delete = True  # even with the switch on
        await setup.inbot.embedded.run_job("stocktake")
        forever = await organize.cleanup(ctx, parse_rules({"rules": [
            {"name": "all", "stage": "cleanup", "scope": "/Inbox", "actions": ["trash"]}]}),
            forever=True)
        assert forever.actions[0].type is ActionType.DELETE_FOREVER
        plan_id = await plans.save(ctx, forever)
        status, body = await setup.call("apply", id=plan_id)
        assert status == 409 and "Permanent deletion" in body["error"]
        assert "delete_forever" not in setup.drive.calls


# --------------------------------------------------------------------- page


class TestPage:
    async def test_the_page_is_served_with_the_catalogue_as_json(self, setup):
        async with aiohttp.ClientSession() as session, session.get(
            setup.base + PAGE_PATH
        ) as response:
            html = await response.text()
            assert response.status == 200
            assert response.headers["Cache-Control"].startswith("no-store")
        assert '<html lang="en">' in html
        strings = json.loads(re.search(
            r'<script type="application/json" id="strings">(.*?)</script>', html, re.S
        ).group(1))
        assert strings["wms.panel.apply"] == "Apply"

    def test_chinese_page(self):
        i18n.set_language("zh")
        html = render_panel()
        assert '<html lang="zh">' in html and "确认执行" in html

    def test_a_translation_cannot_close_the_script(self, monkeypatch):
        monkeypatch.setitem(i18n.CATALOG["en"], "wms.panel.apply", "</script><b>x")
        html = render_panel()
        payload = re.search(r'id="strings">(.*?)</script>', html, re.S).group(1)
        assert json.loads(payload)["wms.panel.apply"] == "</script><b>x"


# -------------------------------------------------------------- panel url


class TestAvailability:
    def make(self, *, enabled=True, base="https://bot.example.com", running=True):
        config = Config()
        config.wms.enabled = enabled
        config.http = HttpConfig(enabled=bool(base), public_base_url=base)
        panel = WmsPanel(config, wms.WmsInBot(config, FakeService(FakeDrive())))
        panel._running = running  # noqa: SLF001 - no server in these tests
        return panel

    def test_https_gives_a_url(self):
        assert self.make().url == "https://bot.example.com/wms/app"

    def test_why_not(self):
        assert "WMS_ENABLED" in self.make(enabled=False).unavailable_reason()
        assert "public HTTPS" in self.make(base="").unavailable_reason()
        assert "only over HTTPS" in self.make(base="http://10.0.0.2:8080").unavailable_reason()


# ------------------------------------------------------------------- /wms


class Event:
    def __init__(self, user_id=ADMIN, *, private=True):
        self.raw_text = "/wms"
        self.sender_id = self.chat_id = user_id
        self.is_private = private
        self.replies: list[tuple[str, dict]] = []

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return self


def handlers_for(config, inbot, panel) -> BotHandlers:
    handlers = BotHandlers(bot=object(), config=config, db=object(), queue=object(),
                           pikpak=object(), portal=object())
    handlers.attach_wms(inbot, panel)
    return handlers


class TestCommand:
    async def test_admins_get_the_status_and_the_panel_button(self, setup):
        setup.config.http.public_base_url = "https://bot.example.com"
        handlers = handlers_for(setup.config, setup.inbot, setup.panel)
        event = Event()
        await handlers.on_wms(event)
        text, kwargs = event.replies[0]
        assert "PikPak warehouse" in text and "entries indexed" in text
        assert kwargs.get("buttons") is not None

    async def test_without_https_it_says_why_there_is_no_button(self, setup):
        handlers = handlers_for(setup.config, setup.inbot, setup.panel)
        event = Event()
        await handlers.on_wms(event)
        text, kwargs = event.replies[0]
        assert "only over HTTPS" in text and "buttons" not in kwargs

    async def test_members_are_refused(self, setup):
        handlers = handlers_for(setup.config, setup.inbot, setup.panel)
        event = Event(MEMBER)
        await handlers.on_wms(event)
        assert "Only admins" in event.replies[0][0]

    async def test_off(self, setup):
        await setup.inbot.stop()
        handlers = handlers_for(setup.config, setup.inbot, setup.panel)
        event = Event()
        await handlers.on_wms(event)
        assert "WMS_ENABLED" in event.replies[0][0]
