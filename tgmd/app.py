"""Application wiring and lifecycle."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from .clients import start_clients
from .config import Config, ConfigError, load_config
from .db import Database
from .delivery import Delivery
from .downloader import Downloader
from .handlers import BotHandlers
from .pikpak import PikPakService
from .resolver import Resolver
from .tasks import JobQueue
from .webserver import FileServer

log = logging.getLogger(__name__)


def setup_logging(level: str) -> None:
    """Configure root logging, keeping Telethon's own chatter down."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


class Application:
    """Owns every long-lived component and shuts them down in order."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config.download.db_path)
        self.file_server: FileServer | None = None
        self.queue: JobQueue | None = None
        self.bot = None
        self.user = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        config = self.config
        config.ensure_directories()

        await self.db.connect()
        secret = await self.db.get_or_create_secret()

        self.file_server = FileServer(config.http, secret)
        await self.file_server.start()

        self.bot, self.user = await start_clients(config)
        # The user client reads history; without one the bot can only read
        # chats it belongs to itself, which still covers some setups.
        reading_client = self.user or self.bot

        pikpak = PikPakService(config.pikpak, self.db)
        resolver = Resolver(
            reading_client, auto_join=config.download.auto_join_invites
        )
        delivery = Delivery(self.bot, config, self.db, pikpak, self.file_server)

        self.queue = JobQueue(
            config=config,
            db=self.db,
            bot=self.bot,
            resolver=resolver,
            downloader=Downloader(reading_client),
            bot_downloader=Downloader(self.bot),
            delivery=delivery,
            pikpak=pikpak,
        )
        await self.queue.start()

        BotHandlers(
            self.bot,
            config,
            self.db,
            self.queue,
            pikpak,
            has_user_client=self.user is not None,
        ).register()

        log.info(
            "ready — mode %s, %d worker(s), cache chat %s, PikPak %s",
            config.delivery.default_mode,
            config.download.concurrent,
            config.delivery.cache_chat_id or "disabled",
            "on" if config.pikpak.configured else "off",
        )

    async def run(self) -> None:
        """Serve until the process is asked to stop."""
        assert self.bot is not None
        disconnected = asyncio.create_task(self.bot.run_until_disconnected())
        stopping = asyncio.create_task(self._stopping.wait())
        done, pending = await asyncio.wait(
            {disconnected, stopping}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            # Surface a client-side failure rather than exiting silently.
            if task is disconnected and not task.cancelled():
                task.result()

    def request_stop(self) -> None:
        self._stopping.set()

    async def stop(self) -> None:
        log.info("shutting down")
        if self.queue is not None:
            await self.queue.stop()
        if self.file_server is not None:
            await self.file_server.stop()
        for client in (self.user, self.bot):
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:  # pragma: no cover - best effort
                    log.debug("client disconnect failed", exc_info=True)
        await self.db.close()


async def run_app(config_path: Path | None = None) -> None:
    """Load configuration, start everything, and run until stopped."""
    load_dotenv()
    config = load_config(config_path)
    setup_logging(config.log_level)

    for warning in config.validate():
        log.warning(warning)

    app = Application(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.request_stop)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    await app.start()
    try:
        await app.run()
    finally:
        await app.stop()


def main(argv: list[str] | None = None) -> int:
    """Console entry point."""
    argv = sys.argv[1:] if argv is None else argv
    config_path = Path(argv[0]) if argv else None

    try:
        asyncio.run(run_app(config_path))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0
