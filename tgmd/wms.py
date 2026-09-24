"""The PikPak warehouse (pikpak_wms) wired into the bot.

WMS never logs in on its own here: it borrows the PikPak account the bot
already has, so nobody types a password twice. Which account:

1. ``WMS_ACCOUNT``: a Telegram user id, or ``shared`` for the shared account;
2. else the first admin who connected their own account (Mini App or chat);
3. else the shared account from ``PIKPAK_USERNAME`` / ``PIKPAK_PASSWORD``.

Two ways in, both through :mod:`pikpak_wms.ops.embed` only:

* ``WMS_ENABLED=true`` runs WMS's scheduled jobs inside the bot;
* ``python -m tgmd.wms <command>`` (``wms`` in the image) runs one WMS
  command with the same account, e.g. ``docker compose run --rm bot wms plans``.
"""

from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv
from pikpakapi import PikPakApi

from pikpak_wms.ops.embed import AccountUnavailable, EmbeddedWms, run_command, set_language

from . import bootstrap, i18n
from .config import Config, load_config
from .db import Database
from .pikpak import PikPakError, PikPakService

log = logging.getLogger(__name__)


async def account_for(config: Config, pikpak: PikPakService) -> int | None:
    """The user whose drive WMS manages; None means the shared account."""
    if config.wms.shared_account:
        return None
    if config.wms.account is not None:
        return config.wms.account
    for admin in config.access.admin_user_ids:
        if await pikpak.has_user_session(admin):
            return admin
    return None


def provider_for(config: Config, pikpak: PikPakService):
    """An async callable giving WMS a logged-in client, decided on every call
    (an admin may connect their account while the bot runs)."""

    async def provider() -> PikPakApi:
        user_id = await account_for(config, pikpak)
        try:
            return await pikpak.client(user_id)
        except PikPakError as exc:
            raise AccountUnavailable(str(exc)) from exc

    return provider


class WmsInBot:
    """Starts and stops the embedded scheduler with the bot."""

    def __init__(self, config: Config, pikpak: PikPakService) -> None:
        self.config = config
        self.pikpak = pikpak
        self.embedded: EmbeddedWms | None = None

    async def start(self) -> None:
        if not self.config.wms.enabled:
            return
        # WMS reads the environment for its language; the bot's may come
        # from config.yaml instead, and both must speak the same one.
        set_language(i18n.language())
        try:
            self.embedded = EmbeddedWms(provider_for(self.config, self.pikpak))
            await self.embedded.start()
        except Exception:
            # A broken WMS config must not keep the downloader from starting.
            log.exception("WMS could not start; the bot carries on without it")
            self.embedded = None

    async def stop(self) -> None:
        if self.embedded is not None:
            await self.embedded.stop()
            self.embedded = None


# ------------------------------------------------------------ command line


def _factory(config: Config):
    """A fresh bot database and PikPak service per event loop the CLI opens."""

    def make():
        state: dict[str, PikPakService] = {}

        async def provider() -> PikPakApi:
            if "pikpak" not in state:
                db = Database(config.download.db_path)
                await db.connect()
                # Admins claimed in chat live in the database, not in .env.
                await bootstrap.load_runtime_settings(db, config)
                state["pikpak"] = PikPakService(config.pikpak, db)
            return await provider_for(config, state["pikpak"])()

        return provider

    return make


def main(argv: list[str] | None = None) -> int:
    """``wms`` inside the image: the WMS command line on the bot's account."""
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    config = load_config(None)
    return run_command(sys.argv[1:] if argv is None else argv, _factory(config))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
