"""Measure download speed for one Telegram message: ``python -m tgmd.bench``.

Downloads the same file once per connection count and prints what each run
achieved, so the effect of ``DOWNLOAD_CONNECTIONS`` can be measured on the
real network rather than guessed::

    python -m tgmd.bench https://t.me/c/1234567890/42
    python -m tgmd.bench https://t.me/somechannel/42 --connections 1,4,8

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
from .config import ConfigError, load_config
from .db import Database
from .downloader import Downloader, has_downloadable_media
from .links import LinkError, parse_message_link
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
    parser.add_argument("--keep", action="store_true", help="keep the downloaded copies")
    return parser


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

        print(f"{'connections':>11}  {'size':>10}  {'seconds':>8}  {'rate':>12}  dc  endpoint")
        for count in args.connections:
            target = work / f"run-{count}.bin"
            downloader = Downloader(client, connections=count)
            await downloader.download(message, target)
            transfer = downloader.last
            print(
                f"{transfer.connections:>11}  {human_size(transfer.size):>10}  "
                f"{transfer.seconds:>8.1f}  {human_rate(transfer.rate):>12}  "
                f"{transfer.dc_id if transfer.dc_id is not None else '?':>2}  "
                f"{transfer.endpoint or 'Telethon default'}"
            )
            if not args.keep:
                target.unlink(missing_ok=True)
        return 0
    finally:
        await client.disconnect()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
