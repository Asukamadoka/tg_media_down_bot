"""PikPak integration.

Two very different transfer paths live here, because PikPak's API has no
upload endpoint:

* **Magnet links, direct URLs and share links** are handed straight to PikPak.
  Nothing passes through this machine, which is what people mean by 转存.
* **Telegram media** has to be downloaded locally first, then published on the
  bot's own HTTP server so PikPak can fetch it by URL.

Sessions are per user. Each person can connect their own PikPak account
through the login Mini App or in chat, and the shared account from the
configuration is used only as a fallback for anyone who has not. Stored sessions never contain a
password: the access and refresh tokens are kept, and the credentials that
produced them are discarded immediately after login.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from pikpakapi import DownloadStatus, PikPakApi
from pikpakapi.PikpakException import PikpakException

from .config import PikPakConfig
from .db import Database

log = logging.getLogger(__name__)

TOKEN_KEY = "pikpak_client"
"""Key holding the shared account's session."""

_SHARE_ID_RE = re.compile(r"/s/([^/?#]+)")

# How often to ask PikPak whether an offline task has finished.
_POLL_INTERVAL = 5.0

# Fields that must never reach the database.
_SECRET_FIELDS = ("username", "password")


def user_token_key(user_id: int) -> str:
    """Key holding one user's own PikPak session."""
    return f"{TOKEN_KEY}:{user_id}"


class PikPakError(RuntimeError):
    """A PikPak operation failed in a way worth showing to the user."""


@dataclass
class OfflineTask:
    """Handle for an offline download queued inside PikPak."""

    task_id: str
    file_id: str
    name: str

    @property
    def known(self) -> bool:
        return bool(self.task_id and self.file_id)


@dataclass
class Quota:
    used: int
    limit: int

    @property
    def free(self) -> int:
        return max(self.limit - self.used, 0)

    @property
    def fraction(self) -> float:
        return self.used / self.limit if self.limit else 0.0


def strip_credentials(data: dict[str, Any]) -> dict[str, Any]:
    """Remove the password and username from a serialised client.

    ``PikPakApi.to_dict()`` includes both in clear text. Persisting them would
    turn the bot's database into a credential store, so only the encoded token
    is kept; it is enough to refresh the session later.
    """
    return {key: value for key, value in data.items() if key not in _SECRET_FIELDS}


class PikPakService:
    """Per-user PikPak clients, with the configured account as a fallback."""

    def __init__(self, config: PikPakConfig, db: Database) -> None:
        self._config = config
        self._db = db
        self._shared: PikPakApi | None = None
        self._users: dict[int, PikPakApi] = {}
        # Users known to have no session of their own, so the common case does
        # not hit the database on every single transfer.
        self._without_session: set[int] = set()
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        """True when a shared account is available to everyone."""
        return self._config.configured

    @property
    def user_login_allowed(self) -> bool:
        """True when users may connect their own accounts."""
        return self._config.allow_user_login

    @property
    def folder(self) -> str:
        return self._config.folder

    # --------------------------------------------------------------- sessions

    async def _persist(
        self, client: PikPakApi, *, key: str = TOKEN_KEY, **_kwargs: Any
    ) -> None:
        """Token refresh callback: keep refreshed tokens across restarts."""
        try:
            client.encode_token()
            await self._db.kv_set_json(key, strip_credentials(client.to_dict()))
        except Exception:  # pragma: no cover - persistence is best effort
            log.exception("could not persist the PikPak session at %s", key)

    async def _restore(self, key: str) -> PikPakApi | None:
        """Rebuild a client from a stored session, or return None."""
        saved = await self._db.kv_get_json(key)
        if not isinstance(saved, dict):
            return None
        if not (saved.get("encoded_token") or saved.get("access_token")):
            return None
        try:
            client = PikPakApi.from_dict(saved)
            client.token_refresh_callback = self._persist
            client.token_refresh_callback_kwargs = {"key": key}
            await client.get_quota_info()  # cheap probe, also refreshes if stale
            return client
        except (PikpakException, ValueError, KeyError) as exc:
            log.info("stored PikPak session at %s is unusable: %s", key, exc)
            return None

    async def has_user_session(self, user_id: int) -> bool:
        """True when this user has connected their own PikPak account."""
        if user_id in self._users:
            return True
        if user_id in self._without_session:
            return False
        saved = await self._db.kv_get_json(user_token_key(user_id))
        present = isinstance(saved, dict) and bool(
            saved.get("encoded_token") or saved.get("access_token")
        )
        if not present:
            self._without_session.add(user_id)
        return present

    async def available_for(self, user_id: int) -> bool:
        """True when this user can reach PikPak at all."""
        return self.configured or await self.has_user_session(user_id)

    async def _user_client(self, user_id: int) -> PikPakApi | None:
        """Load a user's own client, or None when they have not connected one."""
        cached = self._users.get(user_id)
        if cached is not None:
            return cached
        if not await self.has_user_session(user_id):
            return None
        client = await self._restore(user_token_key(user_id))
        if client is None:
            # The stored session is dead. The record stays, so every transfer
            # keeps telling the user to log in again instead of quietly
            # switching to the shared account from the second one on.
            return None
        self._users[user_id] = client
        self._without_session.discard(user_id)
        return client

    async def _shared_client(self) -> PikPakApi:
        if self._shared is not None:
            return self._shared

        restored = await self._restore(TOKEN_KEY)
        if restored is not None:
            self._shared = restored
            log.info("reused the stored shared PikPak session")
            return restored

        client = PikPakApi(
            username=self._config.username,
            password=self._config.password,
            token_refresh_callback=self._persist,
            token_refresh_callback_kwargs={"key": TOKEN_KEY},
        )
        try:
            await client.login()
        except PikpakException as exc:
            raise PikPakError(f"PikPak login failed: {exc}") from exc
        await self._persist(client, key=TOKEN_KEY)
        self._shared = client
        log.info("logged in to the shared PikPak account")
        return client

    async def client(self, user_id: int | None = None) -> PikPakApi:
        """Return a logged-in client, preferring the user's own account.

        Cached clients are returned without taking the lock, so concurrent
        transfers do not serialise behind each other.
        """
        if user_id is not None:
            cached = self._users.get(user_id)
            if cached is not None:
                return cached
            if await self.has_user_session(user_id):
                async with self._lock:
                    own = await self._user_client(user_id)
                if own is not None:
                    return own
                # Falling back to the shared account here would put this
                # user's files in somebody else's drive without telling them.
                raise PikPakError(
                    "your PikPak session has expired. Use /pikpak login to "
                    "connect your account again."
                )

        if self._shared is not None:
            return self._shared
        if not self.configured:
            raise PikPakError(
                "no PikPak account is connected and none is configured on the "
                "server. Use /pikpak login to connect yours."
            )
        async with self._lock:
            return await self._shared_client()

    async def login_with_password(
        self, user_id: int, username: str, password: str
    ) -> str:
        """Connect a user's own PikPak account, storing only the token."""
        client = PikPakApi(
            username=username,
            password=password,
            token_refresh_callback=self._persist,
            token_refresh_callback_kwargs={"key": user_token_key(user_id)},
        )
        try:
            await client.login()
        except PikpakException as exc:
            raise PikPakError(f"PikPak rejected those credentials: {exc}") from exc

        await self._persist(client, key=user_token_key(user_id))
        # Drop the credentials from memory too; the token is all we need now.
        client.password = None
        async with self._lock:
            self._users[user_id] = client
            self._without_session.discard(user_id)
        log.info("user %s connected their own PikPak account", user_id)
        return username

    async def logout(self, user_id: int | None = None) -> None:
        """Forget a stored session, forcing a fresh login next time."""
        key = TOKEN_KEY if user_id is None else user_token_key(user_id)
        async with self._lock:
            if user_id is None:
                self._shared = None
            else:
                self._users.pop(user_id, None)
                self._without_session.add(user_id)
        await self._db.kv_delete(key)

    # ---------------------------------------------------------------- folders

    async def folder_id(
        self, path: str | None, *, user_id: int | None = None
    ) -> str | None:
        """Resolve a PikPak path to a folder id, creating folders as needed.

        ``None`` or ``/`` means the account root, which PikPak represents as a
        null parent id.
        """
        folder = (path or self._config.folder or "").strip()
        if not folder or folder == "/":
            return None
        client = await self.client(user_id)
        try:
            resolved = await client.path_to_id(folder, create=True)
        except PikpakException as exc:
            raise PikPakError(f"could not open PikPak folder {folder}: {exc}") from exc
        if not resolved:
            raise PikPakError(f"could not create PikPak folder {folder}")
        return resolved[-1].get("id")

    # ------------------------------------------------------- offline download

    async def offline_download(
        self,
        url: str,
        *,
        folder: str | None = None,
        name: str | None = None,
        user_id: int | None = None,
    ) -> OfflineTask:
        """Ask PikPak to fetch ``url`` itself and store it under ``folder``."""
        client = await self.client(user_id)
        parent_id = await self.folder_id(folder, user_id=user_id)
        try:
            result = await client.offline_download(url, parent_id=parent_id, name=name)
        except PikpakException as exc:
            raise PikPakError(f"PikPak refused the transfer: {exc}") from exc

        task = result.get("task") or {}
        file_info = result.get("file") or {}
        return OfflineTask(
            task_id=str(task.get("id") or ""),
            file_id=str(task.get("file_id") or file_info.get("id") or ""),
            name=str(task.get("file_name") or file_info.get("name") or name or url),
        )

    async def wait_for_task(
        self,
        task: OfflineTask,
        *,
        timeout: int | None = None,
        user_id: int | None = None,
    ) -> DownloadStatus:
        """Poll until the task finishes, or until ``timeout`` seconds elapse."""
        if not task.known:
            return DownloadStatus.not_found

        client = await self.client(user_id)
        deadline = time.monotonic() + (timeout or self._config.task_timeout)
        # pikpakapi's get_task_status() only answers downloading, done or
        # not_found. It returns `error` when its own request failed, which
        # means "could not tell this time", not "PikPak gave up". Treating
        # that as final unpublished the file PikPak was still fetching.
        known = DownloadStatus.downloading
        while time.monotonic() < deadline:
            try:
                status = await client.get_task_status(task.task_id, task.file_id)
            except PikpakException as exc:
                status = DownloadStatus.error
                log.info("task status check failed: %s", exc)
            if status in (DownloadStatus.done, DownloadStatus.not_found):
                return status
            if status is not DownloadStatus.error:
                known = status
            await asyncio.sleep(_POLL_INTERVAL)
        return known

    # ------------------------------------------------------------ share links

    async def restore_share(
        self,
        share_url: str,
        *,
        pass_code: str | None = None,
        user_id: int | None = None,
    ) -> list[str]:
        """Save the contents of a PikPak share link into the account.

        Returns the names of the files that were saved.
        """
        match = _SHARE_ID_RE.search(share_url)
        if not match:
            raise PikPakError(f"{share_url} is not a PikPak share link")
        share_id = match.group(1)

        client = await self.client(user_id)
        try:
            info = await client.get_share_info(share_url, pass_code=pass_code or "")
        except PikpakException as exc:
            raise PikPakError(f"could not read the share link: {exc}") from exc
        if isinstance(info, ValueError):
            raise PikPakError("could not read the share link")
        if not isinstance(info, dict):
            raise PikPakError("PikPak returned an unexpected share response")

        status = info.get("share_status")
        if status and status not in ("OK", "SHARE_STATUS_OK"):
            raise PikPakError(
                f"the share link is not usable (status {status}); "
                "it may be expired or need a password"
            )

        files = info.get("files") or []
        file_ids = [f.get("id") for f in files if f.get("id")]
        if not file_ids:
            raise PikPakError("the share link contains no files")

        try:
            await client.restore(share_id, info.get("pass_code_token") or "", file_ids)
        except PikpakException as exc:
            raise PikPakError(f"saving the share failed: {exc}") from exc

        return [str(f.get("name") or f.get("id")) for f in files if f.get("id")]

    # ----------------------------------------------------------------- quota

    async def quota(self, *, user_id: int | None = None) -> Quota:
        """Read storage usage, so the bot can refuse transfers that will not fit."""
        client = await self.client(user_id)
        try:
            info = await client.get_quota_info()
        except PikpakException as exc:
            raise PikPakError(f"could not read PikPak quota: {exc}") from exc
        raw = info.get("quota") or {}
        try:
            return Quota(used=int(raw.get("usage", 0)), limit=int(raw.get("limit", 0)))
        except (TypeError, ValueError):
            return Quota(used=0, limit=0)
