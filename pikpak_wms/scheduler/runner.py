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
from ..core.errors import WmsError
from ..ops.context import Context, open_context
from ..ops.jobs import JobResult, run_job, run_rule
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
        self.lock = asyncio.Lock()
        """Held by every job, and by anything else that writes (the panel, /wms)."""
        self._scheduler: AsyncIOScheduler | None = None

    def jobs(self) -> list[ScheduledJob]:
        return [job for job in self.ctx.config.schedule.jobs if job.enabled]

    def scheduled_rules(self) -> list[tuple[str, str]]:
        """``[(rule name, cron), ...]`` for enabled rules that carry a schedule."""
        from ..ops import organize

        try:
            ruleset = organize.load(self.ctx)
        except WmsError:
            return []  # no rules file yet, or a broken one: the jobs still run
        return [(rule.name, rule.schedule.cron) for rule in ruleset.rules
                if rule.enabled and rule.schedule is not None]

    def reload_rules(self) -> list[str]:
        """Re-read rule schedules (after a rule was added); returns the rule names."""
        if self._scheduler is None:
            return []
        tz = self.ctx.config.schedule.tz
        for job in self._scheduler.get_jobs():
            if job.id.startswith("rule:"):
                job.remove()
        names = []
        for name, cron in self.scheduled_rules():
            self._scheduler.add_job(
                self.run_rule, CronTrigger.from_crontab(cron, timezone=tz), args=[name],
                id=f"rule:{name}", name=f"rule:{name}", max_instances=1, coalesce=True,
                misfire_grace_time=300,
            )
            names.append(name)
        return names

    async def run_rule(self, name: str) -> JobResult | None:
        async with self.lock:
            try:
                result = await run_rule(self.ctx, name, deliver=self.deliver)
            except Exception:
                log.exception("WMS rule %s failed", name)
                return None
        if self.on_result is not None:
            try:
                await self.on_result(result)
            except Exception:
                log.exception("WMS rule %s: reporting the result failed", name)
        return result

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
        for name in self.reload_rules():
            log.info("WMS rule %s scheduled", name)

    def scheduled(self) -> list[tuple[str, str]]:
        """``[(job name, time zone), ...]`` of what is scheduled, for status views."""
        if self._scheduler is None:
            return []
        return [(job.id, str(job.trigger.timezone)) for job in self._scheduler.get_jobs()]

    async def run(self, job: ScheduledJob, *, notify: bool = True) -> JobResult | None:
        """Run one job behind the lock. ``notify=False`` for runs someone asked
        for (they report to that person), so only the schedule's own runs are
        announced through ``on_result``."""
        async with self.lock:
            try:
                result = await run_job(self.ctx, job, deliver=self.deliver)
            except Exception:
                # A failing job must not take the scheduler (or the bot) down.
                log.exception("WMS job %s failed", job.name)
                return None
        if notify and self.on_result is not None:
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
