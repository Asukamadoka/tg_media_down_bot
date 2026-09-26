"""Logging in when the command line runs on its own.

Inside the bot this module is not used: the bot hands WMS the client of the
PikPak account the user already connected. On its own, the command line logs
in from ``PIKPAK_ENCODED_TOKEN`` or ``PIKPAK_USERNAME`` / ``PIKPAK_PASSWORD``
once, then keeps only the token in a 0600 file, refreshed automatically, so
later runs (and container restarts) need no password.

The password never reaches the file: the SDK's serialised client carries the
username and password in clear text, and they are stripped before writing.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from pikpakapi import PikPakApi
from pikpakapi.PikpakException import PikpakException

from ..config import Credentials
from .errors import AuthError

log = logging.getLogger(__name__)

_SECRET_FIELDS = ("username", "password")


def strip_credentials(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key not in _SECRET_FIELDS}


def write_token(path: Path, client: Any) -> None:
    """Persist the client's token, and only its token, readable by us alone."""
    client.encode_token()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(strip_credentials(client.to_dict()))
    # Create it 0600 from the start, rather than chmod after a readable write.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.chmod(path, 0o600)


def read_token(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and (data.get("encoded_token") or data.get("access_token")):
        return data
    return None


class StandaloneAuth:
    """A provider for :class:`~pikpak_wms.core.client.WmsClient` on its own."""

    def __init__(self, token_path: Path, credentials: Credentials | None = None) -> None:
        self._token_path = token_path
        self._credentials = credentials or Credentials.from_environment()
        self._client: PikPakApi | None = None

    async def _persist(self, client: Any, **_kwargs: Any) -> None:
        try:
            write_token(self._token_path, client)
        except OSError:
            log.exception("could not save the PikPak token to %s", self._token_path)

    async def __call__(self) -> PikPakApi:
        if self._client is None:
            self._client = await self._restore() or await self.login()
        return self._client

    async def _restore(self) -> PikPakApi | None:
        saved = read_token(self._token_path)
        if saved is None:
            return None
        client = PikPakApi.from_dict(saved)
        client.token_refresh_callback = self._persist
        return client

    async def login(self) -> PikPakApi:
        """Log in from the environment and save the token. Raises AuthError."""
        credentials = self._credentials
        if not credentials.usable:
            raise AuthError(
                "no PikPak credentials: set PIKPAK_USERNAME and PIKPAK_PASSWORD, "
                "or PIKPAK_ENCODED_TOKEN"
            )
        client = PikPakApi(
            username=credentials.username,
            password=credentials.password,
            encoded_token=credentials.encoded_token,
            token_refresh_callback=self._persist,
        )
        if not credentials.encoded_token:
            try:
                await client.login()
            except PikpakException as exc:
                raise AuthError(f"PikPak login failed: {exc}") from exc
        await self._persist(client)
        client.password = None
        self._client = client
        return client
