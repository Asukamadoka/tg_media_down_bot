"""WmsClient: the one place that talks to PikPak.

* Every call waits on the global token bucket first.
* Rate-limit refusals are retried with exponential back-off.
* SDK exceptions become :mod:`pikpak_wms.core.errors`; SDK dicts become
  :mod:`pikpak_wms.core.models`.

The client is handed a *provider*, a callable returning a logged-in
``PikPakApi``, instead of logging in itself. The bot passes one that reuses
the account the user already connected; the command line passes one that
logs in from the environment (:mod:`pikpak_wms.core.auth`). If pikpakapi
ever breaks, this file is the one to change.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from pikpakapi.PikpakException import PikpakException

from .errors import AuthError, NotFoundError, RateLimitedError, WmsError
from .models import ROOT_ID, FileNode, Quota, join_path, normalize_path
from .ratelimit import TokenBucket

log = logging.getLogger(__name__)

Provider = Callable[[], Awaitable[Any]]

# Phrases PikPak uses when refusing for going too fast. Matched loosely: the
# SDK passes on the server's error_description, which is not a stable code.
_RATE_LIMIT_HINTS = ("too frequent", "too many", "rate limit", "frequency", "429")
_AUTH_HINTS = ("invalid username or password", "unauthenticated", "invalid_grant", "token")


def _looks_like(message: str, hints: tuple[str, ...]) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in hints)


class WmsClient:
    def __init__(
        self,
        provider: Provider,
        *,
        limiter: TokenBucket,
        max_retries: int = 3,
        backoff: float = 3.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._provider = provider
        self._limiter = limiter
        self._max_retries = max_retries
        self._backoff = backoff
        self._sleep = sleep
        self.calls = 0
        """Requests actually sent, for reports such as stocktake's."""

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        api = await self._provider()
        for attempt in range(self._max_retries + 1):
            await self._limiter.acquire()
            self.calls += 1
            try:
                return await getattr(api, method)(*args, **kwargs)
            except PikpakException as exc:
                message = str(exc)
                if _looks_like(message, _RATE_LIMIT_HINTS):
                    if attempt < self._max_retries:
                        delay = self._backoff * 2**attempt
                        log.info("PikPak rate-limited %s; waiting %.0fs", method, delay)
                        await self._sleep(delay)
                        continue
                    raise RateLimitedError(f"{method}: {message}") from exc
                if _looks_like(message, _AUTH_HINTS):
                    raise AuthError(f"{method}: {message}") from exc
                raise WmsError(f"{method}: {message}") from exc
        raise RateLimitedError(method)  # pragma: no cover - loop always returns or raises

    # -------------------------------------------------------------- reading

    async def list_folder(
        self, folder_id: str, *, parent_path: str, page_size: int = 100
    ) -> AsyncIterator[FileNode]:
        """Every entry of one folder, page by page. Trashed files are left out."""
        token: str | None = None
        while True:
            page = await self._call(
                "file_list",
                size=page_size,
                parent_id=folder_id or None,
                next_page_token=token,
            )
            for raw in page.get("files") or []:
                node = FileNode.from_api(raw, parent_path=parent_path)
                node.parent_id = folder_id
                yield node
            token = page.get("next_page_token") or None
            if not token:
                return

    async def resolve_path(self, path: str) -> str:
        """The file id at ``path``; the root is ``ROOT_ID``."""
        path = normalize_path(path)
        if path == "/":
            return ROOT_ID
        found = await self._call("path_to_id", path, create=False)
        if not found or len(found) < len([p for p in path.split("/") if p]):
            raise NotFoundError(f"{path} does not exist")
        return str(found[-1].get("id") or "")

    async def quota(self) -> Quota:
        info = await self._call("get_quota_info")
        raw = info.get("quota") or {}
        try:
            return Quota(
                used=int(raw.get("usage") or 0),
                limit=int(raw.get("limit") or 0),
                in_trash=int(raw.get("usage_in_trash") or 0),
            )
        except (TypeError, ValueError):
            return Quota(used=0, limit=0)

    async def events(self, *, page_size: int = 100, token: str | None = None) -> dict:
        return await self._call("events", size=page_size, next_page_token=token)

    async def download_url(self, file_id: str) -> str:
        info = await self._call("get_download_url", file_id)
        link = info.get("web_content_link")
        if not link:
            for media in info.get("medias") or []:
                link = ((media or {}).get("link") or {}).get("url")
                if link:
                    break
        if not link:
            raise WmsError(f"PikPak gave no download link for {file_id}")
        return str(link)

    # -------------------------------------------------------------- writing

    async def create_folder(self, name: str, parent_id: str, *, parent_path: str) -> FileNode:
        result = await self._call("create_folder", name=name, parent_id=parent_id or None)
        raw = result.get("file") or result
        node = FileNode.from_api(raw, parent_path=parent_path)
        node.parent_id = parent_id
        return node

    async def ensure_folder(self, path: str) -> str:
        """The id of the folder at ``path``, creating any missing levels."""
        chain = await self.ensure_folder_chain(path)
        return chain[-1][1] if chain else ROOT_ID

    async def ensure_folder_chain(self, path: str) -> list[tuple[str, str]]:
        """``[(path, id), ...]`` for every level of ``path``, creating missing ones."""
        path = normalize_path(path)
        if path == "/":
            return []
        parts = [p for p in path.split("/") if p]
        found = await self._call("path_to_id", path, create=True)
        if not found or len(found) < len(parts):
            raise WmsError(f"could not create {path}")
        return [
            ("/" + "/".join(parts[: index + 1]), str(level.get("id") or ""))
            for index, level in enumerate(found[: len(parts)])
        ]

    async def rename(self, file_id: str, name: str) -> None:
        await self._call("file_rename", file_id, name)

    async def move(self, file_ids: list[str], to_parent_id: str) -> None:
        await self._call("file_batch_move", file_ids, to_parent_id=to_parent_id or None)

    async def copy(self, file_ids: list[str], to_parent_id: str) -> None:
        await self._call("file_batch_copy", file_ids, to_parent_id=to_parent_id or None)

    async def trash(self, file_ids: list[str]) -> None:
        await self._call("delete_to_trash", file_ids)

    async def untrash(self, file_ids: list[str]) -> None:
        await self._call("untrash", file_ids)

    async def delete_forever(self, file_ids: list[str]) -> None:
        # Guarded by the caller (rule 2); here it is just a call.
        await self._call("delete_forever", file_ids)

    async def star(self, file_ids: list[str]) -> None:
        await self._call("file_batch_star", file_ids)

    async def unstar(self, file_ids: list[str]) -> None:
        await self._call("file_batch_unstar", file_ids)

    async def share(
        self, file_ids: list[str], *, need_password: bool = False, days: int = -1
    ) -> dict:
        return await self._call(
            "file_batch_share", file_ids, need_password=need_password, expiration_days=days
        )

    # ------------------------------------------------------------- inbound

    async def offline_download(self, url: str, parent_id: str, name: str | None = None) -> dict:
        return await self._call(
            "offline_download", url, parent_id=parent_id or None, name=name
        )

    async def share_info(self, share_url: str, pass_code: str = "") -> dict:
        info = await self._call("get_share_info", share_url, pass_code=pass_code)
        if not isinstance(info, dict):
            raise WmsError("PikPak returned an unexpected share response")
        return info

    async def restore_share(self, share_id: str, pass_code_token: str, ids: list[str]) -> None:
        await self._call("restore", share_id, pass_code_token, ids)

    async def offline_tasks(self, *, page_size: int = 100) -> list[dict]:
        """The account's recent offline downloads, every phase, newest first."""
        page = await self._call(
            "offline_list",
            size=page_size,
            phase=[
                "PHASE_TYPE_PENDING", "PHASE_TYPE_RUNNING",
                "PHASE_TYPE_COMPLETE", "PHASE_TYPE_ERROR",
            ],
        )
        return list(page.get("tasks") or [])


def child_path(parent: str, name: str) -> str:
    return join_path(parent, name)
