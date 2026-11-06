"""M9.5.1: a captcha token is never stored in, or restored from, a PikPak session.

A captcha is minted for one action; a stored copy makes every restored client
send a stale ``X-Captcha-Token``. Fake SDK only, no network.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest
from pikpakapi import PikPakApi
from pikpakapi.PikpakException import PikpakException

import tgmd.pikpak as pikpak_module
from pikpak_wms.core.auth import StandaloneAuth, read_token, write_token
from tgmd.config import PikPakConfig
from tgmd.db import Database
from tgmd.pikpak import TOKEN_KEY, PikPakService, user_token_key

STALE = "stale-captcha-value"

SAVED = {
    "access_token": "a",
    "refresh_token": "r",
    "encoded_token": "e",
    "device_id": "device-1",
    "captcha_token": STALE,
    "user_agent": "derived-agent",
}


class FakeSdk:
    """Stands in for PikPakApi inside the service."""

    probe_errors: ClassVar[list[Exception]] = []
    probes: ClassVar[list[str | None]] = []

    def __init__(self, **kwargs):
        self.captcha_token = None
        self.user_agent = None
        self.token_refresh_callback = None
        self.token_refresh_callback_kwargs = {}
        self.encoded_token = "e"
        self.__dict__.update({k: v for k, v in kwargs.items() if k != "token_refresh_callback"})

    @classmethod
    def from_dict(cls, data):
        client = cls()
        client.__dict__.update(data)
        return client

    def encode_token(self):
        pass

    def to_dict(self):
        data = {
            k: v
            for k, v in self.__dict__.items()
            if isinstance(v, (str, int, float, bool, list, dict, type(None)))
        }
        data.update(username="user@example.com", password="secret")
        return data

    async def get_quota_info(self):
        FakeSdk.probes.append(self.captcha_token)
        if FakeSdk.probe_errors:
            raise FakeSdk.probe_errors.pop(0)
        return {"quota": {"limit": "1", "usage": "0"}}


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch):
    FakeSdk.probe_errors = []
    FakeSdk.probes = []
    monkeypatch.setattr(pikpak_module, "PikPakApi", FakeSdk)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "captcha.sqlite3")
    await database.connect()
    yield database
    await database.close()


def service(db) -> PikPakService:
    return PikPakService(PikPakConfig(enabled=True, username="u", password="p"), db)


async def test_persist_drops_the_captcha_and_keeps_the_rest(db):
    client = FakeSdk(device_id="device-1", access_token="a")
    client.captcha_token = STALE
    client.user_agent = "derived-agent"

    await service(db)._persist(client, key=TOKEN_KEY)  # noqa: SLF001

    stored = await db.kv_get_json(TOKEN_KEY)
    assert "captcha_token" not in stored
    assert "user_agent" not in stored
    assert "password" not in stored
    assert stored["device_id"] == "device-1"
    assert stored["access_token"] == "a"
    assert STALE not in json.dumps(stored)


async def test_restore_heals_an_old_record_and_rewrites_it_clean(db):
    await db.kv_set_json(user_token_key(7), dict(SAVED))

    client = await service(db)._restore(user_token_key(7))  # noqa: SLF001

    assert client is not None
    assert client.captcha_token is None
    assert FakeSdk.probes == [None]
    stored = await db.kv_get_json(user_token_key(7))
    assert "captcha_token" not in stored
    assert "user_agent" not in stored
    assert stored["device_id"] == "device-1"
    assert stored["refresh_token"] == "r"


async def test_a_clean_record_is_not_rewritten(db, monkeypatch):
    await db.kv_set_json(TOKEN_KEY, {"access_token": "a", "encoded_token": "e"})
    writes = []
    original = db.kv_set_json

    async def spy(key, value):
        writes.append(key)
        await original(key, value)

    monkeypatch.setattr(db, "kv_set_json", spy)
    assert await service(db)._restore(TOKEN_KEY) is not None  # noqa: SLF001
    assert writes == []


async def test_a_captcha_error_on_the_probe_is_retried_once(db):
    await db.kv_set_json(TOKEN_KEY, dict(SAVED))
    FakeSdk.probe_errors = [PikpakException("Verification code is invalid")]

    client = await service(db)._restore(TOKEN_KEY)  # noqa: SLF001

    assert client is not None
    assert len(FakeSdk.probes) == 2
    assert client.captcha_token is None


async def test_a_second_captcha_error_means_the_session_is_unusable(db):
    await db.kv_set_json(TOKEN_KEY, dict(SAVED))
    FakeSdk.probe_errors = [PikpakException("captcha_invalid"), PikpakException("captcha_invalid")]

    assert await service(db)._restore(TOKEN_KEY) is None  # noqa: SLF001
    assert len(FakeSdk.probes) == 2


async def test_other_probe_errors_are_not_retried(db):
    await db.kv_set_json(TOKEN_KEY, dict(SAVED))
    FakeSdk.probe_errors = [PikpakException("invalid refresh token")]

    assert await service(db)._restore(TOKEN_KEY) is None  # noqa: SLF001
    assert len(FakeSdk.probes) == 1


def test_the_standalone_token_file_never_holds_a_captcha(tmp_path):
    path = tmp_path / "token.json"
    client = FakeSdk(device_id="device-1")
    client.captcha_token = STALE
    client.user_agent = "derived-agent"

    write_token(path, client)

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert "captcha_token" not in stored and "user_agent" not in stored
    assert stored["device_id"] == "device-1"


async def test_the_standalone_restore_ignores_a_captcha_in_an_old_file(tmp_path, monkeypatch):
    import pikpak_wms.core.auth as auth

    monkeypatch.setattr(auth, "PikPakApi", FakeSdk)
    path = tmp_path / "token.json"
    path.write_text(json.dumps(SAVED), encoding="utf-8")
    assert read_token(path) is not None

    client = await StandaloneAuth(path)._restore()  # noqa: SLF001

    assert client is not None
    assert client.captcha_token is None
    assert client.device_id == "device-1"


def test_the_real_sdk_serialises_what_we_strip():
    # Guards the field names: if the SDK renames them this fails, not production.
    client = PikPakApi(username="u", password="p")
    assert {"captcha_token", "user_agent"} <= set(client.to_dict())
