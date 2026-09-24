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
from .forwarder import resolve_peer
from .i18n import describe, language_from_environment, set_language, t
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
    report.add(Check.ok("configuration", t("verify.config.ok")))
    for warning in warnings:
        report.add(Check.warn("configuration note", warning))


def check_access_control(report: Report, config: Config) -> None:
    """Somebody has to be allowed to use the bot, or it is inert."""
    access = config.access
    if access.allow_all_users:
        report.add(
            Check.warn(
                "access control",
                t("verify.access.open"),
            )
        )
        return
    if not access.admin_user_ids and not access.allowed_user_ids:
        report.add(
            Check.fail(
                "access control",
                t("verify.access.none"),
            )
        )
        return
    report.add(
        Check.ok(
            "access control",
            t("verify.access.ok", admins=len(access.admin_user_ids),
              users=len(access.allowed_user_ids)),
        )
    )


def check_token(report: Report, config: Config) -> BotToken | None:
    """Parse the bot token offline and report the bot id it names."""
    try:
        token = parse_bot_token(config.telegram.bot_token)
    except BotTokenError as exc:
        report.add(Check.fail("bot token", describe(exc)))
        return None
    report.add(
        Check.ok("bot token", t("verify.token.ok", id=token.bot_id))
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
        report.add(Check.skip("bot identity", t("verify.bot.no_token")))
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
            Check.fail("bot identity", t("verify.timeout"))
        )
        return None, None
    except Exception as exc:
        report.add(
            Check.fail(
                "bot identity",
                t("verify.bot.sign_in_failed", error=exc),
            )
        )
        return None, None

    try:
        me = await client.get_me()
    except Exception as exc:
        report.add(Check.fail("bot identity", t("verify.bot.read_failed", error=exc)))
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
            t("verify.bot.created", account=describe_account(me), link=link)
            if link
            else t("verify.bot.no_username", account=describe_account(me)),
        )
    )
    if not getattr(me, "bot", False):
        report.add(
            Check.fail(
                "bot identity",
                t("verify.bot.not_bot"),
            )
        )
    elif token is not None and me.id != token.bot_id:
        report.add(
            Check.fail(
                "bot identity",
                t("verify.bot.mismatch", expected=token.bot_id, actual=me.id),
            )
        )
    elif token is not None:
        report.add(
            Check.ok("bot identity", t("verify.bot.ok", id=me.id))
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
                t("verify.user.missing"),
            )
        )
        return None, None
    session, source = chosen
    if source == "an in-chat login":  # the label clients.py logs, in English
        source = t("verify.source.in_chat")

    telegram = config.telegram
    client = TelegramClient(session, telegram.api_id, telegram.api_hash)
    try:
        await asyncio.wait_for(client.connect(), timeout=_NETWORK_TIMEOUT)
    except TimeoutError:
        report.add(Check.fail("user session", t("verify.timeout")))
        return None, None
    except Exception as exc:
        report.add(Check.fail("user session", t("verify.user.connect_failed", error=exc)))
        return None, None

    try:
        if not await client.is_user_authorized():
            report.add(
                Check.fail(
                    "user session",
                    t("verify.user.not_authorised", source=source),
                )
            )
            await client.disconnect()
            return None, None
        me = await client.get_me()
    except Exception as exc:
        report.add(Check.fail("user session", t("verify.user.read_failed", error=exc)))
        await client.disconnect()
        return None, None

    if getattr(me, "bot", False):
        report.add(
            Check.fail(
                "user session",
                t("verify.user.is_bot"),
            )
        )
        return client, me

    premium = t("verify.user.premium") if getattr(me, "premium", False) else ""
    report.add(
        Check.ok(
            "user session",
            t("verify.user.ok", account=describe_account(me), source=source, premium=premium),
        )
    )
    return client, me


def check_distinct_accounts(report: Report, bot_me, user_me) -> None:
    """The bot and the reading account must not be the same account."""
    if bot_me is None or user_me is None:
        report.add(Check.skip("account separation", t("verify.accounts.need_both")))
        return
    if getattr(bot_me, "id", None) == getattr(user_me, "id", None):
        report.add(
            Check.fail(
                "account separation",
                t("verify.accounts.same"),
            )
        )
        return
    report.add(
        Check.ok(
            "account separation",
            t("verify.accounts.ok", bot=bot_me.id, user=user_me.id),
        )
    )


async def check_read_access(report: Report, user: TelegramClient) -> None:
    """Prove the reading account can actually list chats."""
    try:
        dialogs = await asyncio.wait_for(
            user.get_dialogs(limit=1), timeout=_NETWORK_TIMEOUT
        )
    except Exception as exc:
        report.add(Check.fail("history access", t("verify.history.failed", error=exc)))
        return
    if not dialogs:
        report.add(
            Check.warn(
                "history access",
                t("verify.history.empty"),
            )
        )
        return
    report.add(Check.ok("history access", t("verify.history.ok")))


async def check_cache_chat(report: Report, config: Config, bot: TelegramClient) -> None:
    """If an upload cache channel is configured, the bot must be able to post."""
    cache_chat_id = config.delivery.cache_chat_id
    if cache_chat_id is None:
        report.add(
            Check.skip(
                "cache chat",
                t("verify.cache.unset"),
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
                t("verify.cache.cannot_see", chat=cache_chat_id, error=exc),
            )
        )
        return

    if not getattr(permissions, "is_admin", False):
        report.add(
            Check.warn(
                "cache chat",
                t("verify.cache.not_admin", chat=cache_chat_id),
            )
        )
        return
    report.add(Check.ok("cache chat", t("verify.cache.ok", chat=cache_chat_id)))


async def check_forward_path(report: Report, config: Config, user) -> None:
    """Can forwardable files skip the download? See tgmd.forwarder."""
    name = "forward fast path"
    if user is None:
        report.add(
            Check.ok(name, t("verify.forward.bot_reads"))
        )
        return
    cache_chat_id = config.delivery.cache_chat_id
    if cache_chat_id is None:
        report.add(
            Check.warn(
                name,
                t("verify.forward.no_cache"),
            )
        )
        return
    try:
        peer = await asyncio.wait_for(resolve_peer(user, cache_chat_id), _NETWORK_TIMEOUT)
        channel = await user.get_entity(peer)
        permissions = await user.get_permissions(peer, "me")
    except Exception as exc:
        report.add(
            Check.warn(
                name,
                t("verify.forward.cannot_see", chat=cache_chat_id, error=exc),
            )
        )
        return
    # Only admins with the right may post in a broadcast channel; in a group
    # anyone not banned may.
    if getattr(channel, "broadcast", False):
        can_post = permissions.is_creator or permissions.post_messages
    else:
        can_post = not (permissions.is_banned or permissions.has_left)
    if can_post:
        report.add(Check.ok(name, t("verify.forward.ok", chat=cache_chat_id)))
    else:
        report.add(
            Check.warn(
                name,
                t("verify.forward.cannot_post", chat=cache_chat_id),
            )
        )


# ---------------------------------------------------------------------- PikPak


async def check_pikpak(report: Report, config: Config, db: Database) -> None:
    """Check the shared account, then whether users can connect their own."""
    service = PikPakService(config.pikpak, db)

    if config.pikpak.configured:
        try:
            quota = await service.quota()
        except PikPakError as exc:
            report.add(Check.fail("pikpak account", describe(exc)))
        else:
            report.add(
                Check.ok(
                    "pikpak account",
                    t("verify.pikpak.ok", username=config.pikpak.username,
                      used=human_size(quota.used), limit=human_size(quota.limit)),
                )
            )
            report.add(
                Check.ok("pikpak folder", t("verify.pikpak.folder", folder=config.pikpak.folder))
            )
    else:
        report.add(
            Check.skip(
                "pikpak account",
                t("verify.pikpak.no_shared"),
            )
        )

    if not config.pikpak.allow_user_login:
        report.add(Check.skip("pikpak login", t("verify.login.disabled")))
    elif not (config.http.usable and config.http.base_url.startswith("https://")):
        report.add(
            Check.warn(
                "pikpak login",
                t("verify.login.needs_https"),
            )
        )
    else:
        report.add(
            Check.ok(
                "pikpak login",
                t("verify.login.ok", url=config.http.base_url),
            )
        )


# ------------------------------------------------------------------------ HTTP


async def check_http(report: Report, config: Config, db: Database) -> None:
    """Bind the file server and confirm its public URL answers from outside."""
    if not config.http.enabled:
        report.add(
            Check.skip(
                "http server",
                t("verify.http.disabled"),
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
                t("verify.http.bind_failed", host=config.http.host,
                  port=config.http.port, error=exc),
            )
        )
        return

    report.add(
        Check.ok("http server", t("verify.http.bound", host=config.http.host,
                                  port=config.http.port))
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
                Check.ok("public reachability", t("verify.reach.ok", url=url))
            )
        else:
            report.add(
                Check.warn(
                    "public reachability",
                    t("verify.reach.status", url=url, status=status),
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
                t("verify.reach.failed", url=url, error=detail),
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
        report.add(Check.ok("database", t("verify.db.ok", path=config.download.db_path)))
    except Exception as exc:
        report.add(Check.fail("database", t("verify.db.failed", path=config.download.db_path,
                                           error=exc)))
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
            await check_forward_path(report, config, user)
        elif bot is not None:
            await check_forward_path(report, config, None)
        if bot is not None:
            await check_cache_chat(report, config, bot)
        else:
            # Say so rather than dropping the line: a missing check reads as a
            # passing one.
            report.add(
                Check.skip("cache chat", t("verify.cache.no_bot"))
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
        report.add(Check.fail("bot identity", t("verify.bot.read_failed", error=exc)))
        bot_me = None

    if bot_me is not None:
        check_bot_identity(report, bot_me, token)

    user_me = None
    if user is None:
        report.add(
            Check.warn(
                "user session",
                t("verify.live.user_missing"),
            )
        )
    else:
        try:
            user_me = await user.get_me()
            report.add(
                Check.ok(
                    "user session", t("verify.live.user_ok", account=describe_account(user_me))
                )
            )
        except Exception as exc:
            report.add(Check.fail("user session", t("verify.user.read_failed", error=exc)))

    check_distinct_accounts(report, bot_me, user_me)
    if user is not None and user_me is not None:
        await check_read_access(report, user)
    await check_forward_path(report, config, user)
    await check_cache_chat(report, config, bot)

    # PikPak, from the point of view of the person who asked.
    if await pikpak.has_user_session(for_user_id):
        source = t("verify.live.source_own")
    elif pikpak.configured:
        source = t("verify.live.source_shared", username=config.pikpak.username)
    else:
        source = None

    if source is None:
        report.add(
            Check.warn(
                "pikpak account",
                t("verify.live.no_pikpak"),
            )
        )
    else:
        try:
            quota = await pikpak.quota(user_id=for_user_id)
            report.add(
                Check.ok(
                    "pikpak account",
                    t("verify.live.pikpak_ok", source=source,
                      used=human_size(quota.used), limit=human_size(quota.limit)),
                )
            )
        except PikPakError as exc:
            report.add(Check.fail("pikpak account", t("verify.live.pikpak_failed", source=source,
                                                   error=describe(exc))))

    reason = portal.unavailable_reason()
    if reason is None:
        report.add(Check.ok("pikpak login", t("verify.live.login_ok")))
    else:
        report.add(
            Check.warn("pikpak login", t("verify.live.login_none", reason=reason))
        )

    if config.http.enabled:
        report.add(
            Check.ok(
                "http server",
                t("verify.live.http_ok",
                  url=config.http.base_url or t("verify.live.no_url")),
            )
        )
    else:
        report.add(
            Check.skip(
                "http server",
                t("verify.live.http_disabled"),
            )
        )

    return report


async def _main(config_path: Path | None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.ERROR)

    # The environment says the language even when the config does not load.
    set_language(language_from_environment())
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(t("verify.cli.config_error", error=exc), file=sys.stderr)
        return 2
    set_language(config.language)

    print(t("verify.cli.start") + "\n")
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
