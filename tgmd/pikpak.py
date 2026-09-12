"""PikPak integration.

Two very different transfer paths live here, because PikPak's API has no
upload endpoint:

* **Magnet links, direct URLs and share links** are handed straight to PikPak.
  Nothing passes through this machine, which is what people mean by 转存.
* **Telegram media** has to be downloaded locally first, then published on the
  bot's own HTTP server so PikPak can fetch it by URL.
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
_SHARE_ID_RE = re.compile(r"/s/([^/?#]+)")

# How often to ask PikPak whether an offline task has finished.
_POLL_INTERVAL = 5.0


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


class PikPakService:
    """Lazily-authenticated PikPak client with persisted tokens."""

    def __init__(self, config: PikPakConfig, db: Database) -> None:
        self._config = config
        self._db = db
        self._client: PikPakApi | None = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self._config.configured

    # --------------------------------------------------------------- session

    async def _persist(self, client: PikPakApi, **_kwargs: Any) -> None:
        """Token refresh callback: keep the refreshed tokens across restarts."""
        try:
            await self._db.kv_set_json(TOKEN_KEY, client.to_dict())
        except Exception:  # pragma: no cover - persistence is best effort
            log.exception("could not persist PikPak tokens")

    async def client(self) -> PikPakApi:
        """Return a logged-in client, restoring a saved session when possible."""
        if not self.configured:
            raise PikPakError(
                "PikPak is not configured. Set PIKPAK_USERNAME and PIKPAK_PASSWORD."
            )

        async with self._lock:
            if self._client is not None:
                return self._client

            saved = await self._db.kv_get_json(TOKEN_KEY)
            if isinstance(saved, dict) and saved.get("access_token"):
                try:
                    client = PikPakApi.from_dict(saved)
                    client.token_refresh_callback = self._persist
                    await client.get_quota_info()  # cheap probe
                    self._client = client
                    log.info("reused stored PikPak session")
                    return client
                except PikpakException as exc:
                    log.info("stored PikPak session unusable (%s), logging in again", exc)

            client = PikPakApi(
                username=self._config.username,
                password=self._config.password,
                token_refresh_callback=self._persist,
            )
            try:
                await client.login()
            except PikpakException as exc:
                raise PikPakError(f"PikPak login failed: {exc}") from exc
            await self._persist(client)
            self._client = client
            log.info("logged in to PikPak as %s", self._config.username)
            return client

    async def logout(self) -> None:
        """Forget the cached session, forcing a fresh login next time."""
        async with self._lock:
            self._client = None
        await self._db.kv_delete(TOKEN_KEY)

    # ---------------------------------------------------------------- folders

    async def folder_id(self, path: str | None) -> str | None:
        """Resolve a PikPak path to a folder id, creating folders as needed.

        ``None`` or ``/`` means the account root, which PikPak represents as a
        null parent id.
        """
        folder = (path or self._config.folder or "").strip()
        if not folder or folder == "/":
            return None
        client = await self.client()
        try:
            resolved = await client.path_to_id(folder, create=True)
        except PikpakException as exc:
            raise PikPakError(f"could not open PikPak folder {folder}: {exc}") from exc
        if not resolved:
            raise PikPakError(f"could not create PikPak folder {folder}")
        return resolved[-1].get("id")

    # ------------------------------------------------------- offline download

    async def offline_download(
        self, url: str, *, folder: str | None = None, name: str | None = None
    ) -> OfflineTask:
        """Ask PikPak to fetch ``url`` itself and store it under ``folder``."""
        client = await self.client()
        parent_id = await self.folder_id(folder)
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
        self, task: OfflineTask, *, timeout: int | None = None
    ) -> DownloadStatus:
        """Poll until the task finishes, or until ``timeout`` seconds elapse."""
        if not task.known:
            return DownloadStatus.not_found

        client = await self.client()
        deadline = time.monotonic() + (timeout or self._config.task_timeout)
        last = DownloadStatus.downloading
        while time.monotonic() < deadline:
            try:
                last = await client.get_task_status(task.task_id, task.file_id)
            except PikpakException as exc:
                log.info("task status check failed: %s", exc)
                last = DownloadStatus.error
            if last in (DownloadStatus.done, DownloadStatus.error, DownloadStatus.not_found):
                return last
            await asyncio.sleep(_POLL_INTERVAL)
        return last

    # ------------------------------------------------------------ share links

    async def restore_share(
        self, share_url: str, *, pass_code: str | None = None
    ) -> list[str]:
        """Save the contents of a PikPak share link into the account.

        Returns the names of the files that were saved.
        """
        match = _SHARE_ID_RE.search(share_url)
        if not match:
            raise PikPakError(f"{share_url} is not a PikPak share link")
        share_id = match.group(1)

        client = await self.client()
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
            await client.restore(
                share_id, info.get("pass_code_token") or "", file_ids
            )
        except PikpakException as exc:
            raise PikPakError(f"saving the share failed: {exc}") from exc

        return [str(f.get("name") or f.get("id")) for f in files if f.get("id")]

    # ----------------------------------------------------------------- quota

    async def quota(self) -> Quota:
        """Read storage usage, so the bot can refuse transfers that will not fit."""
        client = await self.client()
        try:
            info = await client.get_quota_info()
        except PikpakException as exc:
            raise PikPakError(f"could not read PikPak quota: {exc}") from exc
        raw = info.get("quota") or {}
        try:
            return Quota(used=int(raw.get("usage", 0)), limit=int(raw.get("limit", 0)))
        except (TypeError, ValueError):
            return Quota(used=0, limit=0)
