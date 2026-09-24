"""The ``wms`` command line: ``python -m pikpak_wms`` (``wms`` inside the image).

Only argument parsing and display live here; every command calls ``ops``
(rule 6). Output text goes through :func:`pikpak_wms.i18n.t`; command names
and flags stay English whatever the language.
"""

from __future__ import annotations

import asyncio
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
from ..i18n import t
from ..ops import listing
from ..ops import stocktake as stocktake_ops
from ..ops.context import Context, open_context

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
        console.print(t("cli.error", error=exc), style="red")
        raise typer.Exit(code=1) from exc


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"  # pragma: no cover


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
        console.print(t("cli.error", error=exc), style="red")
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
