"""The PikPak login Mini App, driven over a real socket.

This form collects a password, so its behaviour is worth nailing down: only
a Telegram-signed identity gets in, only if that person may use the bot,
guessing is throttled, and the page never presents itself as PikPak.
"""

from __future__ import annotations

import json
import socket
import time

import aiohttp
import pytest

from tgmd.config import HttpConfig, PikPakConfig
from tgmd.miniapp import sign_init_data
from tgmd.pikpak import PikPakError
from tgmd.portal import MINIAPP_ATTEMPT_LIMIT, PikPakLoginPortal
from tgmd.webserver import FileServer

SECRET = "portal-test-secret"
GOOD_PASSWORD = "correct-horse"
BOT_TOKEN = "123456789:AAHfiqksKZ8wmoyzYeb1n1pbDVHQHKQ1abc"

# User ids the fixture's access list lets in. Everything else is a stranger.
STRANGER = 666


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def allowed(user_id: int) -> bool:
    return user_id != STRANGER


def make_portal(service, http: HttpConfig, pikpak: PikPakConfig | None = None):
    return PikPakLoginPortal(
        service,
        pikpak or PikPakConfig(allow_user_login=True),
        http,
        bot_token=BOT_TOKEN,
        is_allowed=allowed,
    )


class FakePikPak:
    """Stands in for PikPakService, recording logins."""

    def __init__(self) -> None:
        self.logins: list[tuple[int, str, str]] = []

    async def login_with_password(self, user_id: int, username: str, password: str):
        if password != GOOD_PASSWORD:
            raise PikPakError("wrong password")
        self.logins.append((user_id, username, password))
        return username


@pytest.fixture
def service():
    return FakePikPak()


@pytest.fixture
async def portal(service):
    """A running portal on loopback. Plain HTTP, so no button is offered,
    but the routes answer, which is all these tests need."""
    port = free_port()
    http = HttpConfig(
        enabled=True,
        host="127.0.0.1",
        port=port,
        public_base_url=f"http://127.0.0.1:{port}",
    )
    instance = make_portal(service, http)
    server = FileServer(http, SECRET, portal=instance)
    await server.start()
    yield instance
    await server.stop()


async def get(url: str) -> tuple[int, str, dict]:
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        return response.status, await response.text(), dict(response.headers)


async def post_json(url: str, body: dict) -> tuple[int, dict]:
    async with aiohttp.ClientSession() as session, session.post(url, json=body) as response:
        return response.status, await response.json()


def signed_init_data(user_id: int) -> str:
    """initData as Telegram would sign it for this bot."""
    return sign_init_data(
        {
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": user_id, "first_name": "Test"}),
        },
        BOT_TOKEN,
    )


class _DummyRouter:
    """Accepts route registration without a real application."""

    def add_get(self, *_args, **_kwargs):
        pass

    def add_post(self, *_args, **_kwargs):
        pass


class TestAvailability:
    def test_https_and_attached_is_available(self, service):
        http = HttpConfig(enabled=True, public_base_url="https://media.example.com")
        instance = make_portal(service, http)
        instance.register(_DummyRouter())
        assert instance.unavailable_reason() is None
        assert instance.miniapp_url == "https://media.example.com/pikpak/app"

    def test_disabled_by_configuration(self, service):
        http = HttpConfig(enabled=True, public_base_url="https://example.com")
        instance = make_portal(service, http, PikPakConfig(allow_user_login=False))
        instance.register(_DummyRouter())
        assert "disabled per-user PikPak logins" in (instance.unavailable_reason() or "")
        assert instance.miniapp_url is None

    def test_without_a_public_url(self, service):
        instance = make_portal(service, HttpConfig(enabled=True))
        instance.register(_DummyRouter())
        assert "PUBLIC_BASE_URL" in (instance.unavailable_reason() or "")

    @pytest.mark.parametrize(
        "base", ["http://media.example.com", "http://127.0.0.1:8080", "http://localhost"]
    )
    def test_plain_http_is_refused_even_on_loopback(self, service, base):
        # Telegram only opens Mini Apps over HTTPS, whatever the host.
        instance = make_portal(service, HttpConfig(enabled=True, public_base_url=base))
        instance.register(_DummyRouter())
        assert "HTTPS" in (instance.unavailable_reason() or "")
        assert instance.miniapp_url is None

    def test_unregistered_portal_is_unavailable(self, service):
        http = HttpConfig(enabled=True, public_base_url="https://media.example.com")
        instance = make_portal(service, http)
        assert instance.unavailable_reason() is not None
        assert instance.miniapp_url is None

    async def test_the_one_time_link_routes_are_gone(self, portal):
        base = portal._http.base_url  # noqa: SLF001 - need the bound port
        status, _, _ = await get(f"{base}/pikpak/login/anything")
        assert status == 404


class TestMiniApp:
    """The in-Telegram form, where identity comes from signed initData."""

    async def test_the_page_is_served(self, portal):
        status, body, _ = await get(f"{portal._http.base_url}/pikpak/app")  # noqa: SLF001
        assert status == 200
        assert "telegram-web-app.js" in body
        assert 'name="password"' in body

    async def test_the_page_does_not_pretend_to_be_pikpak(self, portal):
        _, body, _ = await get(f"{portal._http.base_url}/pikpak/app")  # noqa: SLF001
        assert "not operated by PikPak" in body

    async def test_signed_init_data_logs_the_right_user_in(self, portal, service):
        url = f"{portal._http.base_url}/pikpak/app"  # noqa: SLF001
        status, payload = await post_json(
            url,
            {
                "initData": signed_init_data(777),
                "username": "me@example.com",
                "password": GOOD_PASSWORD,
            },
        )
        assert status == 200
        assert payload["ok"] is True
        assert service.logins == [(777, "me@example.com", GOOD_PASSWORD)]

    async def test_unsigned_init_data_is_refused(self, portal, service):
        url = f"{portal._http.base_url}/pikpak/app"  # noqa: SLF001
        status, payload = await post_json(
            url,
            {"initData": "user=%7B%22id%22%3A1%7D&hash=deadbeef",
             "username": "me", "password": GOOD_PASSWORD},
        )
        assert status == 401
        assert payload["ok"] is False
        assert service.logins == []

    async def test_init_data_signed_by_another_bot_is_refused(self, portal, service):
        url = f"{portal._http.base_url}/pikpak/app"  # noqa: SLF001
        other = sign_init_data(
            {"auth_date": str(int(time.time())), "user": json.dumps({"id": 5})},
            "987654321:" + "B" * 35,
        )
        status, _ = await post_json(
            url, {"initData": other, "username": "me", "password": GOOD_PASSWORD}
        )
        assert status == 401
        assert service.logins == []

    async def test_a_forged_user_id_is_refused(self, portal, service):
        """Rewriting the id after signing must not let you target someone else."""
        url = f"{portal._http.base_url}/pikpak/app"  # noqa: SLF001
        tampered = signed_init_data(777).replace("777", "888")
        status, _ = await post_json(
            url, {"initData": tampered, "username": "me", "password": GOOD_PASSWORD}
        )
        assert status == 401
        assert service.logins == []

    async def test_wrong_password_is_reported(self, portal, service):
        url = f"{portal._http.base_url}/pikpak/app"  # noqa: SLF001
        status, payload = await post_json(
            url, {"initData": signed_init_data(1), "username": "me", "password": "no"}
        )
        assert status == 401
        assert "wrong password" in payload["error"]
        assert service.logins == []

    async def test_missing_fields(self, portal):
        url = f"{portal._http.base_url}/pikpak/app"  # noqa: SLF001
        status, payload = await post_json(
            url, {"initData": signed_init_data(1), "username": "", "password": ""}
        )
        assert status == 400
        assert "both fields" in payload["error"]

    async def test_malformed_body(self, portal):
        url = f"{portal._http.base_url}/pikpak/app"  # noqa: SLF001
        async with (
            aiohttp.ClientSession() as session,
            session.post(url, data="not json") as response,
        ):
            assert response.status == 400

    async def test_repeated_attempts_are_throttled(self, portal, service):
        url = f"{portal._http.base_url}/pikpak/app"  # noqa: SLF001
        body = {"initData": signed_init_data(99), "username": "me", "password": "no"}
        for _ in range(MINIAPP_ATTEMPT_LIMIT):
            assert (await post_json(url, body))[0] == 401
        status, payload = await post_json(url, body)
        assert status == 429
        assert "Too many attempts" in payload["error"]

    async def test_throttle_is_per_user(self, portal):
        url = f"{portal._http.base_url}/pikpak/app"  # noqa: SLF001
        mine = {"initData": signed_init_data(11), "username": "me", "password": "no"}
        for _ in range(MINIAPP_ATTEMPT_LIMIT):
            await post_json(url, mine)
        assert (await post_json(url, mine))[0] == 429
        theirs = {"initData": signed_init_data(22), "username": "me", "password": "no"}
        assert (await post_json(url, theirs))[0] == 401

    async def test_disabled_logins_refuse_the_endpoint(self, service):
        port = free_port()
        http = HttpConfig(
            enabled=True,
            host="127.0.0.1",
            port=port,
            public_base_url=f"http://127.0.0.1:{port}",
        )
        instance = make_portal(service, http, PikPakConfig(allow_user_login=False))
        server = FileServer(http, SECRET, portal=instance)
        await server.start()
        try:
            status, _ = await post_json(
                f"http://127.0.0.1:{port}/pikpak/app",
                {"initData": signed_init_data(1), "username": "a", "password": "b"},
            )
            assert status == 403
        finally:
            await server.stop()

    async def test_a_user_outside_the_access_list_is_refused(self, portal, service):
        # Telegram's signature says who this is, not that they may use the bot.
        url = f"{portal._http.base_url}/pikpak/app"  # noqa: SLF001
        status, payload = await post_json(
            url,
            {
                "initData": signed_init_data(STRANGER),
                "username": "me@example.com",
                "password": GOOD_PASSWORD,
            },
        )
        assert status == 403
        assert payload["ok"] is False
        assert service.logins == []

    async def test_security_headers(self, portal):
        _, _, headers = await get(f"{portal._http.base_url}/pikpak/app")  # noqa: SLF001
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["X-Frame-Options"] == "DENY"
        assert "no-store" in headers["Cache-Control"]
