"""The ``wms`` command line: ``python -m pikpak_wms`` (``wms`` inside the image).

Only argument parsing and display live here; every command calls ``ops``
(rule 6). Output text goes through :func:`pikpak_wms.i18n.t`; command names
and flags stay English whatever the language.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import typer
from rich.console import Console
from rich.table import Table

from .. import __version__
from ..config import Config, Credentials, config_path, load_config
from ..core.auth import StandaloneAuth, read_token
from ..core.errors import WmsError
from ..core.models import ActionType, Plan
from ..i18n import t
from ..ops import inbound as inbound_ops
from ..ops import listing, organize, plans
from ..ops import outbound as outbound_ops
from ..ops import stocktake as stocktake_ops
from ..ops.context import Context, open_context
from ..rules.schema import RulesError
from ..rules.units import human_size

T = TypeVar("T")

app = typer.Typer(
    name="wms",
    help=t("cli.help"),
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


class State:
    """What the root callback resolved, for every command."""

    config: Config
    config_file: Path
    provider_factory: Callable[[Config], Any] | None = None
    """Tests replace this; normally the standalone login."""


state = State()


@app.callback()
def _root(
    config: Path = typer.Option(None, "--config", "-c", help="config file (WMS_CONFIG)"),
) -> None:
    state.config_file = config or config_path()
    state.config = load_config(state.config_file)


def _provider(config: Config) -> Any:
    if state.provider_factory is not None:
        return state.provider_factory(config)
    return StandaloneAuth(config.store.token_path)


def _run(work: Callable[[Context], Awaitable[T]]) -> T:
    """Open the context, run one operation, close it, and report WmsError cleanly."""

    async def main() -> T:
        ctx = await open_context(state.config, _provider(state.config))
        try:
            return await work(ctx)
        finally:
            await ctx.close()

    try:
        return asyncio.run(main())
    except WmsError as exc:
        console.print(t("cli.error", error=exc.display()), style="red")
        raise typer.Exit(code=1) from exc


# ------------------------------------------------------------------ commands


@app.command()
def version() -> None:
    """Show the version."""
    console.print(t("cli.version", version=__version__))


@app.command()
def doctor() -> None:
    """Check configuration and credentials without touching the network."""
    cfg = state.config
    table = Table(title=t("cli.doctor.title"))
    table.add_column(t("cli.doctor.item"))
    table.add_column(t("cli.doctor.state"))

    config_state = (
        str(state.config_file) if state.config_file.exists() else t("cli.doctor.config_missing")
    )
    table.add_row(t("cli.doctor.config"), config_state)

    if Credentials.from_environment().usable:
        credentials = t("cli.doctor.credentials_env")
    elif read_token(cfg.store.token_path) is not None:
        credentials = t("cli.doctor.credentials_token")
    else:
        credentials = t("cli.doctor.credentials_none")
    table.add_row(t("cli.doctor.credentials"), credentials)
    table.add_row(
        t("cli.doctor.dry_run"),
        t("cli.doctor.on") if cfg.runtime.dry_run else t("cli.doctor.off"),
    )
    table.add_row(
        t("cli.doctor.forever"),
        t("cli.doctor.allowed") if cfg.runtime.allow_permanent_delete else t("cli.doctor.disabled"),
    )
    table.add_row(
        t("cli.doctor.rate"),
        t("cli.doctor.rate_value", rate=cfg.ratelimit.requests_per_second),
    )
    table.add_row(t("cli.doctor.database"), str(cfg.store.database_path))

    async def index(ctx: Context) -> tuple[int, str | None]:
        return await ctx.store.count_files(), await ctx.store.get_meta("last_stocktake")

    count, when = asyncio.run(_index_state(cfg, index))
    table.add_row(
        t("cli.doctor.index"),
        t("cli.doctor.index_value", count=count, when=when or t("cli.doctor.never")),
    )
    console.print(table)


async def _index_state(cfg: Config, work):
    # Reading the index needs no PikPak client at all.
    async def no_client():
        raise WmsError("doctor does not use the network")

    ctx = await open_context(cfg, no_client)
    try:
        return await work(ctx)
    finally:
        await ctx.close()


@app.command()
def login() -> None:
    """Log in from the environment and save the token (never the password)."""

    async def work() -> None:
        await StandaloneAuth(state.config.store.token_path).login()

    try:
        asyncio.run(work())
    except WmsError as exc:
        console.print(t("cli.error", error=exc.display()), style="red")
        raise typer.Exit(code=1) from exc
    console.print(t("cli.login.done", path=state.config.store.token_path))


@app.command()
def stocktake(
    full: bool = typer.Option(False, "--full", help="list every folder, skip nothing"),
    verify: bool = typer.Option(
        False, "--verify", help="compare the index with the drive, change nothing"
    ),
    root: list[str] = typer.Option(None, "--root", help="only this subtree (repeatable)"),
    as_json: bool = typer.Option(False, "--json", help="machine-readable output"),
) -> None:
    """Copy the drive's folder tree into the local index."""
    roots = root or state.config.stocktake.roots
    page_size = state.config.stocktake.page_size

    if verify:
        result = _run(lambda ctx: stocktake_ops.verify(ctx.client, ctx.store, roots=roots,
                                                       page_size=page_size))
        if as_json:
            console.print_json(json.dumps(result.__dict__, ensure_ascii=False))
        elif result.clean:
            console.print(t("cli.stocktake.verify_clean", summary=result.summary()))
        else:
            console.print(t("cli.stocktake.verify_dirty", summary=result.summary()))
            for label, paths in (
                (t("cli.stocktake.missing"), result.missing),
                (t("cli.stocktake.stale"), result.stale),
                (t("cli.stocktake.changed"), result.changed),
            ):
                for path in paths[:20]:
                    console.print(f"  {label}: {path}")
        if not result.clean:
            raise typer.Exit(code=1)
        return

    full = full or not state.config.stocktake.incremental
    report = _run(
        lambda ctx: stocktake_ops.stocktake(
            ctx.client, ctx.store, roots=roots, full=full, page_size=page_size
        )
    )
    if as_json:
        console.print_json(json.dumps(report.__dict__, ensure_ascii=False))
    else:
        console.print(report.summary())


@app.command(name="ls")
def list_path(path: str = typer.Argument("/", help="a path in the drive")) -> None:
    """List a folder from the local index (no network)."""
    nodes = _run(lambda ctx: listing.ls(ctx, path))
    if not nodes:
        console.print(t("cli.ls.empty", path=path))
        return
    table = Table(show_header=True)
    table.add_column(t("cli.ls.name"))
    table.add_column(t("cli.ls.size"), justify="right")
    table.add_column(t("cli.ls.modified"))
    for node in nodes:
        table.add_row(
            node.name + ("/" if node.is_folder else ""),
            t("cli.ls.folder") if node.is_folder else human_size(node.size),
            (node.modified_time or "")[:19].replace("T", " "),
        )
    console.print(table)


@app.command()
def quota() -> None:
    """Show storage used and available."""
    info = _run(listing.quota)
    percent = f"{info.used / info.limit * 100:.0f}" if info.limit else "0"
    console.print(
        t(
            "cli.quota.line",
            used=human_size(info.used),
            limit=human_size(info.limit),
            percent=percent,
            trash=human_size(info.in_trash),
        )
    )


# ------------------------------------------------------------------ M2: plans


def _wants_apply(flag: bool | None) -> bool:
    """``--apply`` / ``--dry-run`` when given, else the config (rule 1: dry run)."""
    return (not state.config.runtime.dry_run) if flag is None else flag


def _deliver_for(ctx: Context, plan: Plan, downloader: str | None = None):
    if any(a.type is ActionType.OUTBOUND for a in plan.actions):
        return outbound_ops.make_deliver(ctx, downloader=downloader)
    return None


def _confirm_forever(yes: bool) -> None:
    if not state.config.runtime.allow_permanent_delete:
        console.print(t("forever.refused"), style="red")
        raise typer.Exit(code=1)
    if not yes and not typer.confirm(t("cli.forever.confirm")):
        raise typer.Exit(code=1)


def _show_report(report: plans.ApplyReport | None) -> None:
    if report is None:
        return
    console.print(report.summary())
    for line in report.outputs:
        console.print(line, markup=False)
    for failure in report.failed[:20]:
        console.print(t("cli.apply.failed", path=failure["path"], error=failure["error"]),
                      style="red", markup=False)
    if report.stopped:
        console.print(t("cli.apply.stopped", reason=report.stopped), style="yellow")


def _plan_command(
    build: Callable[[Context], Awaitable[Plan]],
    *,
    apply_now: bool,
    limit: int | None,
    as_json: bool,
    allow_forever: bool = False,
    downloader: str | None = None,
) -> None:
    async def work(ctx: Context):
        plan = await build(ctx)
        plan_id = await plans.save(ctx, plan)
        report = None
        if apply_now and plan_id is not None:
            report = await plans.apply(ctx, plan_id, limit=limit, allow_forever=allow_forever,
                                       deliver=_deliver_for(ctx, plan, downloader))
        return plan, plan_id, report

    plan, plan_id, report = _run(work)
    if as_json:
        console.print_json(json.dumps(
            {"id": plan_id, "plan": plan.to_dict(),
             "report": report.to_result() if report else None},
            ensure_ascii=False,
        ))
        return
    for line in plans.plan_lines(plan, plan_id=plan_id):
        console.print(line, markup=False)
    if plan_id is not None and not apply_now:
        console.print(t("cli.plan.dry_run_hint", id=plan_id), style="cyan")
    _show_report(report)


def _ruleset():
    try:
        return organize.load_rules_for(state.config)
    except RulesError as exc:
        console.print(t("cli.error", error=exc.display()), style="red")
        raise typer.Exit(code=1) from exc


@app.command()
def rules(
    check: bool = typer.Option(False, "--check", help="only validate, print nothing else"),
) -> None:
    """List the rules file (and validate it)."""
    ruleset = _ruleset()
    if check:
        console.print(t("cli.rules.valid", count=len(ruleset.rules)))
        return
    table = Table(show_header=True)
    for column in ("cli.rules.name", "cli.rules.stage", "cli.rules.scope", "cli.rules.actions",
                   "cli.rules.enabled"):
        table.add_column(t(column))
    for rule in ruleset.rules:
        table.add_row(
            rule.name, rule.stage, rule.scope, ", ".join(step.op for step in rule.actions),
            t("cli.doctor.on") if rule.enabled else t("cli.doctor.off"),
        )
    console.print(table)


ApplyFlag = typer.Option(None, "--apply/--dry-run", help="apply now, or only plan (default: plan)")
LimitFlag = typer.Option(None, "--limit", help="apply at most N actions this run")
JsonFlag = typer.Option(False, "--json", help="machine-readable output")


@app.command(name="organize")
def organize_command(
    rule: list[str] = typer.Option(None, "--rule", help="only this rule (repeatable)"),
    dedupe: bool = typer.Option(False, "--dedupe", help="find duplicates by hash instead"),
    scope: str = typer.Option("/", "--scope", help="with --dedupe: where to look"),
    keep_under: list[str] = typer.Option(None, "--keep-under",
                                         help="with --dedupe: prefer copies under this folder"),
    apply_flag: bool | None = ApplyFlag,
    limit: int | None = LimitFlag,
    as_json: bool = JsonFlag,
) -> None:
    """Plan (and with --apply, carry out) the organize rules."""
    if dedupe:
        def build(ctx):
            return organize.dedupe(ctx, scope=scope, keep_under=keep_under or [])
    else:
        ruleset = _ruleset()

        def build(ctx):
            return organize.organize(ctx, ruleset, names=rule or None)

    _plan_command(build, apply_now=_wants_apply(apply_flag), limit=limit, as_json=as_json)


@app.command()
def cleanup(
    forever: bool = typer.Option(False, "--forever",
                                 help="delete permanently (needs allow_permanent_delete)"),
    yes: bool = typer.Option(False, "--yes", help="do not ask again for --forever"),
    apply_flag: bool | None = ApplyFlag,
    limit: int | None = LimitFlag,
    as_json: bool = JsonFlag,
) -> None:
    """Plan (and with --apply, carry out) the cleanup rules. Trash only, by default."""
    apply_now = _wants_apply(apply_flag)
    if forever and apply_now:
        _confirm_forever(yes)
    ruleset = _ruleset()
    _plan_command(lambda ctx: organize.cleanup(ctx, ruleset, forever=forever),
                  apply_now=apply_now, limit=limit, as_json=as_json, allow_forever=forever)


@app.command()
def layout(apply_flag: bool | None = ApplyFlag, as_json: bool = JsonFlag) -> None:
    """Create the folders listed under layout.ensure."""
    _plan_command(organize.layout, apply_now=_wants_apply(apply_flag), limit=None,
                  as_json=as_json)


@app.command()
def outbound(
    paths: list[str] = typer.Argument(..., help="files or folders in the drive"),
    to: str = typer.Option("", "--to", help="sub-folder at the destination"),
    downloader: str = typer.Option(None, "--downloader", help="none | aria2 | local"),
    apply_flag: bool | None = ApplyFlag,
    limit: int | None = LimitFlag,
    as_json: bool = JsonFlag,
) -> None:
    """Take files out of the drive: links, aria2, or into the local folder."""
    _plan_command(lambda ctx: outbound_ops.plan_paths(ctx, paths, to=to),
                  apply_now=_wants_apply(apply_flag), limit=limit, as_json=as_json,
                  downloader=downloader)


@app.command()
def inbound(
    sources: list[str] = typer.Argument(None, help="magnet / URL / PikPak share links"),
    to: str = typer.Option(None, "--to", help="drive folder (default: layout.inbox)"),
    pass_code: str = typer.Option("", "--pass-code", help="share link password"),
    poll: bool = typer.Option(False, "--poll", help="update pending offline downloads"),
    apply_flag: bool | None = ApplyFlag,
) -> None:
    """Take links into the drive; the same link twice is taken once."""
    if poll:
        report = _run(inbound_ops.poll)
        console.print(t("job.polled", checked=report.checked, finished=report.finished,
                        failed=report.failed))
        return
    apply_now = _wants_apply(apply_flag)
    for source in sources or []:
        result = _run(lambda ctx, source=source: inbound_ops.inbound(
            ctx, source, to=to, pass_code=pass_code, apply_now=apply_now))
        if result.existing is not None:
            console.print(t("cli.inbound.existing", source=source,
                            phase=result.existing["phase"]), markup=False)
        elif result.applied:
            console.print(t("cli.inbound.done", source=source, kind=result.kind,
                            names=", ".join(result.names or []) or "-"), markup=False)
        else:
            console.print(t("cli.inbound.plan", source=source, kind=result.kind,
                            target=result.target), markup=False)
    if sources and not apply_now:
        console.print(t("cli.inbound.hint"), style="cyan")


@app.command(name="plans")
def list_plans(all_plans: bool = typer.Option(False, "--all", help="closed ones too")) -> None:
    """Plans waiting for confirmation (or every plan)."""
    rows = _run(lambda ctx: plans.listing(ctx, open_only=not all_plans))
    if not rows:
        console.print(t("cli.plans.none"))
        return
    table = Table(show_header=True)
    for column in ("cli.plans.id", "cli.plans.source", "cli.plans.status", "cli.plans.actions",
                   "cli.plans.created"):
        table.add_column(t(column))
    for row in rows:
        table.add_row(str(row["id"]), row["source"], t(f"plan.status.{row['status']}"),
                      f"{row['progress']}/{len(row['plan'])}",
                      row["created_at"][:19].replace("T", " "))
    console.print(table)


@app.command(name="plan")
def show_plan(
    plan_id: int = typer.Argument(..., help="plan id"),
    limit: int = typer.Option(200, "--show", help="actions to list"),
) -> None:
    """Show one stored plan."""
    row = _run(lambda ctx: plans.get(ctx, plan_id))
    for line in plans.plan_lines(row["plan"], plan_id=plan_id, limit=limit):
        console.print(line, markup=False)
    console.print(t("cli.plan.status", status=t(f"plan.status.{row['status']}"),
                    progress=row["progress"], total=len(row["plan"])))


@app.command(name="apply")
def apply_plan(
    plan_id: int = typer.Argument(..., help="plan id"),
    limit: int | None = LimitFlag,
    forever: bool = typer.Option(False, "--forever", help="allow permanent deletions in it"),
    yes: bool = typer.Option(False, "--yes", help="do not ask again for --forever"),
    downloader: str = typer.Option(None, "--downloader", help="for outbound: none|aria2|local"),
) -> None:
    """Carry out a stored plan (or its next part)."""
    if forever:
        _confirm_forever(yes)

    async def work(ctx: Context):
        row = await plans.get(ctx, plan_id)
        return await plans.apply(ctx, plan_id, limit=limit, allow_forever=forever,
                                 deliver=_deliver_for(ctx, row["plan"], downloader))

    _show_report(_run(work))


@app.command()
def discard(plan_id: int = typer.Argument(..., help="plan id")) -> None:
    """Drop a stored plan without applying it."""
    _run(lambda ctx: plans.discard(ctx, plan_id))
    console.print(t("cli.plan.discarded", id=plan_id))


@app.command()
def audit(
    limit: int = typer.Option(30, "--limit", help="entries to show"),
    plan_id: int = typer.Option(None, "--plan", help="only this plan's entries"),
    as_json: bool = JsonFlag,
) -> None:
    """What was changed, newest first; every entry can be passed to undo."""

    async def work(ctx: Context):
        return await ctx.store.audit_entries(limit=limit, applied_only=True, plan_id=plan_id)

    entries = _run(work)
    if as_json:
        console.print_json(json.dumps(entries, ensure_ascii=False))
        return
    if not entries:
        console.print(t("cli.audit.none"))
        return
    table = Table(show_header=True)
    for column in ("cli.audit.id", "cli.audit.at", "cli.audit.what", "cli.audit.rule"):
        table.add_column(t(column))
    for entry in entries:
        what = plans.describe_entry(entry)
        if entry["undo_of"]:
            what = t("cli.audit.undo_of", id=entry["undo_of"]) + " " + what
        table.add_row(str(entry["id"]), entry["at"][:19].replace("T", " "), what,
                      entry["rule_name"])
    console.print(table)


@app.command()
def undo(
    audit_id: int = typer.Argument(..., help="audit entry id (see wms audit)"),
    apply_flag: bool | None = ApplyFlag,
) -> None:
    """Reverse one audit entry: rename, move, trash, star, create_folder."""
    apply_now = _wants_apply(apply_flag)
    outcome = _run(lambda ctx: plans.undo(ctx, audit_id, apply_now=apply_now))
    console.print(outcome.action.describe(), markup=False)
    if outcome.applied:
        console.print(t("cli.undo.done", id=audit_id, new=outcome.new_audit_id))
    else:
        console.print(t("cli.undo.hint", id=audit_id), style="cyan")


@app.command()
def run() -> None:
    """Run the scheduled jobs of schedule.jobs until stopped."""
    from ..scheduler.runner import serve

    jobs = [job for job in state.config.schedule.jobs if job.enabled]
    if not jobs:
        console.print(t("cli.run.no_jobs"))
        raise typer.Exit(code=1)
    console.print(t("cli.run.started", count=len(jobs), tz=state.config.schedule.timezone))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(state.config, _provider(state.config)))


@app.command()
def events(
    raw: bool = typer.Option(False, "--raw", help="print PikPak's answer untouched"),
    limit: int = typer.Option(20, "--limit", help="events to ask for"),
) -> None:
    """PikPak's recent-events feed, for designing event-based stocktake."""
    if not raw:
        console.print(t("cli.events.raw_only"))
        raise typer.Exit(code=1)
    data = _run(lambda ctx: listing.events_raw(ctx, limit=limit))
    console.print_json(json.dumps(data, ensure_ascii=False, default=str))


def main(argv: list[str] | None = None) -> int:
    try:
        app(args=argv if argv is not None else sys.argv[1:], standalone_mode=False)
    except typer.Exit as exc:
        return exc.exit_code
    except SystemExit as exc:  # typer/click usage errors
        return int(exc.code or 0)
    except Exception as exc:  # click's own parsing errors carry their exit code
        code = getattr(exc, "exit_code", None)
        if code is None:
            raise
        if hasattr(exc, "show"):
            exc.show()
        return int(code)
    return 0
