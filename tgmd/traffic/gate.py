"""The download gate and the rate limits.

Telegram downloads and uploads ask the :class:`TrafficControl` before each
file and between parts. A closed gate makes them wait; it never fails or
drops them. Everything the operator can change at runtime (paused, the rate
limits, which alerts already went out) lives in the bot's database, so a
restart does not forget it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import TrafficConfig
    from ..db import Database

log = logging.getLogger(__name__)

STATE_KEY = "traffic:state"
MB = 1024 * 1024
UPLOAD_FINAL_BYTES = 2 * MB

Notice = Callable[[], Awaitable[None] | None]

OPEN, PAUSED, OVER_BUDGET = "open", "paused", "over_budget"


class TokenBucket:
    """An average-rate limiter shared by every worker and part.

    Spending more than the bucket holds leaves it in debt and the spender
    waits the debt off, so the long-run rate is exactly ``rate`` whatever the
    size of each request. The burst is ``burst`` seconds' worth (one by default).
    """

    def __init__(self, rate: float = 0.0, *, burst: float = 1.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._burst = burst
        self._clock = clock
        self._sleep = sleep
        self._rate = 0.0
        self._tokens = 0.0
        self._at = clock()
        self.set_rate(rate)

    @property
    def rate(self) -> float:
        return self._rate

    def set_rate(self, bytes_per_second: float) -> None:
        self._rate = max(0.0, float(bytes_per_second))
        capacity = self._rate * self._burst
        self._tokens = min(self._tokens, capacity)
        if self._rate and self._tokens == 0.0:
            self._tokens = capacity
        self._at = self._clock()

    async def take(self, amount: int) -> None:
        if self._rate <= 0 or amount <= 0:
            return
        now = self._clock()
        self._tokens = min(self._rate * self._burst, self._tokens + (now - self._at) * self._rate)
        self._at = now
        self._tokens -= amount
        if self._tokens < 0:
            await self._sleep(-self._tokens / self._rate)


class TrafficControl:
    """Gate, rate limits and the persistent bits of M9 in one place."""

    def __init__(self, config: TrafficConfig, db: Database | None = None, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._config = config
        self._db = db
        self.paused = False
        self.over_budget: str | None = None
        """Which budget closed the gate (``daily_cny:2026-10-02``), or None."""
        self.forced: str | None = None
        """A budget the operator reopened the gate against; not reasserted."""
        self._rate_override: dict[str, float | None] = {"media": None, "upload": None}
        self.alerts: dict[str, str] = {}
        self._uploads: dict[object, bool] = {}
        self.last_report_day = ""
        self.alert_days: dict[str, int] = {}
        self._open = asyncio.Event()
        self._open.set()
        self._direct_open = asyncio.Event()
        self._direct_open.set()
        self.media = TokenBucket(clock=clock, sleep=sleep)
        self.upload = TokenBucket(clock=clock, sleep=sleep)
        self._apply_rates()

    # ------------------------------------------------------------ persistence

    async def load(self) -> None:
        if self._db is None:
            return
        data = await self._db.kv_get_json(STATE_KEY)
        if not isinstance(data, dict):
            return
        self.paused = bool(data.get("paused"))
        self.over_budget = data.get("over_budget") or None
        self.forced = data.get("forced") or None
        for kind in ("media", "upload"):
            value = data.get(f"rate_{kind}")
            self._rate_override[kind] = float(value) if value is not None else None
        self.alerts = dict(data.get("alerts") or {})
        self.last_report_day = str(data.get("last_report_day") or "")
        self.alert_days = {str(k): int(v) for k, v in (data.get("alert_days") or {}).items()}
        self._apply_rates()
        self._sync()

    async def _save(self) -> None:
        if self._db is None:
            return
        await self._db.kv_set_json(
            STATE_KEY,
            {
                "paused": self.paused,
                "over_budget": self.over_budget,
                "forced": self.forced,
                "rate_media": self._rate_override["media"],
                "rate_upload": self._rate_override["upload"],
                "alerts": self.alerts,
                "last_report_day": self.last_report_day,
                "alert_days": self.alert_days,
            },
        )

    # ------------------------------------------------------------------- gate

    @property
    def state(self) -> str:
        if self.paused:
            return PAUSED
        if self.over_budget:
            return OVER_BUDGET
        return OPEN

    @property
    def is_open(self) -> bool:
        return self.state == OPEN

    def _sync(self) -> None:
        if self.is_open:
            self._open.set()
        else:
            self._open.clear()

    async def pause(self) -> None:
        self.paused = True
        self._sync()
        await self._save()

    async def resume(self) -> None:
        """Reopen the gate by hand, also against a budget that closed it."""
        if self.over_budget:
            self.forced = self.over_budget
        self.paused = False
        self.over_budget = None
        self._sync()
        await self._save()

    async def set_over_budget(self, reason: str) -> bool:
        """Close the gate for a budget. False when the operator already
        reopened it against this very budget."""
        if reason == self.forced:
            return False
        if self.over_budget != reason:
            self.over_budget = reason
            self._sync()
            await self._save()
        return True

    async def clear_over_budget(self) -> None:
        if self.over_budget is not None:
            self.over_budget = None
            self._sync()
            await self._save()

    async def wait_open(self, cancel: asyncio.Event | None = None) -> bool:
        """Wait while the gate is closed. False if ``cancel`` was set meanwhile."""
        while not self._open.is_set():
            if cancel is not None and cancel.is_set():
                return False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._open.wait(), 1.0)
        return True

    async def before_file(self, kind: str, *, notice: Notice | None = None,
                          cancel: asyncio.Event | None = None) -> bool:
        """Called before a whole Telegram download or upload. Waits while closed,
        telling ``notice`` once. False means the job was cancelled while waiting."""
        if self._open.is_set():
            return True
        log.info("traffic gate closed (%s); %s waits", self.state, kind)
        if notice is not None:
            result = notice()
            if asyncio.iscoroutine(result):
                await result
        return await self.wait_open(cancel)

    async def before_part(self, kind: str, *, cancel: asyncio.Event | None = None) -> bool:
        """Between parts of a download or upload."""
        return await self.wait_open(cancel)

    # ----------------------------------------------------------- direct (NAS)

    def set_direct_over(self, over: bool) -> None:
        if over:
            self._direct_open.clear()
        else:
            self._direct_open.set()

    @property
    def direct_over(self) -> bool:
        return not self._direct_open.is_set()

    async def before_direct(self, amount: int = 0) -> None:
        """The WMS outbound download, only held back by TRAFFIC_DIRECT_DAILY_GB."""
        while not self._direct_open.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._direct_open.wait(), 1.0)

    # ------------------------------------------------------------ rate limits

    def rate_mbps(self, kind: str) -> float:
        """The limit in force for ``media`` or ``upload``, MB/s; 0 is unlimited."""
        override = self._rate_override[kind]
        if override is not None:
            return override
        return self._config.media_rate_mbps if kind == "media" else self._config.upload_rate_mbps

    def _apply_rates(self) -> None:
        self.media.set_rate(self.rate_mbps("media") * MB)
        self.upload.set_rate(self.rate_mbps("upload") * MB)

    async def set_rate(self, kind: str, mbps: float | None) -> None:
        """Set the runtime limit (0 = unlimited, None = back to the environment)."""
        self._rate_override[kind] = mbps
        self._apply_rates()
        await self._save()

    async def pace(self, kind: str, amount: int) -> None:
        """Spend ``amount`` bytes of ``media`` (downloads) or ``upload`` allowance."""
        await (self.media if kind == "media" else self.upload).take(amount)

    # ------------------------------------------------------ uploads in flight

    def note_upload(self, token: object, sent: int, total: int) -> None:
        """An upload's progress; its last part is where the exit must not change."""
        self._uploads[token] = total - sent <= UPLOAD_FINAL_BYTES

    def end_upload(self, token: object) -> None:
        self._uploads.pop(token, None)

    @property
    def upload_final(self) -> bool:
        """True while any Telegram upload is in its final part."""
        return any(self._uploads.values())

    # ----------------------------------------------------------------- alerts

    async def claim_alert(self, key: str, day: str) -> bool:
        """True the first time ``key`` is claimed; later calls (and restarts) get False."""
        if key in self.alerts:
            return False
        self.alerts[key] = day
        self.alert_days[day] = self.alert_days.get(day, 0) + 1
        # Forget what is too old to matter again.
        horizon = sorted({*self.alerts.values(), *self.alert_days})[-40:]
        if horizon:
            oldest = horizon[0]
            self.alerts = {k: v for k, v in self.alerts.items() if v >= oldest}
            self.alert_days = {k: v for k, v in self.alert_days.items() if k >= oldest}
        await self._save()
        return True

    async def mark_reported(self, day: str) -> None:
        self.last_report_day = day
        await self._save()


class NullControl:
    """No gate, no limits: what a Downloader or Delivery uses by default."""

    async def before_file(self, kind, *, notice=None, cancel=None) -> bool:
        return True

    async def before_part(self, kind, *, cancel=None) -> bool:
        return True

    async def pace(self, kind, amount) -> None:
        return None

    def note_upload(self, token, sent, total) -> None:
        return None

    def end_upload(self, token) -> None:
        return None

    upload_final = False

    async def before_direct(self, amount=0) -> None:
        return None
