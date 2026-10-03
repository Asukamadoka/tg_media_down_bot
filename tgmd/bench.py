"""Measure download speed for one Telegram message: ``python -m tgmd.bench``.

Downloads the same file once per connection count and prints what each run
achieved, so the effect of ``DOWNLOAD_CONNECTIONS`` can be measured on the
real network rather than guessed::

    python -m tgmd.bench https://t.me/c/1234567890/42
    python -m tgmd.bench https://t.me/somechannel/42 --connections 1,4,8
    python -m tgmd.bench https://t.me/c/1234567890/42 --route both

``--route`` is the experiment switch for direct media endpoints
(``TG_DIRECT_MEDIA``): ``normal`` uses the endpoint Telethon would, which on
the NAS goes through the proxy; ``media`` uses only the DC's media-only
endpoint; ``both`` runs each connection count once each way, on the same
file, one after the other. ``v2`` is ``TG_DIRECT_MEDIA=v2``: direct
connections to a non-home DC's media endpoint on a key of their own
(tgmd.direct); a file in the home DC, or a refusal, goes the ordinary way,
and the ``via`` column says which way each run went.

``v2`` needs no ``--same-egress-ip``: its key is used on the direct
connections only, so no key is ever seen from two addresses. It shares the
keys the bot stores in its database.

``media`` and ``both`` are refused unless ``--same-egress-ip`` is given
(docs/wms/M7 §7.1): the media connections reuse the session's auth key, and
when they leave from a different IP address than the main connection (direct
vs. proxy), Telegram treats the key as stolen and revokes the reading
account's session (AuthKeyDuplicatedError). Pass it only when every
connection of this host leaves through the same public IP address.

It reads through the same account the bot does (``TG_USER_SESSION``, the
session saved by ``/setup telegram``, or the session file), and runs happily
next to the bot. The downloaded copies are deleted unless ``--keep`` is
given. Every run is a real download from the user's account, so do not loop
this: a handful of runs is a measurement, hundreds are a flood.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

from .clients import user_session_source
from .config import ConfigError, load_config, parse_direct_endpoints
from .db import Database
from .direct import DirectRouteV2
from .downloader import Downloader, has_downloadable_media
from .links import LinkError, parse_message_link
from .parallel import default_endpoints, media_endpoints
from .resolver import ResolveError, Resolver
from .setup import stored_user_session
from .utils import human_rate, human_size

# Runs of the same file one after another; more is not more accurate enough
# to justify the load on the account.
MAX_RUNS = 6


def parse_counts(text: str) -> list[int]:
    counts = [int(part) for part in text.split(",") if part.strip()]
    if not counts or any(count < 1 for count in counts):
        raise argparse.ArgumentTypeError("connection counts are positive integers, like 1,4")
    if len(counts) > MAX_RUNS:
        raise argparse.ArgumentTypeError(f"at most {MAX_RUNS} runs at a time")
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tgmd.bench", description="Measure download speed for one message."
    )
    parser.add_argument("link", help="a Telegram message link to a file")
    parser.add_argument(
        "--connections",
        type=parse_counts,
        default=[1, 4],
        help="comma-separated connection counts to compare (default: 1,4)",
    )
    parser.add_argument(
        "--route",
        choices=("config", "normal", "media", "both", "v2"),
        default="config",
        help="which endpoints to download from (default: whatever TG_DIRECT_MEDIA says)",
    )
    parser.add_argument(
        "--same-egress-ip",
        action="store_true",
        help=(
            "allow --route media/both. DANGER: only when every connection leaves "
            "through the same public IP; otherwise Telegram revokes the reading "
            "session (AuthKeyDuplicatedError)"
        ),
    )
    parser.add_argument("--keep", action="store_true", help="keep the downloaded copies")
    return parser


REFUSED_ROUTE = (
    "--route {route} is refused: the media route reuses the session's auth key, and "
    "from a second IP address Telegram revokes the reading session "
    "(AuthKeyDuplicatedError). Add --same-egress-ip only if every connection of this "
    "host leaves through the same public IP address."
)


async def _reader(config, work: Path) -> TelegramClient | None:
    db = Database(config.download.db_path)
    await db.connect()
    try:
        stored = await stored_user_session(db)
    finally:
        await db.close()
    chosen = user_session_source(config, stored)
    if chosen is None:
        return None
    session, _source = chosen
    if isinstance(session, str):
        # A session file is an SQLite database the running bot holds open.
        # Work on a copy rather than contend for its lock.
        copy = work / "bench.session"
        shutil.copy(f"{session}.session", copy)
        session = str(copy.with_suffix(""))
    return TelegramClient(
        session, config.telegram.api_id, config.telegram.api_hash, flood_sleep_threshold=60
    )


async def run(args: argparse.Namespace) -> int:
    if args.route in ("media", "both") and not args.same_egress_ip:
        print(REFUSED_ROUTE.format(route=args.route), file=sys.stderr)
        return 2
    load_dotenv()
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        ref = parse_message_link(args.link)
    except LinkError as exc:
        print(f"not a usable link: {exc}", file=sys.stderr)
        return 2
    if ref is None:
        print("that is not a Telegram message link", file=sys.stderr)
        return 2

    config.download.dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".bench-", dir=config.download.dir))
    client = await _reader(config, work)
    if client is None:
        print("no reading account; sign one in with /setup telegram first", file=sys.stderr)
        return 2

    # v2 keeps its keys (and rests) in the bot's database, so a measurement
    # neither negotiates a key the bot already has nor leaves one behind.
    db = Database(config.download.db_path)
    await db.connect()
    direct = DirectRouteV2(
        db, manual=parse_direct_endpoints(config.telegram.direct_endpoints)[0]
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print("the reading account's session is not authorised", file=sys.stderr)
            return 2
        try:
            _entity, messages = await Resolver(client).resolve(ref)
        except ResolveError as exc:
            print(f"could not read that message: {exc}", file=sys.stderr)
            return 1
        message = next((m for m in messages if has_downloadable_media(m)), None)
        if message is None:
            print("that message has no file", file=sys.stderr)
            return 1

        routes = ["normal", "media"] if args.route == "both" else [args.route]
        print(
            f"{'route':>6}  {'connections':>11}  {'size':>10}  {'seconds':>8}  "
            f"{'rate':>12}  dc  {'via':>9}  endpoint"
        )
        for count in args.connections:
            for route in routes:
                target = work / f"run-{count}-{route}.bin"
                downloader = _downloader(client, count, route, config, direct)
                await downloader.download(message, target)
                transfer = downloader.last
                print(
                    f"{route:>6}  {transfer.connections:>11}  {human_size(transfer.size):>10}  "
                    f"{transfer.seconds:>8.1f}  {human_rate(transfer.rate):>12}  "
                    f"{transfer.dc_id if transfer.dc_id is not None else '?':>2}  "
                    f"{transfer.route:>9}  {transfer.endpoint or 'Telethon default'}"
                )
                if not args.keep:
                    target.unlink(missing_ok=True)
        return 0
    finally:
        await client.disconnect()
        await db.close()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


def _downloader(client, count: int, route: str, config, direct=None) -> Downloader:
    """A downloader forced onto one route, so the rows compare like with like."""
    if route == "v2" or (
        route == "config" and config is not None and config.telegram.direct_media == "v2"
    ):
        return Downloader(client, connections=count, direct=direct)
    if route == "normal":
        return Downloader(client, connections=count, endpoints=default_endpoints)
    if route == "media":
        # Media endpoints only: if the direct route fails, the row says so
        # (it falls back to "Telethon default") instead of quietly proxying.
        return Downloader(client, connections=count, endpoints=media_endpoints)
    # TG_DIRECT_MEDIA=auto is refused here as in the bot (docs/wms/M7 §7.1).
    return Downloader(client, connections=count)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
