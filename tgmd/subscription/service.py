"""The revival flow (docs/wms/M9.6 §A-F): detect, open a case, accept a URL, validate,
switch, roll back. UI-neutral; :mod:`tgmd.subscription.ui` turns outcomes into messages.

``accept_url`` is the one entry point for a new subscription URL. The URL lives in memory
while handled and in one kv value afterwards; nothing here logs, stores or raises it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import detect
from .detect import Snapshot, compile_sentinel, real_count
from .fetch import Fetched, Fetcher, make_fetcher
from .redact import redact_url
from .switch import Switcher, SwitchResult
from .validate import (
    Rejected,
    Resolver,
    Validated,
    check_url,
    system_resolve,
    validate_url,
)

if TYPE_CHECKING:
    from ..config import SubscriptionConfig
    from ..db import Database
    from ..traffic.mihomo import MihomoClient

log = logging.getLogger(__name__)

URL_KEY = "sub:url"
PREV_URL_KEY = "sub:url_prev"
STATE_KEY = "sub:state"
ARM_SECONDS = 600.0
REMINDER_EVERY = 12 * 3600.0
MAX_REMINDERS = 3
LATER_HOURS = 6.0
REOPEN_AFTER = 12 * 3600.0
BIG_CHANGE = 0.30

Notify = Callable[..., Awaitable[None]]
Say = Callable[[str, dict], Awaitable[None]]
"""``(message kind, data)``; :mod:`ui` renders it. Kinds: ``case``, ``reminder``,
``recovered``, ``switched``, ``switch_failed``, ``refresh_failed``, ``refreshed``."""


@dataclass
class Outcome:
    """What ``accept_url`` and ``switch_staged`` report to the caller."""

    kind: str
    """``rejected``, ``staged`` (cannot switch), ``switched``, ``failed``, ``nothing``."""
    code: str = ""
    detail: str = ""
    validated: Validated | None = None
    old_real: int | None = None
    old_names: list[str] = field(default_factory=list)
    result: SwitchResult | None = None


@dataclass
class _Staged:
    url: str
    validated: Validated
    case_id: int

    def __repr__(self) -> str:  # a URL must never reach a log through a repr
        return f"<staged case {self.case_id} {redact_url(self.url)}>"


class SubscriptionService:
    def __init__(self, config: SubscriptionConfig, client: MihomoClient, db: Database, *,
                 say: Say | None = None, sub_hosts: tuple[str, ...] = (),
                 direct_hosts: Callable[[], list[str]] = list,
                 fetch: Fetcher | None = None, resolve: Resolver = system_resolve,
                 clock: Callable[[], float] = time.time, switcher: Switcher | None = None) -> None:
        self._config = config
        self._client = client
        self._db = db
        self._say = say
        self._sub_hosts = sub_hosts
        self._direct_hosts = direct_hosts
        self._fetch: Fetcher = fetch or make_fetcher(config.fetch_ua)
        self._resolve = resolve
        self._clock = clock
        self.switcher = switcher or Switcher(config, client, clock=clock)
        self._sentinel = compile_sentinel(config.sentinel_regex)
        self._busy = asyncio.Lock()
        self._staged: _Staged | None = None
        self.state: dict[str, Any] = {}

    # ----------------------------------------------------------------- lifecycle

    @property
    def can_switch(self) -> bool:
        return bool(self._config.provider_file)

    async def load(self) -> None:
        data = await self._db.kv_get_json(STATE_KEY)
        self.state = data if isinstance(data, dict) else {}

    async def _save(self) -> None:
        await self._db.kv_set_json(STATE_KEY, self.state)

    async def resume(self) -> None:
        """At start: a case left in ``switching`` is rolled back, and ``.new`` is removed."""
        case = await self._db.sub_case_open()
        if case is None:
            if self.can_switch:
                await asyncio.to_thread(self.switcher.new_path.unlink, True)
            return
        if case["state"] == "switching":
            result = await asyncio.to_thread(self.switcher.recover, case["meta"])
            await self._audit(case["id"], "recover", result.reason)
            await self._db.sub_case_update(case["id"], self._clock(), state="failed",
                                           last_error="interrupted")
            await self._tell("switch_failed", reason="interrupted", result=result)
        elif case["state"] in ("validating", "staged"):
            await self._db.sub_case_update(case["id"], self._clock(), state="awaiting_url")

    async def _audit(self, case_id: int | None, action: str, detail: str = "") -> None:
        await self._db.sub_audit_add(case_id, action, detail, self._clock())

    async def _tell(self, kind: str, **data: Any) -> None:
        if self._say is None:
            return
        try:
            await self._say(kind, data)
        except Exception:  # noqa: BLE001
            log.warning("could not deliver a subscription message (%s)", kind, exc_info=False)

    # ------------------------------------------------------------------- reading

    async def stored_url(self) -> str:
        return await self._db.kv_get(URL_KEY) or ""

    def _is_direct(self, host: str) -> bool | None:
        """Evidence from mihomo's live connections, else the direct list and ``SUB_HOSTS``."""
        seen: bool | None = None
        try:
            connections = self._client.connections().get("connections") or []
        except Exception:  # noqa: BLE001
            connections = []
        for connection in connections:
            if str((connection.get("metadata") or {}).get("host") or "").lower() != host:
                continue
            chains = [str(n) for n in connection.get("chains") or []]
            if chains and not any(n.upper() == "DIRECT" for n in chains):
                return False
            seen = True
        if seen:
            return True
        listed = [*self._sub_hosts, *self._direct_hosts()]
        return True if any(host == d or host.endswith("." + d) for d in listed) else None

    def _validate(self, url: str) -> tuple[Validated, Fetched]:
        return validate_url(
            url, fetch=self._fetch, is_direct=self._is_direct, min_nodes=self._config.min_nodes,
            sentinel=self._sentinel, allow_private=self._config.allow_private,
            resolve=self._resolve, current_digest=self.switcher.current_digest()
            if self.can_switch else None)

    def _provider_names(self) -> list[str] | None:
        try:
            return [str(p.get("name") or "") for p in self._client.provider().get("proxies") or []]
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ detection

    async def on_health(self, *, sick: bool, alive: int, total: int) -> None:
        """Called by ``NodeManager.health`` every minute with its own verdict."""
        names = await asyncio.to_thread(self._provider_names)
        now = self._clock()
        fetch = self.state.get("fetch") or {}
        snap = Snapshot(now, names, sick, fetch.get("status"), self.state.get("userinfo"),
                        self.state.get("healthy_real"))
        found = detect.signals(snap, sentinel=self._sentinel, warn_days=self._config.warn_days)
        level = detect.level(found)
        counts = {"alive": alive, "total": total,
                  "real": real_count(names, self._sentinel) if names is not None else None,
                  "healthy": self.state.get("healthy_real")}
        await self._apply_level(level, found, counts, now)
        if level == detect.OK and names is not None:
            self.state["healthy_real"] = real_count(names, self._sentinel)
            await self._save()

    async def _apply_level(self, level: str, found: list[str], counts: dict, now: float) -> None:
        case = await self._db.sub_case_open()
        if level == detect.OK:
            self.state["cleared"] = True
            if case is not None and case["state"] in ("open", "awaiting_url", "snoozed"):
                await self._db.sub_case_update(case["id"], now, state="done", last_error="")
                await self._audit(case["id"], "cleared")
                self.state["last_closed"] = now
                await self._save()
                await self._tell("recovered", **counts)
            return
        if case is not None:
            if case["signals"] != found:
                await self._db.sub_case_update(case["id"], now, signals=found)
            return
        if not self.state.get("cleared", True) and \
                now - float(self.state.get("last_closed") or 0.0) < REOPEN_AFTER:
            return  # the last case ended while the signal never cleared: do not nag
        await self._open_case(level, found, counts, now)

    async def _open_case(self, level: str, found: list[str], counts: dict, now: float) -> int:
        case_id = await self._db.sub_case_insert("open", found, now, now + REMINDER_EVERY)
        self.state["cleared"] = False
        await self._save()
        await self._audit(case_id, "open", ",".join(found))
        log.info("subscription case %s opened (%s)", case_id, ",".join(found) or "manual")
        await self._tell("case", level=level, signals=found, case_id=case_id, **counts)
        return case_id

    # --------------------------------------------------------------------- ticks

    async def tick(self, now: float | None = None) -> None:
        now = now if now is not None else self._clock()
        hours = self._config.check_hours
        if hours > 0 and now - float(self.state.get("last_check") or 0.0) >= hours * 3600:
            self.state["last_check"] = now
            await self._save()
            await self.check_fetch(now)
        hours = self._config.refresh_hours
        if (hours > 0 and self.can_switch
                and now - float(self.state.get("last_refresh") or 0.0) >= hours * 3600):
            self.state["last_refresh"] = now
            await self._save()
            await self.refresh()
        await self._remind(now)

    async def check_fetch(self, now: float | None = None) -> None:
        """The bot's own periodic look at the stored URL (signal ``fetch_dead``)."""
        url = await self.stored_url()
        if not url:
            return
        status: int | None = None
        try:
            fetched = await asyncio.to_thread(self._fetch_only, url)
            status = fetched.status
            if status == 200:
                self.state["userinfo"] = fetched.headers.get("subscription-userinfo", "")
        except Rejected as exc:
            log.info("subscription check: %s", exc.code)
        except Exception:  # noqa: BLE001
            log.info("subscription check failed (%s)", redact_url(url))
        self.state["fetch"] = {"status": status, "at": now or self._clock()}
        await self._save()

    def _fetch_only(self, url: str) -> Fetched:
        host = check_url(url, allow_private=self._config.allow_private, resolve=self._resolve)
        if self._is_direct(host) is not True:
            raise Rejected("not_direct")
        try:
            return self._fetch(url)
        except Exception as exc:  # noqa: BLE001 - a fetch error carries a code, never the URL
            raise Rejected("fetch", getattr(exc, "code", "network")) from None

    async def _remind(self, now: float) -> None:
        case = await self._db.sub_case_open()
        if case is None or case["remind_at"] is None or case["remind_at"] > now:
            return
        if case["state"] not in ("open", "snoozed", "awaiting_url"):
            return
        if case["reminders"] >= MAX_REMINDERS:
            await self._db.sub_case_update(case["id"], now, remind_at=None)
            return
        number = case["reminders"] + 1
        await self._db.sub_case_update(
            case["id"], now, state="open", reminders=number,
            remind_at=now + REMINDER_EVERY if number < MAX_REMINDERS else None)
        await self._audit(case["id"], "remind", str(number))
        await self._tell("reminder", number=number, signals=case["signals"], case_id=case["id"])

    # -------------------------------------------------------------- button actions

    async def _case_or_open(self) -> dict:
        case = await self._db.sub_case_open()
        if case is None:
            now = self._clock()
            case_id = await self._db.sub_case_insert("open", ["manual"], now, None)
            await self._audit(case_id, "open", "manual")
            case = await self._db.sub_case_open()
        assert case is not None
        return case

    async def arm(self) -> None:
        """「我已拿到新链接」: the next plain URL message is taken for ten minutes."""
        case = await self._case_or_open()
        await self._db.sub_case_update(case["id"], self._clock(), state="awaiting_url",
                                       armed_until=self._clock() + ARM_SECONDS)

    async def armed(self) -> bool:
        case = await self._db.sub_case_open()
        return bool(case and case["armed_until"] and case["armed_until"] > self._clock())

    async def later(self) -> None:
        case = await self._db.sub_case_open()
        if case is not None:
            await self._db.sub_case_update(case["id"], self._clock(), state="snoozed",
                                           remind_at=self._clock() + LATER_HOURS * 3600)

    async def skip(self) -> None:
        case = await self._db.sub_case_open()
        if case is not None:
            await self._db.sub_case_update(case["id"], self._clock(), state="snoozed",
                                           remind_at=None, armed_until=None)
            await self._audit(case["id"], "skip")

    # ------------------------------------------------------------------ the flow

    async def accept_url(self, url: str, *, source: str = "chat") -> Outcome:
        """The entry point for a new subscription URL (a future source could call it too):
        validate it, then switch to it (docs/wms/M9.6 §C: no approval step). Without
        ``SUB_PROVIDER_FILE`` it stops at ``staged``; after a failed switch the URL stays
        staged for ``/sub switch``."""
        async with self._busy:
            case = await self._case_or_open()
            now = self._clock()
            await self._db.sub_case_update(case["id"], now, state="validating", armed_until=None)
            await self._audit(case["id"], "url", f"{source} {redact_url(url)}")
            old = await asyncio.to_thread(self._provider_names) or []
            try:
                validated, _ = await asyncio.to_thread(self._validate, url)
            except Rejected as exc:
                await self._db.sub_case_update(case["id"], self._clock(), state="awaiting_url",
                                               last_error=exc.code)
                self._staged = None
                log.info("subscription rejected: %s (%s)", exc.code, redact_url(url))
                return Outcome("rejected", exc.code, exc.detail)
            self._staged = _Staged(url, validated, case["id"])
            await self._db.sub_case_update(case["id"], self._clock(), state="staged",
                                           last_error="")
            old_real = real_count(old, self._sentinel)
            if self.can_switch:  # no approval gate: a valid URL from an admin is switched
                outcome = await self._do_switch(validated, url, case["id"])
                if outcome.kind == "switched":
                    self._staged = None
                return outcome
            return Outcome("staged", validated=validated, old_real=old_real, old_names=old)

    async def switch_staged(self) -> Outcome:
        """``/sub switch``: validate the staged URL again, then switch."""
        staged = self._staged
        if staged is None:
            return Outcome("nothing")
        async with self._busy:
            if not self.can_switch:
                return Outcome("rejected", "no_file")
            try:
                validated, _ = await asyncio.to_thread(self._validate, staged.url)
            except Rejected as exc:
                await self._db.sub_case_update(staged.case_id, self._clock(),
                                               state="awaiting_url", last_error=exc.code)
                self._staged = None
                return Outcome("rejected", exc.code, exc.detail)
            outcome = await self._do_switch(validated, staged.url, staged.case_id)
            if outcome.kind == "switched":
                self._staged = None
            return outcome

    async def _do_switch(self, validated: Validated, url: str, case_id: int | None) -> Outcome:
        loop = asyncio.get_running_loop()
        now = self._clock()
        if case_id is not None:
            await self._db.sub_case_update(case_id, now, state="switching")

        def checkpoint(meta: dict) -> None:  # runs in the worker thread
            if case_id is not None:
                asyncio.run_coroutine_threadsafe(
                    self._db.sub_case_update(case_id, self._clock(), meta=meta), loop
                ).result(timeout=10)

        old = await asyncio.to_thread(self._provider_names) or []
        result = await asyncio.to_thread(self.switcher.switch, validated, checkpoint=checkpoint)
        if result.ok:
            previous = await self.stored_url()
            if previous and previous != url:
                await self._db.kv_set(PREV_URL_KEY, previous)
            await self._db.kv_set(URL_KEY, url)
            self.state.update({"healthy_real": validated.real_count, "fetch": {},
                               "cleared": True, "last_closed": self._clock()})
            await self._save()
            if case_id is not None:
                await self._db.sub_case_update(case_id, self._clock(), state="done",
                                               last_error="")
            await self._audit(case_id, "switched",
                              f"{validated.real_count} nodes {validated.digest[:8]}")
            log.info("subscription switched: %d nodes (%s)", validated.real_count,
                     redact_url(url))
            return Outcome("switched", validated=validated,
                           old_real=real_count(old, self._sentinel), old_names=old, result=result)
        if case_id is not None:
            await self._db.sub_case_update(case_id, self._clock(), state="failed",
                                           last_error=result.reason)
            self.state["last_closed"] = self._clock()  # no new case at once for the same signal
            await self._save()
        await self._audit(case_id, "switch_failed", result.reason)
        log.warning("subscription switch failed: %s (restored ok: %s)", result.reason,
                    result.restored_ok)
        return Outcome("failed", result.reason, validated=validated, result=result)

    async def refresh(self) -> Outcome | None:
        """Re-fetch the stored URL and switch if it changed (replaces mihomo's own interval).
        Silent unless it fails or the nodes changed by 30 % or more."""
        url = await self.stored_url()
        if not url or not self.can_switch:
            return None
        async with self._busy:
            failed_before = bool(self.state.get("refresh_failed"))
            try:
                validated, fetched = await asyncio.to_thread(self._validate, url)
            except Rejected as exc:
                if exc.code == "same":
                    self.state.update(refresh_failed=False)
                    await self._save()
                    return Outcome("nothing")
                self.state["fetch"] = {"status": None, "at": self._clock()}
                self.state["refresh_failed"] = True
                await self._save()
                if exc.code == "status":
                    self.state["fetch"] = {"status": int(exc.detail or 0) or None,
                                           "at": self._clock()}
                if not failed_before:
                    await self._tell("refresh_failed", code=exc.code, detail=exc.detail)
                return Outcome("rejected", exc.code, exc.detail)
            self.state["refresh_failed"] = False
            self.state["userinfo"] = fetched.headers.get("subscription-userinfo", "")
            self.state["fetch"] = {"status": 200, "at": self._clock()}
            before = real_count(await asyncio.to_thread(self._provider_names) or [],
                                self._sentinel)
            outcome = await self._do_switch(validated, url, None)
            if outcome.kind == "failed":
                await self._tell("switch_failed", reason=outcome.code, result=outcome.result)
            elif before and abs(validated.real_count - before) * 100 >= BIG_CHANGE * 100 * before:
                await self._tell("refreshed", old=before, new=validated.real_count)
            return outcome

    async def rollback(self) -> SwitchResult:
        """``/sub rollback`` (after the confirm button): the newest backup is active again,
        and the URL that produced it is the stored one."""
        async with self._busy:
            result = await asyncio.to_thread(self.switcher.rollback)
            await self._audit(None, "rollback", result.reason or "ok")
            if result.ok:
                previous = await self._db.kv_get(PREV_URL_KEY)
                if previous:
                    await self._db.kv_set(URL_KEY, previous)
                    await self._db.kv_delete(PREV_URL_KEY)
                else:
                    await self._db.kv_delete(URL_KEY)  # unknown: do not refresh over the backup
            return result

    async def status(self) -> dict[str, Any]:
        names = await asyncio.to_thread(self._provider_names)
        case = await self._db.sub_case_open()
        updated = None
        with contextlib.suppress(Exception):
            updated = (await asyncio.to_thread(self._client.provider)).get("updatedAt")
        return {
            "enabled": self._config.enabled, "can_switch": self.can_switch,
            "total": len(names) if names is not None else None,
            "real": real_count(names, self._sentinel) if names is not None else None,
            "updated_at": updated, "last_refresh": self.state.get("last_refresh"),
            "last_check": self.state.get("last_check"),
            "fetch_status": (self.state.get("fetch") or {}).get("status"),
            "has_url": bool(await self.stored_url()),
            "case": case["state"] if case else None,
            "signals": case["signals"] if case else [],
            "staged": self._staged is not None,
            "backups": [p.name for p in self.switcher.backups()] if self.can_switch else [],
        }
