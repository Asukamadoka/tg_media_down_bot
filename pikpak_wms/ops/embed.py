"""The one door the bot uses to run WMS inside its own process (M3).

``tgmd`` may only reach WMS through ``pikpak_wms.ops`` (CC_BRIEF §5); this
module is that entry. It needs nothing from the bot except a *provider*: an
async callable returning a logged-in ``PikPakApi``. The bot passes one that
reuses the account the user connected, so WMS never asks for a password.

* :class:`EmbeddedWms` runs the scheduled jobs of the WMS config in the
  bot's event loop and stops with it.
* :func:`run_command` runs one ``wms`` command line with that provider;
  the image's ``wms`` shim goes through ``python -m tgmd.wms`` to get here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import Config, config_path, load_config
from ..core.client import Provider
from ..core.errors import AuthError, WmsError
from ..core.models import ActionType
from ..i18n import set_language, t
from ..rules.units import human_size
from . import outbound, plans
from .context import Context, open_context

log = logging.getLogger(__name__)

__all__ = [
    "AccountUnavailable", "EmbeddedWms", "WmsError", "run_command", "set_language",
]

ProviderFactory = Callable[[], Provider]
"""Builds a provider. Called once per event loop: a PikPak client must not be
shared between the loops the command line opens one after another."""


class AccountUnavailable(AuthError):
    """The bot has no PikPak account WMS could use."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"no PikPak account for WMS: {detail}", key="error.no_account",
                         detail=detail)


class EmbeddedWms:
    def __init__(
        self,
        provider: Provider,
        *,
        config: Config | None = None,
        on_result: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config or load_config()
        self.provider = provider
        self.on_result = on_result
        self.ctx: Context | None = None
        self._scheduler = None

    async def start(self) -> list[str]:
        """Open the index and start the scheduler; returns the job names scheduled."""
        from ..scheduler.runner import WmsScheduler  # the scheduler sits above ops

        self.ctx = await open_context(self.config, self.provider)
        self._scheduler = WmsScheduler(self.ctx, on_result=self.on_result)
        self._scheduler.start()
        names = self.scheduled()
        log.info(
            "WMS started: config %s, database %s, %d job(s) scheduled%s",
            config_path(), self.config.store.database_path, len(names),
            f" ({', '.join(names)})" if names else "",
        )
        return names

    def scheduled(self) -> list[str]:
        return [name for name, _tz in self._scheduler.scheduled()] if self._scheduler else []

    async def run_job(self, name: str, *, apply: bool | None = None) -> Any:
        """Run one job now, as configured (or ``apply`` overriding), behind the
        scheduler's lock. Returns its result, or None when it failed."""
        from ..config import ScheduledJob

        if self._scheduler is None:
            raise RuntimeError("EmbeddedWms.start() has not been awaited")
        job = next((j for j in self.config.schedule.jobs if j.name == name), None)
        job = job or ScheduledJob(name=name, cron="0 0 * * *")
        if apply is not None:
            job = job.model_copy(update={"apply": apply})
        return await self._scheduler.run(job)

    # ---------------------------------------------------- for the bot's views
    # The Mini App panel (M4) and /wms (M5) call these; every write takes the
    # scheduler's lock so it never interleaves with a scheduled job.

    @property
    def _live(self) -> Context:
        if self.ctx is None or self._scheduler is None:
            raise RuntimeError("EmbeddedWms.start() has not been awaited")
        return self.ctx

    async def status(self) -> dict[str, Any]:
        ctx = self._live
        return {
            "files": await ctx.store.count_files(),
            "last_stocktake": await ctx.store.get_meta("last_stocktake"),
            "open_plans": len(await plans.listing(ctx, open_only=True, limit=500)),
            "jobs": self.scheduled(),
            "timezone": self.config.schedule.timezone,
        }

    async def open_plans(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = await plans.listing(self._live, open_only=True, limit=limit)
        return [_plan_summary(row) for row in rows]

    async def plan_lines(self, plan_id: int, *, limit: int = 60) -> list[str]:
        row = await plans.get(self._live, plan_id)
        lines = plans.plan_lines(row["plan"], plan_id=plan_id, limit=limit)
        lines.append(t("cli.plan.status", status=t(f"plan.status.{row['status']}"),
                       progress=row["progress"], total=len(row["plan"])))
        return lines

    async def apply(self, plan_id: int, *, limit: int | None = None) -> plans.ApplyReport:
        """Apply a stored plan. Never with permanent deletion: a plan holding
        any is refused here (rule 2); that needs the command line."""
        ctx = self._live
        async with self._scheduler.lock:
            row = await plans.get(ctx, plan_id)
            deliver = None
            if any(a.type is ActionType.OUTBOUND for a in row["plan"].actions):
                deliver = outbound.make_deliver(ctx)
            return await plans.apply(ctx, plan_id, limit=limit, deliver=deliver)

    async def discard(self, plan_id: int) -> None:
        async with self._scheduler.lock:
            await plans.discard(self._live, plan_id)

    async def audit(self, *, limit: int = 20) -> list[dict[str, Any]]:
        entries = await self._live.store.audit_entries(limit=limit, applied_only=True)
        for entry in entries:
            entry["what"] = plans.describe_entry(entry)
        return entries

    async def undo(self, audit_id: int, *, apply_now: bool) -> plans.UndoOutcome:
        async with self._scheduler.lock:
            return await plans.undo(self._live, audit_id, apply_now=apply_now)

    async def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown()
            self._scheduler = None
        if self.ctx is not None:
            await self.ctx.close()
            self.ctx = None


def _plan_summary(row: dict[str, Any]) -> dict[str, Any]:
    plan = row["plan"]
    files, size = plans.plan_totals(plan)
    return {
        "id": row["id"],
        "source": row["source"],
        "status": row["status"],
        "status_text": t(f"plan.status.{row['status']}"),
        "progress": row["progress"],
        "actions": len(plan),
        "files": files,
        "size": human_size(size),
        "created_at": row["created_at"],
        "header": plans.plan_lines(plan, plan_id=row["id"], limit=0)[0],
    }


def run_command(argv: list[str], provider_factory: ProviderFactory) -> int:
    """Run ``wms <argv>`` with the bot's account instead of a standalone login."""
    from ..cli import main as cli  # the command line sits above ops

    cli.state.provider_factory = lambda _config: provider_factory()
    try:
        return cli.main(argv)
    finally:
        cli.state.provider_factory = None
