"""The meter's background task: poll, account, flush, budget, alert, report."""

from __future__ import annotations

import asyncio
import html
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..i18n import t
from .classify import parse_model_host
from .gate import TrafficControl
from .meter import Delta, Meter, Step
from .mihomo import TG_GROUP, MihomoClient
from .pricing import short_node
from .report import MB, Budgets, Report, build_report, fmt_bytes, fmt_cny, render
from .store import (
    HOST_MIN_BYTES,
    HostKey,
    HourKey,
    TrafficStore,
    hour_of,
    local_day,
    period_for,
)

if TYPE_CHECKING:
    from ..config import TrafficConfig

log = logging.getLogger(__name__)

FLUSH_SECONDS = 60.0
SPIKE_WINDOW = 60.0
SPIKE_SUSTAIN = 300.0

Notify = Callable[[str], Awaitable[None]]


class Spend:
    """Running totals for the current day and month, kept in memory so a budget
    can trip within one poll rather than one flush."""

    def __init__(self) -> None:
        self.day_key = ""
        self.month_key = ""
        self.day_cost = 0.0
        self.month_cost = 0.0
        self.day_proxy_bytes = 0
        self.direct_day_bytes = 0

    def reload(self, store: TrafficStore, now: datetime, tz: ZoneInfo) -> None:
        today = period_for("today", now, tz)
        day = store.rows(today)
        month = store.rows(period_for("month", now, tz))
        self.day_key = local_day(now, tz).isoformat()
        self.month_key = self.day_key[:7]
        self.day_cost = sum(r.cost for r in day if r.outbound == "proxy")
        self.month_cost = sum(r.cost for r in month if r.outbound == "proxy")
        self.day_proxy_bytes = sum(r.bytes for r in day if r.outbound == "proxy")
        self.direct_day_bytes = sum(
            r.bytes for r in day if r.outbound == "direct" and r.category not in ("lan", "model")
        )


class TrafficService:
    def __init__(
        self,
        config: TrafficConfig,
        store: TrafficStore,
        control: TrafficControl,
        *,
        notify: Notify | None = None,
        client: MihomoClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._store = store
        self.control = control
        self._notify = notify
        self._client = client or MihomoClient(config.mihomo_api)
        self._clock = clock
        self._tz = ZoneInfo(config.timezone)
        self._meter = Meter(parse_model_host(config.model_host))
        self._spend = Spend()
        self._hours: dict[HourKey, list[float]] = {}
        self._host_total: dict[HostKey, int] = {}
        self._host_flushed: dict[HostKey, int] = {}
        self._window: deque[tuple[float, int]] = deque()
        self._spike_since: float | None = None
        self._spike_fired = False
        self._top: tuple[str, str, str] = ("", "", "")
        self._last_flush = 0.0
        self._last_prune = ""
        self._task: asyncio.Task | None = None
        self.reachable: bool | None = None
        self._extras: list = []
        """Objects with ``async tick()`` (and optionally ``async stop()``) that share this loop:
        node selection and direct routing (docs/wms/M9.1)."""

    @property
    def client(self) -> MihomoClient:
        return self._client

    def add_extra(self, extra) -> None:
        self._extras.append(extra)

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        await asyncio.to_thread(self._store.open)
        now = self._clock()
        await asyncio.to_thread(self._spend.reload, self._store, now, self._tz)
        await self._evaluate(now)
        if self._config.enabled:
            self._task = asyncio.create_task(self._run(), name="traffic-meter")
            log.info("traffic meter polling %s every %gs", self._config.mihomo_api,
                     self._config.poll_seconds)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        for extra in self._extras:
            if hasattr(extra, "stop"):
                await extra.stop()
        try:
            await self.flush()
        finally:
            await asyncio.to_thread(self._store.close)

    async def _run(self) -> None:
        poll = self._config.poll_seconds
        delay = poll
        down = False
        while True:
            try:
                snapshot = await asyncio.to_thread(self._client.connections)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - an unreachable proxy must never reach the bot
                if not down:
                    log.warning("cannot read mihomo at %s (%s); retrying with back-off",
                                self._config.mihomo_api, exc)
                    down = True
                self.reachable = False
                delay = min(60.0, max(poll, delay * 2))
            else:
                if down:
                    log.info("mihomo is reachable again")
                    down = False
                self.reachable = True
                delay = poll
                try:
                    await self.ingest(snapshot)
                except Exception:
                    log.exception("traffic accounting failed")
            try:
                await self.tick()
            except Exception:
                log.exception("traffic housekeeping failed")
            await asyncio.sleep(delay)

    # ------------------------------------------------------------- accounting

    async def ingest(self, snapshot: dict, now: datetime | None = None) -> Step:
        """Account for one ``/connections`` snapshot."""
        now = now or self._clock()
        step = self._meter.ingest(snapshot)
        await self._roll(now)
        proxied = self._account(step, now)
        await self._alerts(step, proxied, now)
        await self._evaluate(now)
        return step

    def _price(self, delta: Delta) -> float:
        info = delta.info
        if info.outbound != "proxy":
            return 0.0
        price = info.price if info.price is not None else self._config.default_price
        return delta.bytes / self._config.bytes_per_gb * price

    def _account(self, step: Step, now: datetime) -> list[tuple[Delta, float]]:
        hour = hour_of(now)
        day = local_day(now, self._tz).isoformat()
        proxied: list[tuple[Delta, float]] = []
        for delta in step.deltas:
            info = delta.info
            cost = self._price(delta)
            slot = self._hours.setdefault((hour, info.category, info.outbound, info.node),
                                          [0, 0, 0.0])
            slot[0] += delta.up
            slot[1] += delta.down
            slot[2] += cost
            host_key = (day, info.host, info.category, info.outbound, info.node)
            self._host_total[host_key] = self._host_total.get(host_key, 0) + delta.bytes
            if info.outbound == "proxy":
                self._spend.day_cost += cost
                self._spend.month_cost += cost
                self._spend.day_proxy_bytes += delta.bytes
                proxied.append((delta, cost))
            elif info.category not in ("lan", "model"):
                self._spend.direct_day_bytes += delta.bytes
        if step.unattributed_up or step.unattributed_down:
            slot = self._hours.setdefault((hour, "unattributed", "unknown", ""), [0, 0, 0.0])
            slot[0] += step.unattributed_up
            slot[1] += step.unattributed_down

        # Rate window for the spike alert.
        stamp = now.timestamp()
        self._window.append((stamp, sum(d.bytes for d, _ in proxied)))
        while self._window and stamp - self._window[0][0] > SPIKE_WINDOW:
            self._window.popleft()
        if proxied:
            biggest = max(proxied, key=lambda item: item[0].bytes)[0].info
            self._top = (biggest.host, biggest.category, biggest.node)
        return proxied

    async def _roll(self, now: datetime) -> None:
        """A new local day: write out what is pending, then start the totals again."""
        if local_day(now, self._tz).isoformat() == self._spend.day_key:
            return
        await self.flush()
        await asyncio.to_thread(self._spend.reload, self._store, now, self._tz)

    # ----------------------------------------------------------------- flush

    async def flush(self) -> None:
        hours, self._hours = self._hours, {}
        hosts: list[tuple[HostKey, int]] = []
        before = dict(self._host_flushed)
        for key, total in self._host_total.items():
            owed = total - self._host_flushed.get(key, 0)
            if total > HOST_MIN_BYTES and owed > 0:
                hosts.append((key, owed))
                self._host_flushed[key] = total
        rows = [(key, int(up), int(down), cost) for key, (up, down, cost) in hours.items()]
        now = self._clock()
        try:
            if rows:
                await asyncio.to_thread(self._store.add_hours, rows)
            if hosts:
                await asyncio.to_thread(self._store.add_hosts, hosts)
            today = local_day(now, self._tz).isoformat()
            if self._last_prune != today:
                await asyncio.to_thread(self._store.prune, now, self._tz)
                self._last_prune = today
        except Exception:
            # Keep what could not be written for the next flush.
            for key, (up, down, cost) in hours.items():
                slot = self._hours.setdefault(key, [0, 0, 0.0])
                slot[0] += up
                slot[1] += down
                slot[2] += cost
            self._host_flushed = before
            raise
        today = local_day(now, self._tz).isoformat()
        for key in [k for k in self._host_total if k[0] < today]:
            self._host_total.pop(key, None)
            self._host_flushed.pop(key, None)
        self._last_flush = now.timestamp()

    async def tick(self) -> None:
        """Housekeeping that runs whether or not mihomo answered."""
        now = self._clock()
        if now.timestamp() - self._last_flush >= FLUSH_SECONDS:
            await self.flush()
        await self.maybe_report(now)
        for extra in self._extras:
            try:
                await extra.tick()
            except Exception:
                log.exception("traffic helper %s failed", type(extra).__name__)

    # -------------------------------------------------------- budgets and gate

    def _budgets(self) -> list[tuple[str, float, float, str, str]]:
        c, s = self._config, self._spend
        return [
            ("daily_cny", s.day_cost, c.budget_daily_cny, s.day_key, "cny"),
            ("monthly_cny", s.month_cost, c.budget_monthly_cny, s.month_key, "cny"),
            ("daily_proxy_gb", s.day_proxy_bytes / c.bytes_per_gb, c.budget_daily_proxy_gb,
             s.day_key, "gb"),
        ]

    async def _evaluate(self, now: datetime) -> None:
        """Budget alerts at 80% and 100%, and the gate that follows them."""
        control = self.control
        today = self._spend.day_key
        over: list[str] = []
        for name, used, limit, period, unit in self._budgets():
            if limit <= 0:
                continue
            ratio = used / limit
            if ratio >= 1:
                over.append(f"{name}:{period}")
            for percent in (80, 100):
                if ratio >= percent / 100 and await control.claim_alert(
                    f"budget:{name}:{percent}:{period}", today
                ):
                    pausing = percent == 100 and self._config.on_budget == "pause"
                    await self._send(t(
                        "traffic.alert.budget",
                        budget=t(f"traffic.budget.{name}"),
                        percent=percent,
                        used=fmt_cny(used) if unit == "cny" else f"{used:.2f} GB",
                        limit=fmt_cny(limit) if unit == "cny" else f"{limit:g} GB",
                        action=(t("traffic.alert.action_paused") if pausing
                                else t("traffic.alert.action_warn") if percent == 100 else ""),
                    ))
        if self._config.on_budget == "pause":
            for reason in over:
                if await control.set_over_budget(reason):
                    break
        if control.over_budget and (control.over_budget not in over
                                    or self._config.on_budget != "pause"):
            await control.clear_over_budget()
        cap = self._config.direct_daily_gb
        control.set_direct_over(
            cap > 0 and self._spend.direct_day_bytes >= cap * self._config.bytes_per_gb)

    # ----------------------------------------------------------------- alerts

    async def _alerts(self, step: Step, proxied: list[tuple[Delta, float]],
                      now: datetime) -> None:
        day = self._spend.day_key
        limit = self._config.conn_alert_mb * MB
        for delta in step.deltas:
            info = delta.info
            if info.leak and await self.control.claim_alert(f"leak:{day}:{info.host}", day):
                await self._send(t("traffic.alert.leak", host=html.escape(info.host),
                                   category=t(f"traffic.cat.{info.category}"),
                                   node=html.escape(short_node(info.node))))
            if (limit > 0 and info.outbound == "proxy" and delta.total >= limit
                    and await self.control.claim_alert(f"conn:{delta.conn_id}", day)):
                await self._send(t("traffic.alert.conn", size=fmt_bytes(delta.total),
                                   host=html.escape(info.host),
                                   category=t(f"traffic.cat.{info.category}"),
                                   node=html.escape(short_node(info.node))))
        threshold = self._config.spike_mbps * MB
        if threshold <= 0:
            return
        stamp = now.timestamp()
        rate = sum(size for _, size in self._window) / SPIKE_WINDOW
        if rate <= threshold:
            self._spike_since = None
            self._spike_fired = False
            return
        if self._spike_since is None:
            self._spike_since = stamp
        if stamp - self._spike_since >= SPIKE_SUSTAIN and not self._spike_fired:
            self._spike_fired = True
            host, category, node = self._top
            await self._send(t("traffic.alert.spike", rate=f"{rate / MB:.1f}",
                               limit=f"{self._config.spike_mbps:g}", host=html.escape(host),
                               category=t(f"traffic.cat.{category}") if category else "",
                               node=html.escape(short_node(node))))

    async def _send(self, text: str) -> None:
        log.info("traffic alert: %s", text.replace("\n", " "))
        if self._notify is None:
            return
        try:
            await self._notify(text)
        except Exception:
            log.warning("could not deliver a traffic alert", exc_info=True)

    # ----------------------------------------------------------------- report

    def budgets(self) -> Budgets:
        c = self._config
        return Budgets(c.budget_daily_cny, c.budget_monthly_cny, c.budget_daily_proxy_gb)

    async def report(self, period: str = "today") -> Report:
        """Build the report from what is stored (flushing what is pending first)."""
        try:
            await self.flush()
        except Exception:
            log.warning("could not flush before reporting", exc_info=True)
        chain: list[str] = []
        if self._config.enabled:
            try:
                chain = await asyncio.to_thread(self._client.current_exit, TG_GROUP)
            except Exception:  # noqa: BLE001 - the exit line is optional
                chain = []
        control = self.control
        return await asyncio.to_thread(
            build_report, self._store, period, self._clock(), self._tz,
            budgets=self.budgets(), gate=control.state,
            gate_reason=control.over_budget or "",
            media_rate=control.rate_mbps("media"), upload_rate=control.rate_mbps("upload"),
            exit_chain=chain,
        )

    async def render(self, period: str = "today") -> str:
        return render(await self.report(period))

    async def maybe_report(self, now: datetime) -> bool:
        """The daily summary of yesterday, once, at ``TRAFFIC_DAILY_REPORT_AT``."""
        at = (self._config.daily_report_at or "").strip()
        if not at:
            return False
        try:
            hour, minute = (int(part) for part in at.split(":", 1))
        except ValueError:
            return False
        local = now.astimezone(self._tz)
        today = local.date().isoformat()
        if self.control.last_report_day == today or (local.hour, local.minute) < (hour, minute):
            return False
        # Once, even if the send fails: a summary that repeats is worse than one that is missed.
        await self.control.mark_reported(today)
        yesterday = (local.date() - timedelta(days=1)).isoformat()
        await self.flush()
        report = await asyncio.to_thread(
            build_report, self._store, "yesterday", now, self._tz, budgets=self.budgets(),
            gate=self.control.state, gate_reason=self.control.over_budget or "",
            media_rate=self.control.rate_mbps("media"),
            upload_rate=self.control.rate_mbps("upload"),
        )
        if report.proxy_bytes == 0 and not self.control.alert_days.get(yesterday):
            return False
        await self._send(t("traffic.daily.head") + "\n" + render(report))
        return True
