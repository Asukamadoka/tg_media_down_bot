"""Persistence layer, exercised against a real SQLite file."""

from __future__ import annotations

import pytest

from tgmd.db import Database, cache_key


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    await database.connect()
    yield database
    await database.close()


class TestUsers:
    async def test_unknown_user_is_none(self, db):
        assert await db.get_user(1) is None

    async def test_mode_round_trip(self, db):
        await db.set_user_mode(1, "pikpak")
        record = await db.get_user(1)
        assert record is not None and record["mode"] == "pikpak"

    async def test_mode_is_updated_not_duplicated(self, db):
        await db.set_user_mode(1, "local")
        await db.set_user_mode(1, "telegram")
        record = await db.get_user(1)
        assert record is not None and record["mode"] == "telegram"

    async def test_folder_and_mode_coexist(self, db):
        await db.set_user_mode(1, "pikpak")
        await db.set_user_pikpak_dir(1, "/Movies")
        record = await db.get_user(1)
        assert record is not None
        assert record["mode"] == "pikpak"
        assert record["pikpak_dir"] == "/Movies"

    async def test_folder_set_before_mode(self, db):
        await db.set_user_pikpak_dir(2, "/A")
        await db.set_user_mode(2, "local")
        record = await db.get_user(2)
        assert record is not None
        assert record["pikpak_dir"] == "/A"
        assert record["mode"] == "local"


class TestMediaCache:
    async def test_miss_then_hit(self, db):
        key = cache_key(-1001234, 55)
        assert await db.cache_lookup(key) is None
        await db.cache_store(key, -100999, 7, "clip.mp4", 1024)
        entry = await db.cache_lookup(key)
        assert entry is not None
        assert entry["cache_chat_id"] == -100999
        assert entry["cache_msg_id"] == 7
        assert entry["file_name"] == "clip.mp4"

    async def test_store_is_idempotent(self, db):
        key = cache_key(1, 1)
        await db.cache_store(key, 10, 1, "a", 1)
        await db.cache_store(key, 20, 2, "b", 2)
        entry = await db.cache_lookup(key)
        assert entry is not None and entry["cache_msg_id"] == 2

    async def test_forget(self, db):
        key = cache_key(1, 1)
        await db.cache_store(key, 10, 1, "a", 1)
        await db.cache_forget(key)
        assert await db.cache_lookup(key) is None

    def test_key_shape(self):
        assert cache_key(-1001234567890, 42) == "-1001234567890:42"


class TestJobs:
    async def test_record_returns_increasing_ids(self, db):
        first = await db.record_job(1, "link-a", "telegram")
        second = await db.record_job(1, "link-b", "telegram")
        assert second > first

    async def test_finish_updates_status(self, db):
        job_id = await db.record_job(1, "link", "local")
        await db.finish_job(job_id, "done", file_name="a.mp4", file_size=2048)
        recent = await db.recent_jobs(1)
        assert recent[0]["id"] == job_id
        assert recent[0]["status"] == "done"
        assert recent[0]["file_size"] == 2048

    async def test_stats_group_by_status(self, db):
        done = await db.record_job(1, "a", "local")
        await db.finish_job(done, "done", file_size=100)
        failed = await db.record_job(1, "b", "local")
        await db.finish_job(failed, "failed", error="nope")
        stats = await db.user_stats(1)
        assert stats["done"]["count"] == 1
        assert stats["done"]["bytes"] == 100
        assert stats["failed"]["count"] == 1

    async def test_stats_are_per_user(self, db):
        mine = await db.record_job(1, "a", "local")
        await db.finish_job(mine, "done", file_size=10)
        theirs = await db.record_job(2, "b", "local")
        await db.finish_job(theirs, "done", file_size=99)
        assert await db.user_stats(1) == {"done": {"count": 1, "bytes": 10}}

    async def test_recent_respects_the_limit(self, db):
        for index in range(5):
            await db.record_job(1, f"link-{index}", "local")
        assert len(await db.recent_jobs(1, limit=3)) == 3

    async def test_recent_is_newest_first(self, db):
        first = await db.record_job(1, "old", "local")
        second = await db.record_job(1, "new", "local")
        recent = await db.recent_jobs(1)
        assert [row["id"] for row in recent] == [second, first]


class TestKeyValue:
    async def test_missing_key(self, db):
        assert await db.kv_get("nope") is None
        assert await db.kv_get_json("nope") is None

    async def test_round_trip(self, db):
        await db.kv_set("k", "v")
        assert await db.kv_get("k") == "v"

    async def test_overwrite(self, db):
        await db.kv_set("k", "one")
        await db.kv_set("k", "two")
        assert await db.kv_get("k") == "two"

    async def test_delete(self, db):
        await db.kv_set("k", "v")
        await db.kv_delete("k")
        assert await db.kv_get("k") is None

    async def test_json_round_trip(self, db):
        await db.kv_set_json("token", {"access_token": "a", "n": 1})
        assert await db.kv_get_json("token") == {"access_token": "a", "n": 1}

    async def test_corrupt_json_returns_none(self, db):
        await db.kv_set("token", "{not json")
        assert await db.kv_get_json("token") is None

    async def test_secret_is_generated_once_and_reused(self, db):
        first = await db.get_or_create_secret()
        assert first
        assert await db.get_or_create_secret() == first

    async def test_secret_survives_reopening(self, db, tmp_path):
        secret = await db.get_or_create_secret()
        await db.close()
        reopened = Database(tmp_path / "test.sqlite3")
        await reopened.connect()
        try:
            assert await reopened.get_or_create_secret() == secret
        finally:
            await reopened.close()


class TestLifecycle:
    async def test_using_before_connect_is_an_error(self, tmp_path):
        database = Database(tmp_path / "unopened.sqlite3")
        with pytest.raises(RuntimeError, match="connect"):
            _ = database.connection

    async def test_connect_creates_the_parent_directory(self, tmp_path):
        database = Database(tmp_path / "nested" / "deeper" / "db.sqlite3")
        await database.connect()
        try:
            assert (tmp_path / "nested" / "deeper").is_dir()
        finally:
            await database.close()
