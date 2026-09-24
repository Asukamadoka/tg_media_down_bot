"""Everything an operation needs, built once per process.

The command line and the bot each build one :class:`Context` and call ops
with it; neither reaches below ``ops`` (rule 6). What differs between them
is only the provider of the logged-in PikPak client.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..core.client import Provider, WmsClient
from ..core.ratelimit import TokenBucket
from ..store.db import Store


@dataclass
class Context:
    config: Config
    client: WmsClient
    store: Store

    async def close(self) -> None:
        await self.store.close()


async def open_context(
    config: Config, provider: Provider, *, database: Path | None = None
) -> Context:
    limits = config.ratelimit
    client = WmsClient(
        provider,
        limiter=TokenBucket(limits.requests_per_second, limits.burst),
        max_retries=limits.max_retries,
        backoff=limits.initial_backoff_seconds,
    )
    store = await Store(database or config.store.database_path).open()
    return Context(config=config, client=client, store=store)
