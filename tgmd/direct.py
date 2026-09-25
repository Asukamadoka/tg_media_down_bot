"""``TG_DIRECT_MEDIA=v2``: direct media connections on keys of their own.

On the NAS, Telegram is reachable only through the proxy, except for a few
``media_only`` endpoints of DC2 and DC4 that answer directly. Downloading
from those saves the proxy's bandwidth.

The first attempt at this (``auto``, docs/wms/M7 §7.1) reused keys Telethon
also used through the proxy: the session's own key, or the key of Telethon's
exported sender. One key seen from two IP addresses at once is what Telegram
calls a stolen key, and it revoked the reading account's session
(AuthKeyDuplicatedError).

This route never shares a key (docs/wms/M7.1 §B2):

* It serves only DCs other than the account's home DC. For the home DC the
  only key is the session's, so that case is refused outright.
* For each target DC it negotiates a new key, with a Diffie-Hellman exchange
  on a direct connection. The main connection exports an authorisation for
  that DC, and the direct connection imports it. The key is used only on
  direct connections to ``media_only`` endpoints, from one kind of address
  (IPv4 or IPv6). It is never handed to Telethon and never goes through the
  proxy.
* Keys are stored in the ``direct_keys`` table, so a restart reuses them.
  A key Telegram rejects, or an import that fails, drops the key and rests
  that DC for 24 hours. A flood wait rests the DC too. Every refusal falls
  back to the ordinary route.
* At most four direct connections per DC run at once. They come out of
  ``DOWNLOAD_CONNECTIONS``; this route never adds connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable

from telethon.crypto import AuthKey
from telethon.errors import AuthKeyNotFound, FloodWaitError, RPCError
from telethon.errors.rpcbaseerrors import AuthKeyError, UnauthorizedError
from telethon.network import MTProtoSender
from telethon.tl.functions.auth import ExportAuthorizationRequest, ImportAuthorizationRequest
from telethon.tl.functions.help import GetNearestDcRequest

from .parallel import (
    ParallelUnavailable,
    PartSource,
    _init_connection,
    _SenderSource,
    media_endpoints,
)

log = logging.getLogger(__name__)

ROUTE = "direct-v2"

# M7.1 §B2.7: at most this many direct connections to one DC at a time.
PER_DC = 4

# A rejected key, a failed import or a flood wait rests the DC this long.
COOLDOWN = 24 * 3600.0

# No media endpoint answered: the network changed. Worth another look sooner.
UNREACHABLE_COOLDOWN = 1800.0

_COOLDOWN_KEY = "direct_v2_cooldown"

# The errors that mean a key is no good: unknown to Telegram (the -404
# transport error Telethon raises as AuthKeyNotFound), unregistered, revoked
# (the 401 family), or duplicated (406).
KEY_ERRORS = (AuthKeyNotFound, UnauthorizedError, AuthKeyError)


class DirectFlood(ParallelUnavailable):
    """A flood wait on the direct route. The fallback waits it out first."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"flood wait of {seconds}s on the direct route")
        self.seconds = seconds


class _ImportFailed(Exception):
    """``auth.importAuthorization`` failed on the direct connection."""


def egress_of(endpoint) -> str:
    """Which kind of address a connection to ``endpoint`` leaves from."""
    return "ipv6" if getattr(endpoint, "ipv6", False) else "ipv4"


class DirectRouteV2:
    """Direct media connections to non-home DCs, on keys only they use."""

    def __init__(
        self,
        db,
        *,
        per_dc: int = PER_DC,
        cooldown: float = COOLDOWN,
        connect_timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._per_dc = per_dc
        self._cooldown = cooldown
        self._connect_timeout = connect_timeout
        self._clock = clock
        self._in_use: dict[int, int] = defaultdict(int)
        self._resting: dict[int, float] | None = None  # loaded on first use
        self._key_locks: dict[tuple, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ------------------------------------------------------------ when to use

    @staticmethod
    def home_dc(client) -> int | None:
        session = getattr(client, "session", None)
        return getattr(session, "dc_id", None)

    async def resting(self, dc_id: int) -> bool:
        """True while ``dc_id`` is cooling down after a refusal."""
        rest = await self._rest()
        until = rest.get(dc_id)
        return until is not None and self._clock() < until

    def free(self, dc_id: int) -> int:
        return max(0, self._per_dc - self._in_use[dc_id])

    async def available(self, client, dc_id: int | None) -> bool:
        """True when a file in ``dc_id`` may try this route now."""
        home = self.home_dc(client)
        if dc_id is None or home is None or dc_id == home:
            return False
        if self.free(dc_id) == 0 or await self.resting(dc_id):
            return False
        try:
            return bool(await media_endpoints(client, dc_id))
        except Exception:  # an optimisation must never fail a download
            log.debug("could not list media endpoints for DC %s", dc_id, exc_info=True)
            return False

    # ------------------------------------------------------------ connections

    @contextlib.asynccontextmanager
    async def sources(
        self, client, dc_id: int, count: int
    ) -> AsyncIterator[tuple[list[PartSource], str]]:
        """Open up to ``count`` direct connections to ``dc_id``.

        Yields the sources and a label for the log. Raises
        :class:`ParallelUnavailable` (or :class:`DirectFlood`) when the
        ordinary route should be used instead.
        """
        home = self.home_dc(client)
        if home is None or dc_id == home:
            # M7.1 §B2.1, a hard rule: the home DC's only key is the session's.
            raise AssertionError(f"direct-v2 never serves the home DC (DC {dc_id})")
        if await self.resting(dc_id):
            raise ParallelUnavailable(f"the direct route to DC {dc_id} is resting")
        slots = min(count, self.free(dc_id))
        if slots <= 0:
            raise ParallelUnavailable(f"DC {dc_id} already has {self._per_dc} direct connections")

        self._in_use[dc_id] += slots
        senders: list[MTProtoSender] = []
        try:
            account, egress, label = await self._open_all(client, dc_id, slots, senders)
            # Fewer connections than reserved: give the rest back at once.
            self._in_use[dc_id] -= slots - len(senders)
            slots = len(senders)
            yield (
                [_Guarded(self, _SenderSource(s), account, dc_id, egress) for s in senders],
                label,
            )
        finally:
            for sender in senders:
                with contextlib.suppress(Exception):
                    await sender.disconnect()
            self._in_use[dc_id] -= slots

    async def _open_all(self, client, dc_id: int, count: int, senders: list) -> tuple:
        """Open the connections into ``senders``; return (account, egress, label)."""
        account = await self._account(client)
        endpoints = await media_endpoints(client, dc_id)  # IPv4 first
        for endpoint in endpoints:
            egress = egress_of(endpoint)
            try:
                first, key = await self._first(client, account, dc_id, endpoint, egress)
            except (ConnectionError, OSError, TimeoutError) as exc:
                log.info("direct-v2: %s did not answer: %s", _describe(endpoint), exc)
                continue
            senders.append(first)
            for _ in range(count - 1):
                try:
                    senders.append(await self._timed(self._open(client, key, endpoint, dc_id)))
                except (ConnectionError, OSError, TimeoutError) as exc:
                    log.info("direct-v2: opened %d of %d connections: %s",
                             len(senders), count, exc)
                    break
                except KEY_ERRORS as exc:
                    await self.drop(account, dc_id, egress, exc)
                    raise ParallelUnavailable(f"DC {dc_id} rejected the direct key") from exc
            return account, egress, f"{ROUTE} {_describe(endpoint)}"
        await self.rest(dc_id, "no media endpoint answered directly", UNREACHABLE_COOLDOWN)
        raise ParallelUnavailable(f"no media endpoint of DC {dc_id} answered directly")

    async def _first(self, client, account: int, dc_id: int, endpoint, egress: str):
        """The first connection: on the stored key, or on a newly negotiated one."""
        async with self._key_locks[(account, dc_id, egress)]:
            stored = await self._db.direct_key_get(account, dc_id, egress)
            if stored is not None:
                try:
                    return await self._timed(self._open(client, stored, endpoint, dc_id)), stored
                except KEY_ERRORS as exc:
                    await self.drop(account, dc_id, egress, exc)
                    raise ParallelUnavailable(f"DC {dc_id} rejected the direct key") from exc
                except FloodWaitError as exc:
                    await self._flood(dc_id, exc)
            try:
                # A key exchange plus a round trip through the proxy: more
                # than a plain connect, so a longer bound.
                sender = await asyncio.wait_for(
                    self._negotiate(client, endpoint, dc_id), 3 * self._connect_timeout
                )
            except FloodWaitError as exc:
                await self._flood(dc_id, exc)
            except (_ImportFailed, *KEY_ERRORS) as exc:
                await self.rest(dc_id, f"a new key for DC {dc_id} was refused: {exc}")
                raise ParallelUnavailable(
                    f"could not authorise a direct key for DC {dc_id}"
                ) from exc
            key = sender.auth_key.key
            await self._db.direct_key_store(account, dc_id, egress, key)
            log.info("direct-v2: negotiated a new key for DC %s over %s", dc_id, egress)
            return sender, key

    async def _negotiate(self, client, endpoint, dc_id: int) -> MTProtoSender:
        """A new key by DH on a direct connection, authorised by import."""
        # auth_key None: the sender runs the key exchange as it connects.
        sender = MTProtoSender(None, loggers=client._log)  # noqa: SLF001
        await sender.connect(_direct_connection(client, endpoint, dc_id))
        try:
            # Over the main connection (through the proxy): only the one-time
            # authorisation bytes cross it, never the new key.
            auth = await client(ExportAuthorizationRequest(dc_id))
            try:
                await sender.send(
                    _init_connection(
                        client, ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
                    )
                )
            except FloodWaitError:
                raise
            except RPCError as exc:
                raise _ImportFailed(str(exc)) from exc
        except BaseException:
            await sender.disconnect()
            raise
        return sender

    async def _open(self, client, key: bytes, endpoint, dc_id: int) -> MTProtoSender:
        sender = MTProtoSender(AuthKey(key), loggers=client._log)  # noqa: SLF001
        await sender.connect(_direct_connection(client, endpoint, dc_id))
        try:
            await sender.send(_init_connection(client, GetNearestDcRequest()))
        except BaseException:
            await sender.disconnect()
            raise
        return sender

    async def _timed(self, coro):
        return await asyncio.wait_for(coro, self._connect_timeout)

    async def _account(self, client) -> int:
        me = await client.get_me(input_peer=True)
        return int(me.user_id)

    # ------------------------------------------------------------ refusals

    async def drop(self, account: int, dc_id: int, egress: str, why) -> None:
        """Forget a key Telegram rejected, and rest the DC."""
        await self._db.direct_key_forget(account, dc_id, egress)
        await self.rest(dc_id, f"DC {dc_id} rejected the direct key ({type(why).__name__})")

    async def rest(self, dc_id: int, why: str, seconds: float | None = None) -> None:
        seconds = self._cooldown if seconds is None else seconds
        rest = await self._rest()
        rest[dc_id] = max(rest.get(dc_id, 0.0), self._clock() + seconds)
        await self._db.kv_set_json(_COOLDOWN_KEY, {str(k): v for k, v in rest.items()})
        log.warning("direct-v2: %s; DC %s uses the proxy route for %.0f h",
                    why, dc_id, seconds / 3600)

    async def _flood(self, dc_id: int, exc: FloodWaitError):
        await self.rest(dc_id, f"flood wait of {exc.seconds}s")
        raise DirectFlood(exc.seconds) from exc

    async def _rest(self) -> dict[int, float]:
        if self._resting is None:
            stored = await self._db.kv_get_json(_COOLDOWN_KEY) or {}
            self._resting = {int(k): float(v) for k, v in stored.items()}
        return self._resting


class _Guarded:
    """A direct source whose refusals rest the DC instead of being waited out."""

    def __init__(self, route: DirectRouteV2, inner: PartSource, account: int, dc_id: int,
                 egress: str) -> None:
        self._route = route
        self._inner = inner
        self._account = account
        self._dc_id = dc_id
        self._egress = egress

    async def get(self, location, offset: int, limit: int) -> bytes:
        try:
            return await self._inner.get(location, offset, limit)
        except FloodWaitError as exc:
            # M7.1 §B2.7: a flood wait sends the file back to the ordinary
            # route (after the wait) and rests this DC.
            await self._route._flood(self._dc_id, exc)  # noqa: SLF001
            raise  # unreachable: _flood raises
        except KEY_ERRORS as exc:
            await self._route.drop(self._account, self._dc_id, self._egress, exc)
            raise ParallelUnavailable(f"DC {self._dc_id} rejected the direct key") from exc


def _direct_connection(client, endpoint, dc_id: int):
    """A connection that never goes through Telethon's configured proxy."""
    return client._connection(  # noqa: SLF001
        endpoint.ip_address,
        endpoint.port,
        dc_id,
        loggers=client._log,  # noqa: SLF001
        proxy=None,
        local_addr=client._local_addr,  # noqa: SLF001
    )


def _describe(endpoint) -> str:
    ip = getattr(endpoint, "ip_address", "?")
    ip = f"[{ip}]" if ":" in str(ip) else ip
    return f"{ip}:{getattr(endpoint, 'port', '?')}"
