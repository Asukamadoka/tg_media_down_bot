"""Runtime claiming of admin rights and the upload cache.

The claim flow decides who owns the bot, so its refusals matter as much as
its success path: a code that still worked after the first claim, or one that
could be brute-forced, would be a standing backdoor into every chat the
reading account can see.
"""

from __future__ import annotations

import pytest

from tgmd import bootstrap
from tgmd.config import (
    AccessConfig,
    Config,
    DeliveryConfig,
    TelegramConfig,
)
from tgmd.db import Database


def make_config(*, admins=None, cache_chat_id=None) -> Config:
    return Config(
        telegram=TelegramConfig(api_id=1, api_hash="h", bot_token="123456789:x"),
        access=AccessConfig(admin_user_ids=list(admins or [])),
        delivery=DeliveryConfig(cache_chat_id=cache_chat_id),
    )


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "bootstrap.sqlite3")
    await database.connect()
    yield database
    await database.close()


class TestClaimCode:
    async def test_a_code_is_generated_once_and_reused(self, db):
        first = await bootstrap.ensure_claim_code(db)
        assert first
        assert await bootstrap.ensure_claim_code(db) == first

    async def test_codes_are_not_guessable(self, db):
        code = await bootstrap.ensure_claim_code(db)
        # token_urlsafe(8) is ~11 characters of base64url.
        assert len(code) >= 10

    async def test_two_databases_get_different_codes(self, db, tmp_path):
        other = Database(tmp_path / "other.sqlite3")
        await other.connect()
        try:
            assert await bootstrap.ensure_claim_code(db) != (
                await bootstrap.ensure_claim_code(other)
            )
        finally:
            await other.close()


class TestClaimAvailability:
    async def test_available_with_no_admin(self, db):
        assert await bootstrap.claim_available(db, make_config())

    async def test_unavailable_once_an_admin_exists(self, db):
        assert not await bootstrap.claim_available(db, make_config(admins=[42]))

    async def test_announce_returns_a_code_when_unclaimed(self, db):
        code = await bootstrap.announce_claim(db, make_config(), "mybot")
        assert code == await db.kv_get(bootstrap.CLAIM_CODE_KEY)

    async def test_announce_is_silent_when_already_claimed(self, db):
        assert await bootstrap.announce_claim(db, make_config(admins=[42]), "b") is None

    async def test_announce_survives_a_missing_username(self, db):
        assert await bootstrap.announce_claim(db, make_config(), None)


class TestClaiming:
    async def test_the_right_code_makes_you_admin(self, db):
        config = make_config()
        code = await bootstrap.ensure_claim_code(db)
        await bootstrap.claim_admin(db, config, code, 777)
        assert config.access.is_admin(777)

    async def test_the_claim_persists(self, db):
        config = make_config()
        code = await bootstrap.ensure_claim_code(db)
        await bootstrap.claim_admin(db, config, code, 777)
        assert await bootstrap.runtime_admin_ids(db) == [777]

    async def test_surrounding_whitespace_is_tolerated(self, db):
        config = make_config()
        code = await bootstrap.ensure_claim_code(db)
        await bootstrap.claim_admin(db, config, f"  {code}\n", 777)
        assert config.access.is_admin(777)

    async def test_a_wrong_code_is_refused(self, db):
        config = make_config()
        await bootstrap.ensure_claim_code(db)
        with pytest.raises(bootstrap.ClaimError, match="wrong"):
            await bootstrap.claim_admin(db, config, "not-the-code", 777)
        assert not config.access.admin_user_ids

    async def test_an_empty_code_is_refused(self, db):
        config = make_config()
        await bootstrap.ensure_claim_code(db)
        with pytest.raises(bootstrap.ClaimError):
            await bootstrap.claim_admin(db, config, "", 777)

    async def test_the_code_is_spent_after_a_successful_claim(self, db):
        config = make_config()
        code = await bootstrap.ensure_claim_code(db)
        await bootstrap.claim_admin(db, config, code, 777)
        assert await db.kv_get(bootstrap.CLAIM_CODE_KEY) is None

    async def test_a_second_claim_is_refused_even_with_the_same_code(self, db):
        config = make_config()
        code = await bootstrap.ensure_claim_code(db)
        await bootstrap.claim_admin(db, config, code, 777)
        with pytest.raises(bootstrap.ClaimError, match="already has an admin"):
            await bootstrap.claim_admin(db, config, code, 888)
        assert not config.access.is_admin(888)

    async def test_a_configured_admin_blocks_claiming_entirely(self, db):
        config = make_config(admins=[42])
        await bootstrap.ensure_claim_code(db)
        with pytest.raises(bootstrap.ClaimError, match="already has an admin"):
            await bootstrap.claim_admin(db, config, "anything", 777)

    async def test_claiming_without_an_issued_code_is_refused(self, db):
        with pytest.raises(bootstrap.ClaimError, match="no claim code"):
            await bootstrap.claim_admin(db, make_config(), "guess", 777)


class TestRuntimeAdmins:
    async def test_none_by_default(self, db):
        assert await bootstrap.runtime_admin_ids(db) == []

    async def test_adding_is_idempotent(self, db):
        config = make_config()
        await bootstrap.add_runtime_admin(db, config, 5)
        await bootstrap.add_runtime_admin(db, config, 5)
        assert await bootstrap.runtime_admin_ids(db) == [5]
        assert config.access.admin_user_ids == [5]

    async def test_corrupt_storage_is_ignored(self, db):
        await db.kv_set(bootstrap.ADMIN_IDS_KEY, '"not a list"')
        assert await bootstrap.runtime_admin_ids(db) == []

    async def test_junk_entries_are_dropped(self, db):
        await db.kv_set_json(bootstrap.ADMIN_IDS_KEY, [1, "2", None, "abc", 1])
        assert await bootstrap.runtime_admin_ids(db) == [1, 2]


class TestLoadRuntimeSettings:
    async def test_a_stored_admin_is_restored(self, db):
        await bootstrap.add_runtime_admin(db, make_config(), 777)
        fresh = make_config()
        await bootstrap.load_runtime_settings(db, fresh)
        assert fresh.access.is_admin(777)

    async def test_configured_admins_are_kept_alongside(self, db):
        await bootstrap.add_runtime_admin(db, make_config(), 777)
        fresh = make_config(admins=[42])
        await bootstrap.load_runtime_settings(db, fresh)
        assert sorted(fresh.access.admin_user_ids) == [42, 777]

    async def test_no_duplicates_on_reload(self, db):
        await bootstrap.add_runtime_admin(db, make_config(), 42)
        fresh = make_config(admins=[42])
        await bootstrap.load_runtime_settings(db, fresh)
        assert fresh.access.admin_user_ids == [42]

    async def test_a_stored_cache_chat_is_restored(self, db):
        await bootstrap.set_cache_chat(db, make_config(), -1001234567890)
        fresh = make_config()
        await bootstrap.load_runtime_settings(db, fresh)
        assert fresh.delivery.cache_chat_id == -1001234567890

    async def test_the_environment_outranks_a_stored_cache_chat(self, db):
        await bootstrap.set_cache_chat(db, make_config(), -100111)
        fresh = make_config(cache_chat_id=-100999)
        await bootstrap.load_runtime_settings(db, fresh)
        assert fresh.delivery.cache_chat_id == -100999

    async def test_nothing_stored_changes_nothing(self, db):
        fresh = make_config()
        await bootstrap.load_runtime_settings(db, fresh)
        assert fresh.access.admin_user_ids == []
        assert fresh.delivery.cache_chat_id is None


class TestCacheChat:
    async def test_setting_applies_and_persists(self, db):
        config = make_config()
        await bootstrap.set_cache_chat(db, config, -100123)
        assert config.delivery.cache_chat_id == -100123
        assert await bootstrap.runtime_cache_chat_id(db) == -100123

    async def test_clearing_applies_and_persists(self, db):
        config = make_config()
        await bootstrap.set_cache_chat(db, config, -100123)
        await bootstrap.clear_cache_chat(db, config)
        assert config.delivery.cache_chat_id is None
        assert await bootstrap.runtime_cache_chat_id(db) is None

    async def test_corrupt_storage_reads_as_absent(self, db):
        await db.kv_set(bootstrap.CACHE_CHAT_KEY, "not-an-id")
        assert await bootstrap.runtime_cache_chat_id(db) is None
