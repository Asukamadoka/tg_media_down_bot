"""PikPak integration.

The live API cannot be exercised in tests, so this does two things instead:
it checks that the methods this project calls still have the signatures it
calls them with (so a pikpakapi upgrade fails here rather than in production),
and it drives the service's own logic against a stub client.
"""

from __future__ import annotations

import inspect

import pytest
from pikpakapi import DownloadStatus, PikPakApi
from pikpakapi.PikpakException import PikpakException

from tgmd.config import PikPakConfig
from tgmd.db import Database
from tgmd.pikpak import (
    TOKEN_KEY,
    PikPakError,
    PikPakService,
    strip_credentials,
    user_token_key,
)


class TestLibraryContract:
    """Guards against an upstream change in the methods we depend on."""

    @pytest.mark.parametrize(
        ("method", "required"),
        [
            ("login", set()),
            ("offline_download", {"file_url", "parent_id", "name"}),
            ("path_to_id", {"path", "create"}),
            ("offline_list", {"size", "phase"}),
            ("get_share_info", {"share_link", "pass_code"}),
            ("restore", {"share_id", "pass_code_token", "file_ids"}),
            ("get_quota_info", set()),
            ("to_dict", set()),
        ],
    )
    def test_signature(self, method, required):
        function = getattr(PikPakApi, method)
        params = set(inspect.signature(function).parameters) - {"self"}
        assert required <= params, f"{method} lost parameters: {required - params}"

    def test_the_calls_we_make_are_coroutines(self):
        for name in (
            "login",
            "offline_download",
            "path_to_id",
            "offline_list",
            "get_share_info",
            "restore",
            "get_quota_info",
        ):
            assert inspect.iscoroutinefunction(getattr(PikPakApi, name)), name

    def test_there_is_still_no_upload_method(self):
        # The whole HTTP-file-server detour exists because of this. If PikPak
        # ever grows a real upload endpoint, this test should fail loudly so
        # the simpler path can replace it.
        assert not [
            name for name in dir(PikPakApi) if "upload" in name and not name.startswith("_")
        ]

    def test_download_status_values(self):
        assert {DownloadStatus.done, DownloadStatus.error, DownloadStatus.not_found}


def phase_page(phase, *, task_id="t1", file_id="f1", message=""):
    """PikPak's task list with our task in ``phase``, behind someone else's."""
    return {
        "tasks": [
            {"id": "other", "phase": "PHASE_TYPE_ERROR", "message": "not ours"},
            {"id": task_id, "phase": phase, "file_id": file_id, "message": message},
        ]
    }


class FakeClient:
    """Records calls and returns canned PikPak responses."""

    def __init__(self, **responses):
        self.responses = responses
        self.calls: list[tuple] = []

    async def path_to_id(self, path, create=False):
        self.calls.append(("path_to_id", path, create))
        return self.responses.get("path_to_id", [{"id": "folder-1", "name": path}])

    async def offline_download(self, file_url, parent_id=None, name=None):
        self.calls.append(("offline_download", file_url, parent_id, name))
        return self.responses.get(
            "offline_download",
            {"task": {"id": "t1", "file_id": "f1", "file_name": name or "x"}},
        )

    async def offline_list(self, size=10000, next_page_token=None, phase=None):
        self.calls.append(("offline_list", size, tuple(phase or ())))
        answer = self.responses.get("offline_list", phase_page("PHASE_TYPE_COMPLETE"))
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def get_share_info(self, share_link, pass_code=None):
        self.calls.append(("get_share_info", share_link, pass_code))
        return self.responses.get(
            "get_share_info",
            {
                "share_status": "OK",
                "pass_code_token": "tok",
                "files": [{"id": "a", "name": "one.mkv"}, {"id": "b", "name": "two.mkv"}],
            },
        )

    async def restore(self, share_id, pass_code_token, file_ids):
        self.calls.append(("restore", share_id, pass_code_token, file_ids))
        return {}

    async def get_quota_info(self):
        self.calls.append(("get_quota_info",))
        return self.responses.get(
            "get_quota_info", {"quota": {"limit": "1000", "usage": "250"}}
        )

    encoded_token: str | None = None

    def encode_token(self):
        self.encoded_token = "encoded-token-value"

    def to_dict(self):
        # Mirrors the real client, which serialises the credentials too. The
        # service is expected to strip them before they reach the database.
        return {
            "access_token": "a",
            "refresh_token": "r",
            "encoded_token": self.encoded_token,
            "username": "user@example.com",
            "password": "secret",
        }


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "pikpak.sqlite3")
    await database.connect()
    yield database
    await database.close()


def make_service(db, client: FakeClient | None = None, **overrides) -> PikPakService:
    config = PikPakConfig(
        enabled=True,
        username="user@example.com",
        password="secret",
        folder=overrides.pop("folder", "/TelegramMedia"),
        task_timeout=overrides.pop("task_timeout", 5),
    )
    service = PikPakService(config, db)
    if client is not None:
        service._shared = client  # noqa: SLF001 - injecting the stub is the point
    return service


class TestConfiguration:
    async def test_unconfigured_service_refuses_to_build_a_client(self, db):
        service = PikPakService(PikPakConfig(), db)
        assert not service.configured
        with pytest.raises(PikPakError, match="no PikPak account is connected"):
            await service.client()

    async def test_credentials_make_it_configured(self, db):
        assert make_service(db).configured

    async def test_unconfigured_service_is_unavailable_to_a_user(self, db):
        service = PikPakService(PikPakConfig(), db)
        assert not await service.available_for(42)

    async def test_shared_account_is_available_to_everyone(self, db):
        assert await make_service(db, FakeClient()).available_for(42)


class TestFolders:
    async def test_root_needs_no_client(self, db):
        service = make_service(db, folder="/")
        assert await service.folder_id(None) is None

    async def test_explicit_root_is_none(self, db):
        service = make_service(db, FakeClient())
        assert await service.folder_id("/") is None

    async def test_folder_is_created_on_demand(self, db):
        client = FakeClient()
        service = make_service(db, client)
        assert await service.folder_id("/Movies") == "folder-1"
        assert ("path_to_id", "/Movies", True) in client.calls

    async def test_default_folder_is_used_when_none_given(self, db):
        client = FakeClient()
        service = make_service(db, client)
        await service.folder_id(None)
        assert ("path_to_id", "/TelegramMedia", True) in client.calls

    async def test_empty_resolution_is_an_error(self, db):
        service = make_service(db, FakeClient(path_to_id=[]))
        with pytest.raises(PikPakError, match="could not create"):
            await service.folder_id("/Nope")


class TestOfflineDownload:
    async def test_task_fields_are_extracted(self, db):
        service = make_service(db, FakeClient())
        task = await service.offline_download("magnet:?xt=urn:btih:abc", name="film.mkv")
        assert (task.task_id, task.file_id, task.name) == ("t1", "f1", "film.mkv")
        assert task.known

    async def test_file_id_falls_back_to_the_file_object(self, db):
        client = FakeClient(
            offline_download={"file": {"id": "from-file", "name": "n.bin"}}
        )
        service = make_service(db, client)
        task = await service.offline_download("https://example.com/a")
        assert task.file_id == "from-file"
        assert task.name == "n.bin"

    async def test_empty_response_yields_an_unknown_task(self, db):
        service = make_service(db, FakeClient(offline_download={}))
        task = await service.offline_download("https://example.com/a")
        assert not task.known

    async def test_url_and_folder_are_passed_through(self, db):
        client = FakeClient()
        service = make_service(db, client)
        await service.offline_download("https://example.com/a", folder="/X", name="a")
        assert ("offline_download", "https://example.com/a", "folder-1", "a") in client.calls

    async def test_a_task_without_a_file_id_is_still_followed(self, db):
        # PikPak answers a new URL task with an empty file_id. Treating that
        # as "unknown" skipped the wait and told the user it was fetching.
        client = FakeClient(offline_download={"task": {"id": "t1", "file_id": ""}})
        service = make_service(db, client)
        task = await service.offline_download("https://example.com/a")
        assert task.known
        assert await service.wait_for_task(task) is DownloadStatus.done
        assert task.file_id == "f1"

    async def test_waiting_on_nothing_reports_not_found(self, db):
        service = make_service(db, FakeClient(offline_download={}))
        task = await service.offline_download("https://example.com/a")
        assert await service.wait_for_task(task) is DownloadStatus.not_found

    async def test_a_file_without_a_task_is_already_done(self, db):
        client = FakeClient(offline_download={"file": {"id": "f9", "name": "n.bin"}})
        service = make_service(db, client)
        task = await service.offline_download("https://example.com/a")
        assert await service.wait_for_task(task) is DownloadStatus.done

    async def test_completed_task_is_reported_done(self, db):
        client = FakeClient()
        service = make_service(db, client)
        task = await service.offline_download("https://example.com/a")
        assert await service.wait_for_task(task) is DownloadStatus.done
        phases = next(call[2] for call in client.calls if call[0] == "offline_list")
        assert set(phases) == {
            "PHASE_TYPE_PENDING", "PHASE_TYPE_RUNNING",
            "PHASE_TYPE_COMPLETE", "PHASE_TYPE_ERROR",
        }

    async def test_a_pending_task_is_not_done(self, db, monkeypatch):
        # The bug seen on the NAS: PikPak creates the target file the moment
        # a task is queued, and pikpakapi's get_task_status() took that file
        # for a finished transfer.
        monkeypatch.setattr("tgmd.pikpak._POLL_INTERVAL", 0.01)
        client = FakeClient(offline_list=phase_page("PHASE_TYPE_PENDING"))
        service = make_service(db, client, task_timeout=0.05)
        task = await service.offline_download("https://example.com/a")
        assert await service.wait_for_task(task) is DownloadStatus.downloading

    async def test_a_failed_task_is_an_error_with_pikpaks_reason(self, db):
        client = FakeClient(
            offline_list=phase_page("PHASE_TYPE_ERROR", message="Download timed out")
        )
        service = make_service(db, client)
        task = await service.offline_download("https://example.com/a")
        assert await service.wait_for_task(task) is DownloadStatus.error
        assert task.message == "Download timed out"

    async def test_a_failed_status_check_is_not_a_failed_transfer(self, db, monkeypatch):
        # A failed list request means "could not tell this time". The task
        # may well still be running, so that is never final.
        monkeypatch.setattr("tgmd.pikpak._POLL_INTERVAL", 0.01)
        client = FakeClient(offline_list=PikpakException("network down"))
        service = make_service(db, client, task_timeout=0.05)
        task = await service.offline_download("https://example.com/a")
        assert await service.wait_for_task(task) is DownloadStatus.downloading

    async def test_polling_carries_on_until_the_task_finishes(self, db, monkeypatch):
        monkeypatch.setattr("tgmd.pikpak._POLL_INTERVAL", 0.0)
        client = SequencedClient([
            phase_page("PHASE_TYPE_PENDING"),
            PikpakException("blip"),
            {"tasks": []},
            phase_page("PHASE_TYPE_RUNNING"),
            phase_page("PHASE_TYPE_COMPLETE"),
        ])
        service = make_service(db, client)
        task = await service.offline_download("https://example.com/a")
        assert await service.wait_for_task(task) is DownloadStatus.done


class SequencedClient(FakeClient):
    """Answers task-list requests from a script, then repeats the last one."""

    def __init__(self, pages):
        super().__init__()
        self._pages = list(pages)

    async def offline_list(self, size=10000, next_page_token=None, phase=None):
        self.calls.append(("offline_list", size, tuple(phase or ())))
        page = self._pages.pop(0) if len(self._pages) > 1 else self._pages[0]
        if isinstance(page, Exception):
            raise page
        return page


class TestShareLinks:
    async def test_files_are_restored(self, db):
        client = FakeClient()
        service = make_service(db, client)
        names = await service.restore_share("https://mypikpak.com/s/ABC123")
        assert names == ["one.mkv", "two.mkv"]
        assert ("restore", "ABC123", "tok", ["a", "b"]) in client.calls

    async def test_a_non_share_url_is_rejected(self, db):
        service = make_service(db, FakeClient())
        with pytest.raises(PikPakError, match="not a PikPak share link"):
            await service.restore_share("https://example.com/whatever")

    async def test_a_bad_status_is_reported(self, db):
        client = FakeClient(get_share_info={"share_status": "SHARE_STATUS_DELETED"})
        service = make_service(db, client)
        with pytest.raises(PikPakError, match="not usable"):
            await service.restore_share("https://mypikpak.com/s/ABC")

    async def test_an_empty_share_is_reported(self, db):
        client = FakeClient(get_share_info={"share_status": "OK", "files": []})
        service = make_service(db, client)
        with pytest.raises(PikPakError, match="no files"):
            await service.restore_share("https://mypikpak.com/s/ABC")

    async def test_an_unexpected_response_is_reported(self, db):
        service = make_service(db, FakeClient(get_share_info=["not", "a", "dict"]))
        with pytest.raises(PikPakError, match="unexpected"):
            await service.restore_share("https://mypikpak.com/s/ABC")


class TestQuota:
    async def test_values_are_parsed(self, db):
        service = make_service(db, FakeClient())
        quota = await service.quota()
        assert (quota.used, quota.limit, quota.free) == (250, 1000, 750)
        assert quota.fraction == pytest.approx(0.25)

    async def test_missing_quota_is_zeroed(self, db):
        service = make_service(db, FakeClient(get_quota_info={}))
        quota = await service.quota()
        assert (quota.used, quota.limit) == (0, 0)
        assert quota.fraction == 0.0

    async def test_garbage_quota_does_not_raise(self, db):
        client = FakeClient(get_quota_info={"quota": {"limit": "lots", "usage": "some"}})
        quota = await make_service(db, client).quota()
        assert (quota.used, quota.limit) == (0, 0)


class TestSessionPersistence:
    async def test_tokens_are_written_on_refresh(self, db):
        service = make_service(db)
        await service._persist(FakeClient())  # noqa: SLF001 - the refresh callback
        stored = await db.kv_get_json(TOKEN_KEY)
        assert stored["access_token"] == "a"
        assert stored["refresh_token"] == "r"
        assert stored["encoded_token"] == "encoded-token-value"

    async def test_the_password_is_never_stored(self, db):
        service = make_service(db)
        await service._persist(FakeClient())  # noqa: SLF001
        stored = await db.kv_get_json(TOKEN_KEY)
        assert "password" not in stored
        assert "username" not in stored
        assert "secret" not in str(stored)

    async def test_strip_credentials_keeps_everything_else(self):
        cleaned = strip_credentials(
            {"username": "u", "password": "p", "access_token": "a", "device_id": "d"}
        )
        assert cleaned == {"access_token": "a", "device_id": "d"}

    async def test_logout_clears_the_stored_session(self, db):
        service = make_service(db, FakeClient())
        await service._persist(FakeClient())  # noqa: SLF001
        await service.logout()
        assert await db.kv_get(TOKEN_KEY) is None


class TestPerUserSessions:
    async def test_a_user_starts_with_no_session(self, db):
        service = make_service(db, FakeClient())
        assert not await service.has_user_session(42)

    async def test_a_stored_token_counts_as_a_session(self, db):
        service = make_service(db, FakeClient())
        await db.kv_set_json(user_token_key(42), {"encoded_token": "t"})
        assert await service.has_user_session(42)

    async def test_a_token_without_credentials_does_not_count(self, db):
        service = make_service(db, FakeClient())
        await db.kv_set_json(user_token_key(42), {"device_id": "d"})
        assert not await service.has_user_session(42)

    async def test_the_users_own_client_is_preferred(self, db):
        shared = FakeClient()
        own = FakeClient()
        service = make_service(db, shared)
        service._users[42] = own  # noqa: SLF001
        assert await service.client(42) is own
        assert await service.client(7) is shared
        assert await service.client() is shared

    async def test_key_is_namespaced_per_user(self):
        assert user_token_key(42) == f"{TOKEN_KEY}:42"
        assert user_token_key(7) != user_token_key(42)

    async def test_logout_only_affects_that_user(self, db):
        service = make_service(db, FakeClient())
        await db.kv_set_json(user_token_key(42), {"encoded_token": "t"})
        await db.kv_set_json(user_token_key(7), {"encoded_token": "t"})
        await service.logout(42)
        assert not await service.has_user_session(42)
        assert await service.has_user_session(7)

    async def test_logout_does_not_clear_the_shared_session(self, db):
        service = make_service(db, FakeClient())
        await service._persist(FakeClient())  # noqa: SLF001
        await service.logout(42)
        assert await db.kv_get(TOKEN_KEY) is not None

    async def test_a_users_transfers_use_their_own_client(self, db):
        shared = FakeClient()
        own = FakeClient()
        service = make_service(db, shared)
        service._users[42] = own  # noqa: SLF001
        await service.offline_download("magnet:?xt=urn:btih:abc", user_id=42)
        assert any(call[0] == "offline_download" for call in own.calls)
        assert not any(call[0] == "offline_download" for call in shared.calls)

    async def test_a_dead_own_session_never_falls_back_to_the_shared_one(
        self, db, monkeypatch
    ):
        # The user expects files in their own drive. Quietly using the shared
        # account would put them in someone else's, so they are told instead,
        # on every attempt and not just the first.
        shared = FakeClient()
        service = make_service(db, shared)
        await db.kv_set_json(user_token_key(42), {"encoded_token": "expired"})

        async def dead(_key):
            return None

        monkeypatch.setattr(service, "_restore", dead)
        for _ in range(2):
            with pytest.raises(PikPakError, match="/pikpak login"):
                await service.offline_download("magnet:?xt=urn:btih:abc", user_id=42)
        assert not any(call[0] == "offline_download" for call in shared.calls)
