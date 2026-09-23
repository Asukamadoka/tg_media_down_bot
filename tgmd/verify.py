"""Preflight verification: prove the setup works before running the bot.

Run it with ``python -m tgmd.verify``. It answers the questions that otherwise
only surface as a silently broken bot:

* Does the bot token parse, and does the bot it names actually answer?
* Is the account that answered the same one the token claims? The digits
  before the colon in a BotFather token are the bot's own user id, so this is
  a real cross-check rather than a formality.
* Is a user session present and authorised, is it a human account rather than
  another bot, and can it actually read chat history?
* Are the bot and the reading account two different accounts?
* If a cache channel is configured, can the bot post to it?
* Does PikPak accept the shared credentials, and can users connect their own?
* Is the advertised public URL reachable from outside, which is what PikPak
  needs in order to fetch anything?

Exit status is 0 when nothing failed, 1 otherwise. Warnings do not fail the
run. Best run while the bot is stopped, so the two processes do not share a
session file.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from . import bootstrap
from .clients import user_session_source
from .config import Config, ConfigError, load_config
from .db import Database
from .identity import (
    BotToken,
    BotTokenError,
    Check,
    Report,
    account_link,
    describe_account,
    parse_bot_token,
)
from .pikpak import PikPakError, PikPakService
from .portal import PikPakLoginPortal
from .setup import stored_user_session
from .utils import human_size
from .webserver import FileServer

log = logging.getLogger(__name__)

_NETWORK_TIMEOUT = 20
_REACHABILITY_TIMEOUT = 10


# --------------------------------------------------------------------- offline


def check_configuration(report: Report, config: Config) -> None:
    """Validate the configuration itself, surfacing its warnings as checks."""
    try:
        warnings = config.validate()
    except ConfigError as exc:
        report.add(Check.fail("configuration", str(exc)))
        return
    report.add(Check.ok("configuration", "loaded and internally consistent"))
    for warning in warnings:
        report.add(Check.warn("configuration note", warning))


def check_access_control(report: Report, config: Config) -> None:
    """Somebody has to be allowed to use the bot, or it is inert."""
    access = config.access
    if access.allow_all_users:
        report.add(
            Check.warn(
                "access control",
                "open to every Telegram user; the bot can read anything your "
                "account can see",
            )
        )
        return
    if not access.admin_user_ids and not access.allowed_user_ids:
        report.add(
            Check.fail(
                "access control",
                "no admin or allowed user ids, so every request will be refused. "
                "Send /claim with the code from the bot's log, or set ADMIN_USER_IDS.",
            )
        )
        return
    report.add(
        Check.ok(
            "access control",
            f"{len(access.admin_user_ids)} admin(s), "
            f"{len(access.allowed_user_ids)} additional user(s)",
        )
    )


def check_token(report: Report, config: Config) -> BotToken | None:
    """Parse the bot token offline and report the bot id it names."""
    try:
        token = parse_bot_token(config.telegram.bot_token)
    except BotTokenError as exc:
        report.add(Check.fail("bot token", str(exc)))
        return None
    report.add(
        Check.ok("bot token", f"well-formed, names bot id {token.bot_id}")
    )
    return token


def check_directories(report: Report, config: Config) -> None:
    """Every directory the bot writes to must exist and be writable."""
    targets = {
        "downloads": config.download.dir,
        "data": config.download.data_dir,
        "sessions": config.telegram.session_dir,
    }
    problems: list[str] = []
    for label, directory in targets.items():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".tgmd-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            problems.append(f"{label} ({directory}): {exc}")
    if problems:
        report.add(Check.fail("directories", "; ".join(problems)))
    else:
        report.add(
            Check.ok(
                "directories",
                ", ".join(f"{label}={path}" for label, path in targets.items()),
            )
        )


# -------------------------------------------------------------------- Telegram


async def connect_bot(
    report: Report, config: Config, token: BotToken | None
) -> tuple[TelegramClient | None, object | None]:
    """Confirm the bot exists and that it is the bot the token names."""
    if token is None:
        report.add(Check.skip("bot identity", "no usable token to check"))
        return None, None

    # An in-memory session keeps this from touching the running bot's session
    # file, so verification is safe to run at any time.
    client = TelegramClient(
        StringSession(), config.telegram.api_id, config.telegram.api_hash
    )
    try:
        await asyncio.wait_for(
            client.start(bot_token=config.telegram.bot_token),
            timeout=_NETWORK_TIMEOUT,
        )
    except TimeoutError:
        report.add(
            Check.fail("bot identity", "timed out connecting to Telegram")
        )
        return None, None
    except Exception as exc:
        report.add(
            Check.fail(
                "bot identity",
                f"could not sign in as the bot: {exc}. Check the token with "
                "@BotFather, and check that this host can open a direct TCP "
                "connection to Telegram — MTProto is not plain HTTPS, so an "
                "HTTPS-only proxy will block it.",
            )
        )
        return None, None

    try:
        me = await client.get_me()
    except Exception as exc:
        report.add(Check.fail("bot identity", f"could not read the bot account: {exc}"))
        await client.disconnect()
        return None, None

    check_bot_identity(report, me, token)
    return client, me


def check_bot_identity(report: Report, me, token: BotToken | None) -> None:
    """The account that answered must be a bot, and the one the token names."""
    link = account_link(me)
    report.add(
        Check.ok(
            "bot created",
            describe_account(me) + (f" — {link}" if link else " — no username"),
        )
    )
    if not getattr(me, "bot", False):
        report.add(
            Check.fail(
                "bot identity",
                "that token belongs to an account Telegram does not mark as a bot",
            )
        )
    elif token is not None and me.id != token.bot_id:
        report.add(
            Check.fail(
                "bot identity",
                f"the token names bot id {token.bot_id} but the account that "
                f"answered is {me.id}",
            )
        )
    elif token is not None:
        report.add(
            Check.ok("bot identity", f"id {me.id} matches the token, and is a bot")
        )


async def connect_user(
    report: Report, config: Config, stored_session: str | None = None
) -> tuple[TelegramClient | None, object | None]:
    """Confirm the reading account is authorised and is a human account.

    The session is chosen exactly as the bot chooses it at startup, including
    one stored by an in-chat ``/setup telegram``.
    """
    chosen = user_session_source(config, stored_session)
    if chosen is None:
        report.add(
            Check.warn(
                "user session",
                "not configured. Without one, only chats the bot itself is in "
                "can be read. Sign one in from Telegram with /setup telegram.",
            )
        )
        return None, None
    session, source = chosen

    telegram = config.telegram
    client = TelegramClient(session, telegram.api_id, telegram.api_hash)
    try:
        await asyncio.wait_for(client.connect(), timeout=_NETWORK_TIMEOUT)
    except TimeoutError:
        report.add(Check.fail("user session", "timed out connecting to Telegram"))
        return None, None
    except Exception as exc:
        report.add(Check.fail("user session", f"could not connect: {exc}"))
        return None, None

    try:
        if not await client.is_user_authorized():
            report.add(
                Check.fail(
                    "user session",
                    f"the session from {source} is not authorised; sign in "
                    "again with /setup telegram",
                )
            )
            await client.disconnect()
            return None, None
        me = await client.get_me()
    except Exception as exc:
        report.add(Check.fail("user session", f"could not read the account: {exc}"))
        await client.disconnect()
        return None, None

    if getattr(me, "bot", False):
        report.add(
            Check.fail(
                "user session",
                "that session belongs to a bot; downloads need a real account",
            )
        )
        return client, me

    premium = " Telegram Premium" if getattr(me, "premium", False) else ""
    report.add(
        Check.ok(
            "user session",
            f"{describe_account(me)} from {source}, authorised{premium}",
        )
    )
    return client, me


def check_distinct_accounts(report: Report, bot_me, user_me) -> None:
    """The bot and the reading account must not be the same account."""
    if bot_me is None or user_me is None:
        report.add(Check.skip("account separation", "needs both accounts"))
        return
    if getattr(bot_me, "id", None) == getattr(user_me, "id", None):
        report.add(
            Check.fail(
                "account separation",
                "the bot and the reading account are the same account",
            )
        )
        return
    report.add(
        Check.ok(
            "account separation",
            f"bot {bot_me.id} reads through account {user_me.id}",
        )
    )


async def check_read_access(report: Report, user: TelegramClient) -> None:
    """Prove the reading account can actually list chats."""
    try:
        dialogs = await asyncio.wait_for(
            user.get_dialogs(limit=1), timeout=_NETWORK_TIMEOUT
        )
    except Exception as exc:
        report.add(Check.fail("history access", f"could not list dialogs: {exc}"))
        return
    if not dialogs:
        report.add(
            Check.warn(
                "history access",
                "the account has no chats, so there is nothing to download from",
            )
        )
        return
    report.add(Check.ok("history access", "the account can list its chats"))


async def check_cache_chat(report: Report, config: Config, bot: TelegramClient) -> None:
    """If an upload cache channel is configured, the bot must be able to post."""
    cache_chat_id = config.delivery.cache_chat_id
    if cache_chat_id is None:
        report.add(
            Check.skip(
                "cache chat",
                "not configured; every request re-downloads (set CACHE_CHAT_ID)",
            )
        )
        return
    try:
        permissions = await asyncio.wait_for(
            bot.get_permissions(cache_chat_id, "me"), timeout=_NETWORK_TIMEOUT
        )
    except Exception as exc:
        report.add(
            Check.fail(
                "cache chat",
                f"the bot cannot see chat {cache_chat_id}: {exc}. Add the bot to "
                "it as an administrator.",
            )
        )
        return

    if not getattr(permissions, "is_admin", False):
        report.add(
            Check.warn(
                "cache chat",
                f"the bot is in chat {cache_chat_id} but is not an administrator; "
                "it may not be able to post or read back uploads",
            )
        )
        return
    report.add(Check.ok("cache chat", f"the bot administrates chat {cache_chat_id}"))


# ---------------------------------------------------------------------- PikPak


async def check_pikpak(report: Report, config: Config, db: Database) -> None:
    """Check the shared account, then whether users can connect their own."""
    service = PikPakService(config.pikpak, db)

    if config.pikpak.configured:
        try:
            quota = await service.quota()
        except PikPakError as exc:
            report.add(Check.fail("pikpak account", str(exc)))
        else:
            report.add(
                Check.ok(
                    "pikpak account",
                    f"{config.pikpak.username} signed in, "
                    f"{human_size(quota.used)} of {human_size(quota.limit)} used",
                )
            )
            report.add(
                Check.ok("pikpak folder", f"transfers land in {config.pikpak.folder}")
            )
    else:
        report.add(
            Check.skip(
                "pikpak account",
                "no shared account configured; users connect their own instead",
            )
        )

    if not config.pikpak.allow_user_login:
        report.add(Check.skip("pikpak login", "disabled (pikpak.allow_user_login)"))
    elif not (config.http.usable and config.http.base_url.startswith("https://")):
        report.add(
            Check.warn(
                "pikpak login",
                "the Mini App needs HTTP_ENABLED=true and an HTTPS "
                "PUBLIC_BASE_URL; until then users connect with /setup pikpak",
            )
        )
    else:
        report.add(
            Check.ok(
                "pikpak login",
                f"/pikpak login opens {config.http.base_url}/pikpak/app in Telegram",
            )
        )


# ------------------------------------------------------------------------ HTTP


async def check_http(report: Report, config: Config, db: Database) -> None:
    """Bind the file server and confirm its public URL answers from outside."""
    if not config.http.enabled:
        report.add(
            Check.skip(
                "http server",
                "disabled; magnet and URL transfers still work, Telegram media "
                "cannot reach PikPak",
            )
        )
        return

    secret = await db.get_or_create_secret()
    pikpak = PikPakService(config.pikpak, db)
    portal = PikPakLoginPortal(
        pikpak,
        config.pikpak,
        config.http,
        bot_token=config.telegram.bot_token,
        is_allowed=config.access.is_allowed,
    )
    server = FileServer(config.http, secret, portal=portal)

    try:
        await server.start()
    except OSError as exc:
        report.add(
            Check.warn(
                "http server",
                f"could not bind {config.http.host}:{config.http.port}: {exc}. "
                "If the bot is already running, this port is expected to be busy.",
            )
        )
        return

    report.add(
        Check.ok("http server", f"bound {config.http.host}:{config.http.port}")
    )

    url = f"{config.http.base_url}/healthz"
    try:
        timeout = aiohttp.ClientTimeout(total=_REACHABILITY_TIMEOUT)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url) as response,
        ):
            status = response.status
            body = await response.text()
        if status == 200 and "ok" in body:
            report.add(
                Check.ok("public reachability", f"{url} answers, so PikPak can fetch files")
            )
        else:
            report.add(
                Check.warn(
                    "public reachability",
                    f"{url} answered HTTP {status}; check the reverse proxy",
                )
            )
    except Exception as exc:
        # A self-fetch can fail on a host whose NAT does not hairpin even
        # though PikPak reaches it fine, so this is not treated as fatal.
        # Several aiohttp errors stringify to nothing, hence the type fallback.
        detail = str(exc) or type(exc).__name__
        report.add(
            Check.warn(
                "public reachability",
                f"could not fetch {url} from this host ({detail}). Verify from "
                "outside; NAT hairpinning often breaks a self-test.",
            )
        )
    finally:
        await server.stop()


# ------------------------------------------------------------------- the runner


async def run_checks(config: Config) -> Report:
    """Run every check and return the report."""
    report = Report()

    check_configuration(report, config)
    token = check_token(report, config)
    check_directories(report, config)

    db = Database(config.download.db_path)
    try:
        await db.connect()
        report.add(Check.ok("database", f"opened {config.download.db_path}"))
    except Exception as exc:
        report.add(Check.fail("database", f"could not open {config.download.db_path}: {exc}"))
        return report

    # An admin claimed with /claim and a session from /setup telegram live in
    # the database, exactly as the running bot would find them.
    await bootstrap.load_runtime_settings(db, config)
    check_access_control(report, config)

    bot = user = None
    try:
        bot, bot_me = await connect_bot(report, config, token)
        user, user_me = await connect_user(report, config, await stored_user_session(db))
        check_distinct_accounts(report, bot_me, user_me)
        if user is not None and user_me is not None:
            await check_read_access(report, user)
        if bot is not None:
            await check_cache_chat(report, config, bot)
        else:
            # Say so rather than dropping the line: a missing check reads as a
            # passing one.
            report.add(
                Check.skip("cache chat", "cannot be checked without a working bot")
            )
    finally:
        for client in (bot, user):
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:  # pragma: no cover - best effort
                    log.debug("disconnect failed", exc_info=True)

    try:
        await check_pikpak(report, config, db)
        await check_http(report, config, db)
    finally:
        await db.close()

    return report


async def run_live_checks(
    config: Config,
    *,
    bot: TelegramClient,
    user: TelegramClient | None,
    pikpak: PikPakService,
    portal: PikPakLoginPortal,
    for_user_id: int,
) -> Report:
    """The same identity checks, against the clients that are already running.

    This backs the in-chat ``/verify`` command, so it must not reconnect
    anything or touch the running bot's session.
    """
    report = Report()
    token = check_token(report, config)

    try:
        bot_me = await bot.get_me()
    except Exception as exc:
        report.add(Check.fail("bot identity", f"could not read the bot account: {exc}"))
        bot_me = None

    if bot_me is not None:
        check_bot_identity(report, bot_me, token)

    user_me = None
    if user is None:
        report.add(
            Check.warn(
                "user session",
                "not configured; only chats the bot itself is in can be read",
            )
        )
    else:
        try:
            user_me = await user.get_me()
            report.add(
                Check.ok("user session", f"{describe_account(user_me)}, authorised")
            )
        except Exception as exc:
            report.add(Check.fail("user session", f"could not read the account: {exc}"))

    check_distinct_accounts(report, bot_me, user_me)
    if user is not None and user_me is not None:
        await check_read_access(report, user)
    await check_cache_chat(report, config, bot)

    # PikPak, from the point of view of the person who asked.
    if await pikpak.has_user_session(for_user_id):
        source = "your own account"
    elif pikpak.configured:
        source = f"the shared account ({config.pikpak.username})"
    else:
        source = None

    if source is None:
        report.add(
            Check.warn(
                "pikpak account",
                "no account connected for you and none configured on the server; "
                "use /pikpak login",
            )
        )
    else:
        try:
            quota = await pikpak.quota(user_id=for_user_id)
            report.add(
                Check.ok(
                    "pikpak account",
                    f"{source}, {human_size(quota.used)} of "
                    f"{human_size(quota.limit)} used",
                )
            )
        except PikPakError as exc:
            report.add(Check.fail("pikpak account", f"{source}: {exc}"))

    reason = portal.unavailable_reason()
    if reason is None:
        report.add(Check.ok("pikpak login", "/pikpak login opens the Mini App"))
    else:
        report.add(
            Check.warn("pikpak login", f"no Mini App ({reason}); /setup pikpak works")
        )

    if config.http.enabled:
        report.add(
            Check.ok(
                "http server",
                f"serving at {config.http.base_url or 'no public URL set'}",
            )
        )
    else:
        report.add(
            Check.skip(
                "http server",
                "disabled; Telegram media cannot be transferred to PikPak",
            )
        )

    return report


async def _main(config_path: Path | None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.ERROR)

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    print("Verifying tg_media_down_bot setup…\n")
    report = await run_checks(config)
    print(report.render_text())
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    """Console entry point for ``python -m tgmd.verify``."""
    argv = sys.argv[1:] if argv is None else argv
    config_path = Path(argv[0]) if argv else None
    try:
        return asyncio.run(_main(config_path))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
