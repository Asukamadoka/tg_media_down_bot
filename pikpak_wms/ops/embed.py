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

from ..config import Config, config_path, load_config, rules_path
from ..core.client import Provider
from ..core.errors import AuthError, WmsError
from ..core.models import ActionType
from ..i18n import set_language, t
from ..nl.query import Clarification, Query
from ..rules.schema import Rule
from ..rules.units import human_size
from . import nl, organize, outbound, plans, protect, tidy
from .context import Context, open_context
from .runs import Run, Runs

log = logging.getLogger(__name__)

__all__ = [
    "AccountUnavailable", "Clarification", "EmbeddedWms", "Query", "Rule", "Run", "WmsError",
    "run_command", "set_language",
]

ProviderFactory = Callable[[], Provider]
"""Builds a provider. Called once per event loop: a PikPak client must not be
shared between the loops the command line opens one after another."""


class AccountUnavailable(AuthError):
    """The bot has no PikPak account WMS could use."""

    def __init__(self, detail: str, *, shown: str | None = None) -> None:
        # ``detail`` is for the log; ``shown`` is the same reason in the reader's language.
        super().__init__(f"no PikPak account for WMS: {detail}", key="error.no_account",
                         detail=shown or detail)


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
        self._translator = None
        self.runs: Runs | None = None
        self.interrupted: list[dict[str, Any]] = []
        """Plans a restart cut off, found at start (docs/wms/M8.3 §G5)."""

    async def start(self) -> list[str]:
        """Open the index and start the scheduler; returns the job names scheduled."""
        from ..scheduler.runner import WmsScheduler  # the scheduler sits above ops

        self.ctx = await open_context(self.config, self.provider)
        self._scheduler = WmsScheduler(self.ctx, on_result=self.on_result)
        self.runs = Runs(self.ctx, self._scheduler.lock)
        self.interrupted = await self.runs.recover()
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
        job = next((j for j in self.config.schedule.effective_jobs() if j.name == name), None)
        job = job or ScheduledJob(name=name, cron="0 0 * * *")
        if apply is not None:
            job = job.model_copy(update={"apply": apply})
        return await self._scheduler.run(job, notify=False)

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

    async def _labelled(self, row: dict[str, Any]) -> dict[str, Any]:
        summary = _plan_summary(row)
        state = await self.runs.label(row["id"]) if self.runs is not None else ""
        if state:
            summary["state"] = state
            summary["status_text"] = t(f"plan.status.{state}")
        return summary

    async def open_plans(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = await plans.listing(self._live, open_only=True, limit=limit)
        return [await self._labelled(row) for row in rows]

    async def plan_lines(self, plan_id: int, *, limit: int = 60) -> list[str]:
        row = await plans.get(self._live, plan_id)
        lines = plans.plan_lines(row["plan"], plan_id=plan_id, limit=limit)
        state = (await self.runs.label(plan_id) if self.runs is not None else "") or row["status"]
        lines.append(t("cli.plan.status", status=t(f"plan.status.{state}"),
                       progress=row["progress"], total=len(row["plan"])))
        return lines

    async def apply(self, plan_id: int, *, limit: int | None = None) -> plans.ApplyReport:
        """Apply a stored plan. Never with permanent deletion: a plan holding
        any is refused here (rule 2); that needs the command line."""
        ctx = self._live
        if self.runs is not None and (run := self.runs.get(plan_id)) and run.active:
            raise WmsError(f"plan {plan_id} is already running", key="plan.running", id=plan_id)
        async with self._scheduler.lock:
            row = await plans.get(ctx, plan_id)
            deliver = None
            if any(a.type is ActionType.OUTBOUND for a in row["plan"].actions):
                deliver = outbound.make_deliver(ctx)
            return await plans.apply(ctx, plan_id, limit=limit, deliver=deliver)

    async def start_apply(self, plan_id: int, *, limit: int | None = None) -> Run:
        """Confirming a plan: start it in the background and return at once
        (docs/wms/M8.3 §G). Watch the returned :class:`Run`; stop it with
        :meth:`stop_apply`. A plan already running is refused (``plan.running``)."""
        ctx = self._live
        assert self.runs is not None
        return await self.runs.start(
            plan_id, limit=limit,
            make_deliver=lambda progress: outbound.make_deliver(ctx, progress=progress))

    async def stop_apply(self, plan_id: int) -> Run | None:
        return await self.runs.stop(plan_id) if self.runs is not None else None

    def run_of(self, plan_id: int) -> Run | None:
        return self.runs.get(plan_id) if self.runs is not None else None

    async def discard(self, plan_id: int) -> None:
        if self.runs is not None and (run := self.runs.get(plan_id)) and run.active:
            raise WmsError(f"plan {plan_id} is running", key="plan.running", id=plan_id)
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

    # ------------------------------------------------- M7: tidying the drive

    async def plan_overview(self, plan_ids: list[int]) -> list[dict[str, Any]]:
        """One summary per plan, for a message that lists a batch of them."""
        out = []
        for plan_id in plan_ids:
            row = await plans.get(self._live, plan_id)
            out.append(await self._labelled(row))
        return out

    async def organize_scope(self, scope: str) -> Any:
        """organize-tree for one top-level folder, planned only (/wms organize tree /A)."""
        from .eventsync import refresh_index
        from .jobs import JobResult

        ctx = self._live
        async with self._scheduler.lock:
            await refresh_index(ctx, allow_full=False)
            planned = await tidy.organize_tree(ctx, scope=scope)
            ids = [pid for pid in [await plans.save(ctx, p) for p in planned] if pid]
        result = JobResult(tidy.TREE, t("job.nothing", name=tidy.TREE),
                           plan_id=ids[0] if ids else None, plan_ids=ids)
        return result

    async def protect(self, verb: str, path: str = "") -> dict[str, Any]:
        """``ls`` / ``add`` / ``rm`` on the whitelist; returns the listing after."""
        ctx = self._live
        async with self._scheduler.lock:
            if verb == "add":
                await protect.add(ctx, path)
            elif verb == "rm":
                await protect.remove(ctx, path)
            return await protect.listing(ctx)

    async def trash_item(self, file_id: str) -> tuple[str, plans.ApplyReport] | None:
        """The big-files report's [trash] button: one audited, undoable trash,
        through the same pipeline (so the whitelist applies). Returns the path
        and the report; None when the item is gone or protected."""
        ctx = self._live
        async with self._scheduler.lock:
            node = await ctx.store.node(file_id)
            if node is None:
                return None
            plan_id = await plans.save(ctx, tidy.trash_plan(node))
            if plan_id is None:
                return None
            return node.path, await plans.apply(ctx, plan_id)

    # ------------------------------------------- natural language (M6)

    def _nl(self):
        if self._translator is None:
            self._translator = nl.from_environment()
        return self._translator

    async def understand(self, text: str) -> Any:
        """A sentence → Query, Clarification, or None (not understood)."""
        return await nl.understand(self._live, text, translator=self._nl())

    @property
    def last_translator(self) -> str:
        """Who understood the last sentence, for a person (docs/wms/M8.3 §I): the rules,
        or a local model with the name of the machine it runs on."""
        return getattr(self._nl(), "last_label", "") or t("nl.by.rules")

    async def propose(self, query: Any) -> Any:
        async with self._scheduler.lock:
            proposal = await nl.make_proposal(self._live, query)
        proposal.translator = self.last_translator
        return proposal

    def proposal_lines(self, proposal: Any, *, limit: int = 8) -> list[str]:
        return nl.proposal_lines(proposal, limit=limit)

    def clarification_text(self, result: Any) -> str:
        return nl.clarification_text(result)

    # A proposal's buttons must outlive a restart, so what they act on is kept
    # in the database (meta ``nl:<id>``), not in the bot's memory (docs/wms/M8.3 §G6).

    PROPOSALS_KEPT = 200

    @staticmethod
    def rules_to_json(rules: list[Any]) -> list[dict[str, Any]]:
        """Rules as plain data (a step is written ``{op: spec}``, as in the rules file)."""
        out = []
        for rule in rules:
            data = rule.model_dump(mode="json", exclude={"actions"})
            data["actions"] = [
                {step.op: step.spec.model_dump(mode="json") if hasattr(step.spec, "model_dump")
                 else (step.spec or {})}
                for step in rule.actions
            ]
            out.append(data)
        return out

    @staticmethod
    def rules_from_json(items: list[dict[str, Any]]) -> list[Rule]:
        return [Rule.model_validate(item) for item in items]

    async def save_proposal(self, data: dict[str, Any], pid: int | None = None) -> int:
        """Store (or replace) a proposal's record; returns its id."""
        import json

        store = self._live.store
        if pid is None:
            pid = int(await store.get_meta("nl_next") or 0) + 1
            await store.set_meta("nl_next", str(pid))
            await store.delete_meta(f"nl:{pid - self.PROPOSALS_KEPT}")
        await store.set_meta(f"nl:{pid}", json.dumps(data, ensure_ascii=False))
        return pid

    async def load_proposal(self, pid: int) -> dict[str, Any] | None:
        import json

        raw = await self._live.store.get_meta(f"nl:{pid}")
        return json.loads(raw) if raw else None

    async def drop_proposal(self, pid: int) -> None:
        await self._live.store.delete_meta(f"nl:{pid}")

    async def add_rules(self, rules: list[Any], *, sentence: str) -> str:
        async with self._scheduler.lock:
            path = nl.add_rules(self._live, rules, sentence=sentence)
        self._scheduler.reload_rules()
        return str(path)

    @property
    def rules_file(self) -> str:
        return str(self.config.rules_file or rules_path())

    def rules(self) -> list[dict[str, Any]]:
        """The rules file, validated (raises RulesError, a WmsError, if broken)."""
        return [
            {"name": rule.name, "stage": rule.stage, "enabled": rule.enabled,
             "scope": rule.scope, "actions": [step.op for step in rule.actions]}
            for rule in organize.load_rules_for(self.config).rules
        ]

    async def stop(self) -> None:
        if self.runs is not None:
            await self.runs.stop_all()
            self.runs = None
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
