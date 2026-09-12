"""HTTP file server used for Telegram to PikPak transfers.

PikPak has no upload API: the only way to get a file into it is to hand it a
URL it can fetch. So a Telegram-to-PikPak transfer works in two steps -- the
bot downloads the file with the user session, then serves it at a signed,
expiring URL and asks PikPak to pull it from there.

The server is optional. Without it, PikPak still accepts magnet links, direct
URLs and share links, which need no local download at all.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from .config import HttpConfig
from .signing import TokenError, make_token, verify_token

log = logging.getLogger(__name__)

# Files are unregistered once PikPak has taken them, but a crash mid-transfer
# should not leak entries forever.
_SWEEP_INTERVAL = 300.0


@dataclass
class ServedFile:
    path: Path
    name: str
    expires_at: float


class FileServer:
    """Serves registered local files at unguessable, expiring URLs."""

    def __init__(self, config: HttpConfig, secret: str) -> None:
        self._config = config
        self._secret = secret
        self._files: dict[str, ServedFile] = {}
        self._runner: web.AppRunner | None = None
        self._sweeper: asyncio.Task | None = None

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Bind the HTTP listener. Safe to call when the server is disabled."""
        if not self._config.enabled:
            log.info("HTTP file server disabled")
            return

        app = web.Application()
        app.router.add_get("/healthz", self._handle_health)
        # add_get also registers HEAD, which PikPak uses to size a file first.
        app.router.add_get("/f/{token}/{name}", self._handle_file)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._config.host, self._config.port)
        await site.start()
        self._sweeper = asyncio.create_task(self._sweep_expired(), name="file-sweeper")
        log.info(
            "HTTP file server listening on %s:%s (public base %s)",
            self._config.host,
            self._config.port,
            self._config.base_url or "<unset>",
        )

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except asyncio.CancelledError:
                pass
            self._sweeper = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @property
    def usable(self) -> bool:
        """True when a URL published by this server is reachable by PikPak."""
        return self._config.usable and self._runner is not None

    @property
    def base_url(self) -> str:
        """The public address this server publishes URLs under."""
        return self._config.base_url

    # -------------------------------------------------------------- registry

    def publish(self, path: Path, *, name: str | None = None, ttl: int | None = None) -> str:
        """Register ``path`` and return the public URL PikPak should fetch."""
        if not self.usable:
            raise RuntimeError(
                "the HTTP file server is not running or has no public base URL"
            )
        file_id = secrets.token_urlsafe(12)
        lifetime = ttl or self._config.url_ttl
        expires_at = time.time() + lifetime
        display_name = name or path.name
        self._files[file_id] = ServedFile(
            path=path, name=display_name, expires_at=expires_at
        )
        token = make_token(self._secret, file_id, int(expires_at))
        return f"{self._config.base_url}/f/{token}/{quote(display_name)}"

    def unpublish_all(self, path: Path) -> None:
        """Drop every registration pointing at ``path``."""
        for file_id in [fid for fid, served in self._files.items() if served.path == path]:
            self._files.pop(file_id, None)

    # -------------------------------------------------------------- handlers

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "served": len(self._files)})

    async def _handle_file(self, request: web.Request) -> web.StreamResponse:
        token = request.match_info["token"]
        try:
            file_id = verify_token(self._secret, token)
        except TokenError as exc:
            # Never say why: a probe learns nothing from a 404.
            log.info("rejected file request: %s", exc)
            raise web.HTTPNotFound(text="not found") from None

        served = self._files.get(file_id)
        if served is None or served.expires_at <= time.time():
            self._files.pop(file_id, None)
            raise web.HTTPNotFound(text="not found")

        if not served.path.is_file():
            log.warning("served file vanished before it was fetched: %s", served.path)
            self._files.pop(file_id, None)
            raise web.HTTPNotFound(text="not found")

        # FileResponse handles Range requests and conditional headers, which
        # PikPak's fetcher relies on for resuming.
        return web.FileResponse(
            served.path,
            headers={
                "Content-Disposition": f'attachment; filename="{served.name}"',
                "Cache-Control": "no-store",
            },
        )

    # --------------------------------------------------------------- sweeper

    async def _sweep_expired(self) -> None:
        while True:
            try:
                await asyncio.sleep(_SWEEP_INTERVAL)
                now = time.time()
                stale = [fid for fid, s in self._files.items() if s.expires_at <= now]
                for file_id in stale:
                    self._files.pop(file_id, None)
                if stale:
                    log.debug("dropped %d expired file registration(s)", len(stale))
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                log.exception("file sweeper failed")
