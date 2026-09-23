"""The PikPak login-link portal, driven over a real socket.

This page collects a password, so its behaviour is worth nailing down: the
link must work once, expire, resist forgery, refuse to be issued over
cleartext HTTP, and never present itself as PikPak.
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
from tgmd.portal import (
    MAX_ATTEMPTS,
    MINIAPP_ATTEMPT_LIMIT,
    PikPakLoginPortal,
    PortalError,
    is_secure_base_url,
)
from tgmd.signing import make_token
from tgmd.webserver import FileServer

SECRET = "portal-test-secret"
GOOD_PASSWORD = "correct-horse"
BOT_TOKEN = "123456789:AAHfiqksKZ8wmoyzYeb1n1pbDVHQHKQ1abc"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class FakePikPak:
    """Stands in for PikPakService, recording logins."""

    def __init__(self) -> None:
        self.logins: list[tuple[int, str, str]] = []
        self.sessions: set[int] = set()
        self.configured = False

    async def login_with_password(self, user_id: int, username: str, password: str):
        if password != GOOD_PASSWORD:
            raise PikPakError("wrong password")
        self.logins.append((user_id, username, password))
        self.sessions.add(user_id)
        return username

    async def has_user_session(self, user_id: int) -> bool:
        return user_id in self.sessions


@pytest.fixture
def service():
    return FakePikPak()


@pytest.fixture
async def portal(service):
    """A running portal on loopback, which counts as a secure base URL."""
    port = free_port()
    http = HttpConfig(
        enabled=True,
        host="127.0.0.1",
        port=port,
        public_base_url=f"http://127.0.0.1:{port}",
    )
    pikpak = PikPakConfig(allow_user_login=True, login_link_ttl=300)
    instance = PikPakLoginPortal(service, pikpak, http, SECRET, bot_token=BOT_TOKEN)
    server = FileServer(http, SECRET, portal=instance)
    await server.start()
    yield instance
    await server.stop()


async def get(url: str) -> tuple[int, str, dict]:
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        return response.status, await response.text(), dict(response.headers)


async def post(url: str, data: dict) -> tuple[int, str]:
    async with aiohttp.ClientSession() as session, session.post(url, data=data) as response:
        return response.status, await response.text()


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


class TestSecureBaseUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://media.example.com",
            "https://example.com:8443",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
        ],
    )
    def test_accepted(self, url):
        assert is_secure_base_url(url)

    @pytest.mark.parametrize(
        "url",
        ["http://media.example.com", "http://203.0.113.5:8080", "ftp://x", "", "nonsense"],
    )
    def test_refused(self, url):
        assert not is_secure_base_url(url)


class TestAvailability:
    def test_a_running_portal_on_loopback_is_available(self, portal):
        assert portal.unavailable_reason() is None
        assert portal.enabled

    def test_disabled_by_configuration(self, service):
        http = HttpConfig(enabled=True, public_base_url="https://example.com")
        instance = PikPakLoginPortal(
            service, PikPakConfig(allow_user_login=False), http, SECRET
        )
        assert "disabled per-user PikPak logins" in (instance.unavailable_reason() or "")

    def test_without_a_public_url(self, service):
        instance = PikPakLoginPortal(
            service, PikPakConfig(), HttpConfig(enabled=True), SECRET
        )
        assert "PUBLIC_BASE_URL" in (instance.unavailable_reason() or "")

    def test_plain_http_on_a_public_host_is_refused(self, service):
        http = HttpConfig(enabled=True, public_base_url="http://media.example.com")
        instance = PikPakLoginPortal(service, PikPakConfig(), http, SECRET)
        instance.register(_DummyRouter())  # pretend it is attached
        reason = instance.unavailable_reason() or ""
        assert "HTTPS" in reason

    def test_create_link_refuses_when_unavailable(self, service):
        instance = PikPakLoginPortal(
            service, PikPakConfig(), HttpConfig(enabled=False), SECRET
        )
        with pytest.raises(PortalError):
            instance.create_link(42)


class _DummyRouter:
    """Accepts route registration without a real application."""

    def add_get(self, *_args, **_kwargs):
        pass

    def add_post(self, *_args, **_kwargs):
        pass


class TestLoginFlow:
    async def test_link_shape(self, portal):
        link = portal.create_link(42)
        assert "/pikpak/login/" in link
        assert link.startswith("http://127.0.0.1:")

    async def test_the_form_is_served(self, portal):
        status, body, _ = await get(portal.create_link(42))
        assert status == 200
        assert 'name="password"' in body
        assert 'name="username"' in body

    async def test_the_page_does_not_pretend_to_be_pikpak(self, portal):
        _, body, _ = await get(portal.create_link(42))
        assert "not operated by PikPak" in body
        assert "served by your own" in body

    async def test_the_page_explains_what_is_stored(self, portal):
        _, body, _ = await get(portal.create_link(42))
        assert "Only that token is saved" in body

    async def test_security_headers(self, portal):
        _, _, headers = await get(portal.create_link(42))
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["X-Frame-Options"] == "DENY"
        assert "no-store" in headers["Cache-Control"]

    async def test_successful_login_connects_the_right_user(self, portal, service):
        link = portal.create_link(4242)
        status, body = await post(
            link, {"username": "me@example.com", "password": GOOD_PASSWORD}
        )
        assert status == 200
        assert "PikPak connected" in body
        assert service.logins == [(4242, "me@example.com", GOOD_PASSWORD)]

    async def test_the_link_is_single_use(self, portal):
        link = portal.create_link(42)
        await post(link, {"username": "me@example.com", "password": GOOD_PASSWORD})
        status, body, _ = await get(link)
        assert status == 410
        assert "already been used" in body

    async def test_issuing_a_new_link_invalidates_the_old_one(self, portal):
        old = portal.create_link(42)
        new = portal.create_link(42)
        assert old != new
        assert (await get(old))[0] == 410
        assert (await get(new))[0] == 200

    async def test_one_users_link_does_not_affect_another(self, portal):
        first = portal.create_link(1)
        second = portal.create_link(2)
        assert (await get(first))[0] == 200
        assert (await get(second))[0] == 200

    async def test_revoke_kills_an_outstanding_link(self, portal):
        link = portal.create_link(42)
        portal.revoke(42)
        assert (await get(link))[0] == 410


class TestRejections:
    async def test_missing_fields(self, portal):
        status, body = await post(portal.create_link(42), {"username": "x"})
        assert status == 400
        assert "Enter both fields" in body

    async def test_wrong_password_keeps_the_link_alive(self, portal, service):
        link = portal.create_link(42)
        status, body = await post(link, {"username": "me", "password": "nope"})
        assert status == 401
        assert "wrong password" in body
        assert f"{MAX_ATTEMPTS - 1} attempt(s) left" in body
        # Still usable, so one typo is survivable.
        assert (await get(link))[0] == 200
        assert service.logins == []

    async def test_the_link_burns_after_too_many_attempts(self, portal):
        link = portal.create_link(42)
        for _ in range(MAX_ATTEMPTS - 1):
            assert (await post(link, {"username": "me", "password": "nope"}))[0] == 401
        status, body = await post(link, {"username": "me", "password": "nope"})
        assert status == 429
        assert "Too many attempts" in body
        assert (await get(link))[0] == 410

    async def test_a_forged_token_is_refused(self, portal):
        base = portal._http.base_url  # noqa: SLF001 - need the bound port
        status, _, _ = await get(f"{base}/pikpak/login/not-a-real-token")
        assert status == 410

    async def test_a_token_signed_with_another_secret_is_refused(self, portal):
        base = portal._http.base_url  # noqa: SLF001
        forged = make_token("other-secret", "login:42:nonce", int(time.time()) + 60)
        assert (await get(f"{base}/pikpak/login/{forged}"))[0] == 410

    async def test_a_validly_signed_unknown_nonce_is_refused(self, portal):
        base = portal._http.base_url  # noqa: SLF001
        token = make_token(SECRET, "login:42:never-issued", int(time.time()) + 60)
        status, body, _ = await get(f"{base}/pikpak/login/{token}")
        assert status == 410
        assert "already been used" in body

    async def test_a_token_for_a_different_payload_is_refused(self, portal):
        base = portal._http.base_url  # noqa: SLF001
        token = make_token(SECRET, "file:something", int(time.time()) + 60)
        status, body, _ = await get(f"{base}/pikpak/login/{token}")
        assert status == 410
        assert "not a login link" in body

    async def test_an_expired_link_is_refused(self, service):
        port = free_port()
        http = HttpConfig(
            enabled=True,
            host="127.0.0.1",
            port=port,
            public_base_url=f"http://127.0.0.1:{port}",
        )
        # A negative lifetime produces a link that is already past its expiry.
        instance = PikPakLoginPortal(
            service, PikPakConfig(login_link_ttl=-5), http, SECRET
        )
        server = FileServer(http, SECRET, portal=instance)
        await server.start()
        try:
            status, body, _ = await get(instance.create_link(42))
            assert status == 410
            assert "expired" in body
        finally:
            await server.stop()

    async def test_a_stolen_link_cannot_target_another_user(self, portal, service):
        """The user id is inside the signed payload, so it cannot be swapped."""
        base = portal._http.base_url  # noqa: SLF001
        portal.create_link(42)
        # Re-signing with a different id yields a nonce the portal never issued.
        token = make_token(SECRET, "login:99:borrowed", int(time.time()) + 60)
        assert (await get(f"{base}/pikpak/login/{token}"))[0] == 410
        assert service.logins == []


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
        instance = PikPakLoginPortal(
            service,
            PikPakConfig(allow_user_login=False),
            http,
            SECRET,
            bot_token=BOT_TOKEN,
        )
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


class TestMiniAppAvailability:
    async def test_plain_http_gets_no_miniapp_url(self, portal):
        # Telegram only opens web_app buttons over HTTPS, so loopback http
        # must fall back to the one-time link.
        assert portal.miniapp_url is None

    def test_https_gets_a_miniapp_url(self, service):
        http = HttpConfig(enabled=True, public_base_url="https://media.example.com")
        instance = PikPakLoginPortal(
            service, PikPakConfig(), http, SECRET, bot_token=BOT_TOKEN
        )
        instance.register(_DummyRouter())
        assert instance.miniapp_url == "https://media.example.com/pikpak/app"

    def test_unregistered_portal_has_no_miniapp_url(self, service):
        http = HttpConfig(enabled=True, public_base_url="https://media.example.com")
        instance = PikPakLoginPortal(
            service, PikPakConfig(), http, SECRET, bot_token=BOT_TOKEN
        )
        assert instance.miniapp_url is None

    def test_disabled_logins_have_no_miniapp_url(self, service):
        http = HttpConfig(enabled=True, public_base_url="https://media.example.com")
        instance = PikPakLoginPortal(
            service,
            PikPakConfig(allow_user_login=False),
            http,
            SECRET,
            bot_token=BOT_TOKEN,
        )
        instance.register(_DummyRouter())
        assert instance.miniapp_url is None
