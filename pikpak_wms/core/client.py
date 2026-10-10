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

from .errors import AuthError, CaptchaError, NotFoundError, RateLimitedError, WmsError
from .models import ROOT_ID, FileNode, Quota, normalize_path
from .ratelimit import TokenBucket

log = logging.getLogger(__name__)

Provider = Callable[[], Awaitable[Any]]
Scoped = Callable[[Any, str | None], Awaitable[Any]]

# Phrases PikPak uses when refusing for going too fast. Matched loosely: the
# SDK passes on the server's error_description, which is not a stable code.
_RATE_LIMIT_HINTS = ("too frequent", "too many", "rate limit", "frequency", "429")
_CAPTCHA_HINTS = ("verification code", "captcha", "4002")
_AUTH_HINTS = ("invalid username or password", "unauthenticated", "invalid_grant", "token")
_NOT_FOUND_HINTS = ("not found", "404", "does not exist", "file_not_found", "no such file")


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

    async def _call(
        self, method: str, *args: Any, action: str | None = None,
        scoped: Scoped | None = None, **kwargs: Any,
    ) -> Any:
        """Call one SDK method; see :meth:`_run` for ``action`` and ``scoped``."""
        async def plain(api: Any) -> Any:
            return await getattr(api, method)(*args, **kwargs)

        return await self._run(method, plain, action=action, scoped=scoped)

    async def _run(
        self, label: str, invoke: Callable[[Any], Awaitable[Any]] | None, *,
        action: str | None = None, scoped: Scoped | None = None, eager: bool = False,
    ) -> Any:
        """Run ``invoke(api)`` with the rate-limit, auth and captcha handling.

        PikPak sometimes answers "Verification code is invalid": a captcha is
        needed for this one action. The first time, mint one for ``action`` and
        run ``scoped(api, token)`` with it as an explicit header; never through
        ``api.captcha_token``, which every concurrent request would carry. With
        ``eager`` the first try already is ``scoped(api, None)`` (it mints its
        own). A second refusal is a :class:`CaptchaError`.
        """
        api = await self._provider()
        token: str | None = None
        retried = False
        for attempt in range(self._max_retries + 1):
            await self._limiter.acquire()
            self.calls += 1
            try:
                if scoped is not None and (eager or retried):
                    return await scoped(api, token)
                assert invoke is not None
                return await invoke(api)
            except PikpakException as exc:
                message = str(exc)
                if _looks_like(message, _CAPTCHA_HINTS):
                    if retried or scoped is None or action is None:
                        raise CaptchaError(f"{label}: {message}") from exc
                    retried = True
                    if getattr(api, "captcha_token", None):
                        log.warning("cleared a captcha token left on the PikPak client")
                        api.captcha_token = None
                    if not eager:
                        try:
                            token = await self._mint_captcha(api, action)
                        except PikpakException as again:
                            raise CaptchaError(f"{label}: {again}") from again
                    continue
                if _looks_like(message, _RATE_LIMIT_HINTS):
                    if attempt < self._max_retries:
                        delay = self._backoff * 2**attempt
                        log.info("PikPak rate-limited %s; waiting %.0fs", label, delay)
                        await self._sleep(delay)
                        continue
                    raise RateLimitedError(f"{label}: {message}") from exc
                if _looks_like(message, _AUTH_HINTS):
                    raise AuthError(f"{label}: {message}") from exc
                raise WmsError(f"{label}: {message}") from exc
        raise RateLimitedError(label)  # pragma: no cover - loop always returns or raises

    async def _mint_captcha(self, api: Any, action: str) -> str:
        await self._limiter.acquire()
        self.calls += 1
        result = await api.captcha_init(action=action)
        token = (result or {}).get("captcha_token")
        if not token:
            raise CaptchaError("PikPak gave no captcha token")
        return str(token)

    @staticmethod
    async def _scoped_get(api: Any, url: str, token: str, params: dict | None = None) -> Any:
        """GET with ``token`` in this request's own headers, nothing on ``api``."""
        headers = dict(api.get_headers())
        headers["X-Captcha-Token"] = token
        headers["User-Agent"] = api.build_custom_user_agent()
        return await api._make_request(  # noqa: SLF001 - the SDK has no public scoped GET
            "get", url, params=params, headers=headers)

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

    async def shares(self, *, page_size: int = 100) -> list[dict]:
        """Every share the account ever made, expired ones included.

        pikpakapi has no call for this, so it goes through the SDK's own
        authenticated GET (docs/wms/M7 §1).
        """
        api = await self._provider()
        url = f"https://{getattr(api, 'PIKPAK_API_HOST', 'api-drive.mypikpak.com')}"
        url += "/drive/v1/share/list"
        found: list[dict] = []
        token: str | None = None
        for _page in range(1000):  # a hard stop, should the token never run out
            params: dict[str, Any] = {"limit": page_size, "thumbnail_size": "SIZE_SMALL"}
            if token:
                params["page_token"] = token
            page = await self._call("_request_get", url, params)
            page = page if isinstance(page, dict) else {}
            items = page.get("data") or page.get("shares") or page.get("list") or []
            found.extend(item for item in items if isinstance(item, dict))
            token = page.get("next_page_token") or None
            if not token:
                break
        return found

    async def events(self, *, page_size: int = 100, token: str | None = None) -> dict:
        action = "GET:/drive/v1/events"

        async def scoped(api: Any, captcha: str | None) -> Any:
            url = f"https://{getattr(api, 'PIKPAK_API_HOST', 'api-drive.mypikpak.com')}{action[4:]}"
            params = {"thumbnail_size": "SIZE_MEDIUM", "limit": page_size,
                      "next_page_token": token}
            return await self._scoped_get(api, url, str(captcha), params)

        return await self._call("events", size=page_size, next_page_token=token,
                                action=action, scoped=scoped)

    async def file_info(self, file_id: str) -> dict | None:
        """One file or folder as PikPak has it now, or None when it is gone.

        A trashed file still answers, with ``trashed`` set; callers decide.
        """
        try:
            info = await self._call("offline_file_info", file_id)
        except AuthError:
            raise
        except RateLimitedError:
            raise
        except WmsError as exc:
            if _looks_like(str(exc), _NOT_FOUND_HINTS):
                return None
            raise
        return info if isinstance(info, dict) and info.get("id") else None

    async def download_url(self, file_id: str) -> str:
        return (await self.download_links(file_id))[0]

    async def download_links(self, file_id: str) -> tuple[str, str | None]:
        """``(web link, origin media link)`` from one file-details request.

        The request is the SDK's ``get_download_url`` done here: a captcha for
        this file's action, sent in this request's headers only. The SDK parks
        it on the shared api object, where concurrent calls pick it up.

        The web link is ``web_content_link`` (the first media link when there is
        none); the origin link is the ``medias`` entry with ``is_origin`` set, or
        None. Only links the API returned are ever used (docs/wms/M9.1 §C.1).
        """
        action = f"GET:/drive/v1/files/{file_id}"

        async def scoped(api: Any, _token: str | None) -> Any:
            token = await self._mint_captcha(api, action)
            host = getattr(api, "PIKPAK_API_HOST", "api-drive.mypikpak.com")
            return await self._scoped_get(api, f"https://{host}/drive/v1/files/{file_id}?", token)

        info = await self._run("get_download_url", None, action=action, scoped=scoped,
                               eager=True)
        origin = None
        first = None
        for media in info.get("medias") or []:
            url = ((media or {}).get("link") or {}).get("url")
            if not url:
                continue
            first = first or str(url)
            if (media or {}).get("is_origin") and origin is None:
                origin = str(url)
        web = info.get("web_content_link") or first
        if not web:
            raise WmsError(f"PikPak gave no download link for {file_id}")
        web = str(web)
        return web, (origin if origin and origin != web else None)

    # -------------------------------------------------------------- writing

    async def create_folder(self, name: str, parent_id: str, *, parent_path: str) -> FileNode:
        result = await self._call("create_folder", name=name, parent_id=parent_id or None)
        raw = result.get("file") or result
        node = FileNode.from_api(raw, parent_path=parent_path)
        node.parent_id = parent_id
        return node


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
