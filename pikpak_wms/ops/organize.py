"""Organize and clean up: run the rules over the index, and find duplicates.

``organize`` runs the rules with ``stage: organize`` (shelving, renaming,
classifying, archiving); ``cleanup`` runs those with ``stage: cleanup``
(retention). Both only produce a :class:`Plan`; applying it is
:mod:`pikpak_wms.ops.plans`.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from ..config import Config
from ..core.models import Action, ActionType, FileNode, Plan, normalize_path
from ..rules.engine import evaluate
from ..rules.schema import Rule, RuleSet, load_rules, rules_path
from ..rules.units import human_size
from . import protect
from .context import Context


def load_rules_for(config: Config) -> RuleSet:
    return load_rules(config.rules_file or rules_path())


def load(ctx: Context) -> RuleSet:
    return load_rules_for(ctx.config)


def now() -> datetime:
    return datetime.now(UTC)


def scopes(rules: list[Rule]) -> list[str]:
    """The smallest set of folders covering every rule, for a stocktake first."""
    paths = sorted({rule.scope for rule in rules})
    kept: list[str] = []
    for path in paths:
        if not any(path == k or path.startswith(k.rstrip("/") + "/") for k in kept):
            kept.append(path)
    return kept


async def plan_rules(
    ctx: Context,
    rules: list[Rule],
    *,
    source: str,
    at: datetime | None = None,
) -> Plan:
    return await evaluate(
        rules, ctx.store, now=at or now(), tz=ctx.config.schedule.tz, source=source
    )


async def organize(
    ctx: Context,
    ruleset: RuleSet,
    *,
    names: list[str] | None = None,
    at: datetime | None = None,
) -> Plan:
    rules = ruleset.select(stage=None if names else "organize", names=names)
    return await plan_rules(ctx, rules, source="organize", at=at)


async def cleanup(
    ctx: Context,
    ruleset: RuleSet,
    *,
    forever: bool = False,
    at: datetime | None = None,
) -> Plan:
    """The cleanup rules' plan. ``forever`` turns their trash into permanent
    deletion; applying that still needs the config switch (rule 2)."""
    plan = await plan_rules(ctx, ruleset.select(stage="cleanup"), source="cleanup", at=at)
    if forever:
        for action in plan.actions:
            if action.type is ActionType.TRASH:
                action.type = ActionType.DELETE_FOREVER
        plan.source = "cleanup-forever"
    return plan


# ------------------------------------------------------------------ dedupe


def _keeper(group: list[FileNode], keep_under: list[str]) -> FileNode:
    def rank(node: FileNode) -> tuple:
        preferred = any(
            node.path.startswith(normalize_path(k).rstrip("/") + "/") for k in keep_under
        )
        return (
            not preferred,
            node.created_time or "9999",
            len(node.path),
            node.path,
        )

    return min(group, key=rank)


async def dedupe(
    ctx: Context, *, scope: str = "/", keep_under: list[str] | None = None
) -> Plan:
    """Files with the same content hash: keep one, send the rest to the trash.

    Which one stays: one under ``keep_under`` if any, else the oldest, else
    the shortest path (docs/wms/EXTRAS.md §1). Protected copies (M7 §1) all
    stay, and one of them is the keeper, so a group with a protected copy
    loses only its unprotected ones.
    """
    keep_under = keep_under or []
    protection = await protect.load(ctx)
    plan = Plan(source="dedupe", generated_at=now().isoformat(timespec="seconds"))
    groups: dict[str, list[FileNode]] = defaultdict(list)
    for node in await ctx.store.nodes_under(scope):
        if not node.is_folder and node.hash:
            groups[node.hash].append(node)

    saved = 0
    for digest, group in sorted(groups.items(), key=lambda item: item[1][0].path):
        if len(group) < 2:
            continue
        if len({node.size for node in group}) > 1:
            plan.note("dedupe.size_mismatch", hash=digest[:12],
                      paths=", ".join(n.path for n in group))
            continue
        guarded = [node for node in group if protection.covers(node.path)]
        keep = _keeper(guarded or group, keep_under)
        drop = [node for node in group
                if node.file_id != keep.file_id and not protection.covers(node.path)]
        if not drop:
            continue
        for node in drop:
            plan.actions.append(
                Action(ActionType.TRASH, node.file_id, before=node.snapshot(),
                       after={"duplicate_of": keep.path}, rule_name="dedupe")
            )
        saved += sum(node.size for node in drop)
        plan.note("dedupe.group", keep=keep.path, count=len(drop),
                  size=human_size(sum(node.size for node in drop)))
    if plan.actions:
        plan.note("dedupe.total", size=human_size(saved))
    return plan


# ------------------------------------------------------------------ layout


async def layout(ctx: Context) -> Plan:
    """create_folder for every folder of ``layout.ensure`` the index lacks.

    The drive-wide 其他 of organize-tree (``tidy.loose.other``, M7.1 A2) is
    always part of it, when it is a path rather than a per-folder name.
    """
    from . import tidy  # tidy imports this module; only needed here

    plan = Plan(source="layout", generated_at=now().isoformat(timespec="seconds"))
    ensure = list(ctx.config.layout.ensure)
    other = tidy.load_spec(ctx).loose.other
    if other.startswith("/") and other not in ensure:
        ensure.append(other)
    for path in ensure:
        node = await ctx.store.node_at(path)
        if node is None or not node.is_folder:
            plan.actions.append(
                Action(ActionType.CREATE_FOLDER, "", after={"path": normalize_path(path)},
                       rule_name="layout")
            )
    return plan
