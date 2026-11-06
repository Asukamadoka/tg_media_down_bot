"""Rules + the local index → a :class:`Plan`. Nothing here touches PikPak.

Order and precedence, so a plan is predictable:

* Rules run in file order. A file (or folder) is handled by the **first**
  rule that matches it; later rules do not see it.
* When a rule matches a folder, nothing inside that folder is matched
  separately: whatever happens to the folder carries its contents.
* A rule's steps run step by step across all its files (every rename, then
  every move), so moves to one folder sit together and go out as one batch
  request. Each file still gets its steps in the order written.
* A step that cannot be taken (the target name is taken, a template field
  is missing) drops that file from the rest of the rule and adds a note to
  the plan instead of guessing.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from ..core.models import ActionType, Plan
from ..store.db import Store
from .actions import PRIMITIVES, STEP_TYPES, Conflict, Draft, Planner
from .matcher import Matcher
from .schema import Rule
from .template import TemplateError


def _under(path: str, folders: list[str]) -> bool:
    return any(path.startswith(folder + "/") for folder in folders)


async def evaluate(
    rules: list[Rule],
    store: Store,
    *,
    now: datetime,
    tz: tzinfo,
    source: str,
    planner: Planner | None = None,
) -> Plan:
    plan = Plan(source=source, generated_at=now.isoformat(timespec="seconds"))
    planner = planner or Planner(store, now=now, tz=tz)
    handled: set[str] = set()
    handled_folders: list[str] = []

    for rule in rules:
        everything = await store.nodes_under(rule.scope)
        nonempty = {node.parent_id for node in everything}
        depth = rule.scope.count("/") if rule.scope != "/" else 0
        candidates = (
            everything
            if rule.recursive
            else [n for n in everything if n.path.count("/") == depth + 1]
        )
        matcher = Matcher(rule.match)
        drafts: list[Draft] = []
        matched_folders: list[str] = []
        # Paths sort parents before children, so a folder is seen first.
        for node in candidates:
            if node.file_id in handled or _under(node.path, handled_folders + matched_folders):
                continue
            captures = matcher.test(node, now=now, tz=tz, nonempty=nonempty)
            if captures is None:
                continue
            drafts.append(Draft(node=node, captures=captures))
            if node.is_folder:
                matched_folders.append(node.path)
        handled.update(d.node.file_id for d in drafts)
        handled_folders.extend(matched_folders)

        dropped: set[str] = set()
        for step in rule.actions:
            primitive = PRIMITIVES[STEP_TYPES[step.op]]
            folders, others = [], []
            for draft in drafts:
                if draft.gone or draft.node.file_id in dropped:
                    continue
                try:
                    actions = await primitive.plan(step.spec, draft, planner, rule.name)
                except Conflict as conflict:
                    dropped.add(draft.node.file_id)
                    plan.note(conflict.key, rule=rule.name, file=draft.node.path,
                              **conflict.kwargs)
                    continue
                except TemplateError as error:
                    dropped.add(draft.node.file_id)
                    plan.note("conflict.template", rule=rule.name, file=draft.node.path,
                              error=str(error))
                    continue
                for action in actions:
                    (folders if action.type is ActionType.CREATE_FOLDER else others).append(
                        action
                    )
            # Group moves and copies by destination; sorted() is stable, so
            # each file's own order is kept.
            others.sort(key=lambda a: str(a.after.get("parent_path", "")))
            plan.actions.extend(folders + others)
        if drafts:
            plan.note("plan.rule_matched", rule=rule.name, count=len(drafts))
    return plan
