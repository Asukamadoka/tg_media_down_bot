"""The resident scheduler: ``wms run`` on its own, or inside the bot (M3).

APScheduler's asyncio scheduler, cron times in ``schedule.timezone``. One
lock runs jobs one at a time, so a slow stocktake and an organize never
interleave their requests or their index writes; a job that is still
running when its next time comes is skipped rather than stacked
(``max_instances=1``, ``coalesce``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config import Config, ScheduledJob
from ..core.client import Provider
from ..ops.context import Context, open_context
from ..ops.jobs import JobResult, run_job
from ..rules.actions import Deliver

log = logging.getLogger(__name__)

OnResult = Callable[[JobResult], Awaitable[None]]


class WmsScheduler:
    def __init__(
        self,
        ctx: Context,
        *,
        on_result: OnResult | None = None,
        deliver: Deliver | None = None,
    ) -> None:
        self.ctx = ctx
        self.on_result = on_result
        self.deliver = deliver
        self._lock = asyncio.Lock()
        self._scheduler: AsyncIOScheduler | None = None

    def jobs(self) -> list[ScheduledJob]:
        return [job for job in self.ctx.config.schedule.jobs if job.enabled]

    def start(self) -> None:
        tz = self.ctx.config.schedule.tz
        scheduler = AsyncIOScheduler(timezone=tz)
        for job in self.jobs():
            scheduler.add_job(
                self.run, CronTrigger.from_crontab(job.cron, timezone=tz), args=[job],
                id=job.name, name=job.name, max_instances=1, coalesce=True,
                misfire_grace_time=300,
            )
            log.info("WMS job %s scheduled: %s (%s)", job.name, job.cron,
                     "apply" if job.apply else "plan only")
        scheduler.start()
        self._scheduler = scheduler

    def scheduled(self) -> list[tuple[str, str]]:
        """``[(job name, time zone), ...]`` of what is scheduled, for status views."""
        if self._scheduler is None:
            return []
        return [(job.id, str(job.trigger.timezone)) for job in self._scheduler.get_jobs()]

    async def run(self, job: ScheduledJob) -> JobResult | None:
        async with self._lock:
            try:
                result = await run_job(self.ctx, job, deliver=self.deliver)
            except Exception:
                # A failing job must not take the scheduler (or the bot) down.
                log.exception("WMS job %s failed", job.name)
                return None
        if self.on_result is not None:
            try:
                await self.on_result(result)
            except Exception:
                log.exception("WMS job %s: reporting the result failed", job.name)
        return result

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None


async def serve(config: Config, provider: Provider) -> None:  # pragma: no cover - runs forever
    ctx = await open_context(config, provider)
    scheduler = WmsScheduler(ctx)
    scheduler.start()
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown()
        await ctx.close()
