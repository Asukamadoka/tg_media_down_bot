"""Tidying the whole drive by the owner's own rules (docs/wms/M7 §2 to §5).

Three jobs, all planned over the local index and nothing else (rule 4):

``organize-tree``, one plan per top-level folder ``/<A>`` so each can be
confirmed on its own (§3.2 asks for batches):

1. **slim** inside the second-level folders: trash empty folders and junk
   files, and lift the contents of single-folder chains up one level;
2. **big**: a second-level folder of ``big.folder`` or more moves whole to
   ``<big.to>/<A>/``; a file of ``big.file`` or more (depth 3 and deeper,
   in a folder that stays) moves to ``<big.to>/<A>/``;
3. **loose files** directly in ``/<A>``: grouped by name into
   ``/<A>/<group>/``, other videos to ``/<A>/杂/``, images to ``/写真/杂/``,
   everything else to ``/<A>/其他/``.

``organize-inbox``: whatever lands in the entry folders (``/Telegram``,
``/Pack From Shared``) and names a top-level folder moves into it, then
joins the loose-file grouping there; the rest is grouped where it is.

``big-report``: the biggest files and folders and the big files nothing
changed for a long time. A report only; it deletes nothing.

Every plan still goes through :func:`pikpak_wms.ops.plans.save`, so the
whitelist (§1) is applied last, whatever is planned here. Protected
top-level folders are skipped from the start as well, which keeps plans small.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import rules_path
from ..core.models import (
    Action,
    ActionType,
    FileNode,
    Plan,
    join_path,
    normalize_path,
    parent_of,
    parse_time,
)
from ..rules.actions import Conflict, Planner
from ..rules.matcher import category_of
from ..rules.names import group_names, is_messy, name_key, stem_of
from ..rules.schema import TidySpec, load_rules
from ..rules.units import human_size
from . import protect
from .context import Context

TREE, INBOX = "organize-tree", "organize-inbox"
TREE_ADS, INBOX_ADS = "organize-tree-ads", "organize-inbox-ads"
"""Suspected ads get a plan of their own, confirmed on its own, never applied
by a job (M7.1 A4.2)."""


def is_ads_plan(source: str) -> bool:
    return source.split(":", 1)[0] in (TREE_ADS, INBOX_ADS)


def load_spec(ctx: Context) -> TidySpec:
    """The ``tidy:`` section of the rules file; defaults when there is no file."""
    path = ctx.config.rules_file or rules_path()
    return load_rules(path).tidy if path.exists() else TidySpec()


def depth(path: str) -> int:
    return 0 if normalize_path(path) == "/" else normalize_path(path).count("/")


def top_of(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    return "/" + parts[0] if parts else "/"


# ------------------------------------------------------------------ the tree


@dataclass
class Tree:
    """The index as a tree, with sizes and file counts per folder."""

    by_path: dict[str, FileNode]
    kids: dict[str, list[FileNode]]
    size: dict[str, int]
    files: dict[str, int]

    @classmethod
    def build(cls, nodes: list[FileNode]) -> Tree:
        by_path = {node.path: node for node in nodes}
        kids: dict[str, list[FileNode]] = defaultdict(list)
        size: dict[str, int] = defaultdict(int)
        files: dict[str, int] = defaultdict(int)
        for node in nodes:
            kids[parent_of(node.path)].append(node)
            if node.is_folder:
                continue
            level = parent_of(node.path)
            while True:
                size[level] += node.size
                files[level] += 1
                if level == "/":
                    break
                level = parent_of(level)
        for children in kids.values():
            children.sort(key=lambda n: n.name)
        return cls(by_path, kids, size, files)

    def folders(self, parent: str) -> list[FileNode]:
        return [n for n in self.kids.get(parent, []) if n.is_folder]

    def loose(self, parent: str) -> list[FileNode]:
        return [n for n in self.kids.get(parent, []) if not n.is_folder]

    def below(self, path: str) -> list[FileNode]:
        """Everything under ``path``, parents before children."""
        out: list[FileNode] = []
        stack = [path]
        while stack:
            current = stack.pop()
            children = self.kids.get(current, [])
            out.extend(children)
            stack.extend(reversed([c.path for c in children if c.is_folder]))
        return out


# ------------------------------------------------------------ the planning


@dataclass
class Tidier:
    """Plans moves and trashes without templates, and remembers what moved.

    Paths are the index's until something is planned: a folder moved or
    lifted is recorded in ``remaps``, so a later step on something inside it
    uses the path it will have by then.
    """

    ctx: Context
    spec: TidySpec
    planner: Planner
    protection: protect.Protection
    remaps: list[tuple[str, str]] = field(default_factory=list)
    """Folders moved or emptied upwards: (old prefix, new prefix), in order."""
    moved_files: dict[str, str] = field(default_factory=dict)
    gone: set[str] = field(default_factory=set)
    leaving: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    """Files planned to leave each folder (trashed, or moved out of it)."""

    def _leave(self, source: str, dest: str | None) -> None:
        folder = parent_of(source)
        while depth(folder) >= 2:
            if dest is None or not (dest == folder or dest.startswith(folder + "/")):
                self.leaving[folder] += 1
            folder = parent_of(folder)

    def where(self, path: str) -> str:
        for old, new in self.remaps:
            if path.startswith(old + "/"):
                path = new + path[len(old):]
            elif path == old:
                path = new
        return self.moved_files.get(path, path)

    def is_gone(self, path: str) -> bool:
        """``path`` or a folder above it is planned for the trash."""
        if not self.gone:
            return False
        while path != "/":
            if path in self.gone:
                return True
            path = parent_of(path)
        return False

    def current(self, node: FileNode) -> FileNode:
        path = self.where(node.path)
        return node if path == node.path else replace(node, path=path)

    async def move(self, node: FileNode, dest: str, rule: str,
                   extra: dict[str, Any] | None = None) -> list[Action]:
        node = self.current(node)
        dest = normalize_path(dest)
        if dest == parent_of(node.path):
            return []
        if dest == node.path or dest.startswith(node.path + "/"):
            raise Conflict("conflict.into_itself", path=node.path)
        if (await self.planner.occupant(dest) is not None
                and not await self.planner.folder_exists(dest)):
            raise Conflict("conflict.taken", path=dest)
        new_path = join_path(dest, node.name)
        occupant = await self.planner.occupant(new_path)
        if occupant not in (None, node.file_id):
            raise Conflict("conflict.taken", path=new_path)
        actions = await self.planner.need_folder(dest, rule)
        actions.append(Action(
            ActionType.MOVE, node.file_id, before=node.snapshot(),
            after={"path": new_path, "parent_path": dest, "create_missing": True,
                   **(extra or {})},
            rule_name=rule,
        ))
        self.planner.vacate(node.path)
        self.planner.claim(new_path, node.file_id)
        if node.is_folder:
            self.remaps.append((node.path, new_path))
        else:
            self.moved_files[node.path] = new_path
            self._leave(node.path, dest)
        return actions

    async def rename(self, node: FileNode, name: str, rule: str) -> tuple[list[Action], FileNode]:
        node = self.current(node)
        new_path = join_path(parent_of(node.path), name)
        occupant = await self.planner.occupant(new_path)
        if occupant not in (None, node.file_id):
            raise Conflict("conflict.taken", path=new_path)
        action = Action(ActionType.RENAME, node.file_id, before=node.snapshot(),
                        after={"name": name, "path": new_path}, rule_name=rule)
        self.planner.vacate(node.path)
        self.planner.claim(new_path, node.file_id)
        return [action], replace(node, name=name, path=new_path)

    def trash(self, node: FileNode, rule: str) -> list[Action]:
        node = self.current(node)
        self.planner.vacate(node.path)
        self.gone.add(node.path)
        if not node.is_folder:
            self._leave(node.path, None)
        return [Action(ActionType.TRASH, node.file_id, before=node.snapshot(), rule_name=rule)]

    async def attempt(self, plan: Plan, rule: str, node: FileNode, work) -> bool:
        """Run one planning step; a conflict becomes a note on ``plan``."""
        try:
            plan.actions.extend(await work())
        except Conflict as conflict:
            plan.note(conflict.key, rule=rule, file=node.path, **conflict.kwargs)
            return False
        return True


def _now() -> datetime:
    return datetime.now(UTC)


async def _tidier(ctx: Context, spec: TidySpec, protection: protect.Protection,
                  at: datetime | None) -> Tidier:
    now = at or _now()
    planner = Planner(ctx.store, now=now, tz=ctx.config.schedule.tz)
    return Tidier(ctx=ctx, spec=spec, planner=planner, protection=protection)


def _special(spec: TidySpec) -> set[str]:
    """Top-level folders organize-tree leaves alone (and inbox never shelves into)."""
    special = {top_of(spec.big.to), *(top_of(f) for f in spec.inbox.folders), *spec.skip}
    if spec.loose.other.startswith("/"):
        special.add(top_of(spec.loose.other))  # the drive-wide 其他 (M7.1 A2)
    return special


# ------------------------------------------------------------ §3.1 slimming


def _is_junk(node: FileNode, spec: TidySpec) -> bool:
    slim = spec.slim
    if node.extension.lower() in slim.junk_extensions:
        return True
    return (node.size < slim.junk_words_max_size
            and any(word and word in node.name for word in slim.junk_words))


async def _slim(td: Tidier, tree: Tree, top: str, plan: Plan, counts: dict[str, int]) -> None:
    """Inside the second-level folders only (M7.1 A1): a second-level folder
    itself is never trashed or moved here, so what it holds stays its own."""
    spec = td.spec.slim
    below = [n for n in tree.below(top) if depth(n.path) >= 2]
    if spec.empty_folders:
        for node in below:
            if (node.is_folder and depth(node.path) >= 3
                    and tree.files.get(node.path, 0) == 0
                    and not td.is_gone(node.path)
                    and not td.protection.covers(node.path)
                    and not td.protection.holds(node.path)):
                plan.actions.extend(td.trash(node, "tidy:empty"))
                counts["empty"] += 1
    for node in below:
        if node.is_folder or td.is_gone(node.path) or depth(node.path) < 3:
            continue
        if _is_junk(node, td.spec):
            plan.actions.extend(td.trash(node, "tidy:junk"))
            counts["junk"] += 1
    if not spec.flatten:
        return
    lifted: set[str] = set()
    for node in below:
        if not node.is_folder or td.is_gone(node.path) or _under_any(node.path, lifted):
            continue
        chain = _chain(tree, node)
        if chain is None:
            continue
        top_link, last = chain
        rule = "tidy:flatten"
        contents = [c for c in tree.kids.get(last.path, []) if not td.is_gone(c.path)]
        if any(c.name == top_link.name for c in contents):
            plan.note("conflict.taken", rule=rule, file=last.path,
                      path=join_path(node.path, top_link.name))
            continue
        moved: list[Action] = []
        try:
            for child in contents:
                moved.extend(await td.move(child, node.path, rule))
        except Conflict as conflict:
            plan.note(conflict.key, rule=rule, file=last.path, **conflict.kwargs)
            continue
        plan.actions.extend(moved)
        plan.actions.extend(td.trash(top_link, rule))
        td.remaps.append((last.path, node.path))  # what was in the chain's end is in node now
        lifted.add(node.path)
        counts["flatten"] += 1


def _under_any(path: str, folders: set[str]) -> bool:
    """``path`` lies strictly below one of ``folders``."""
    if not folders:
        return False
    path = parent_of(path)
    while path != "/":
        if path in folders:
            return True
        path = parent_of(path)
    return False


def _chain(tree: Tree, node: FileNode) -> tuple[FileNode, FileNode] | None:
    """For a folder holding exactly one folder and no files: (that folder,
    the deepest folder of the chain). None when there is no chain, or the
    chain holds no files at all (the empty-folder step takes that)."""
    if tree.files.get(node.path, 0) == 0:
        return None
    children = tree.kids.get(node.path, [])
    if len(children) != 1 or not children[0].is_folder:
        return None
    first = last = children[0]
    while True:
        inner = tree.kids.get(last.path, [])
        if len(inner) == 1 and inner[0].is_folder:
            last = inner[0]
            continue
        return first, last


# ---------------------------------------------------------------- §3.2 big


def _weight(td: Tidier, tree: Tree, folder: str) -> tuple[int, int]:
    """(total size, biggest file) of a folder after this plan's slimming:
    junk already planned for the trash does not count (M7.1 A1: slim first)."""
    total = biggest = 0
    for node in tree.below(folder):
        if node.is_folder or td.is_gone(td.where(node.path)):
            continue
        total += node.size
        biggest = max(biggest, node.size)
    return total, biggest


async def _big(td: Tidier, tree: Tree, top: str, plan: Plan, counts: dict[str, int]) -> None:
    """Second-level folders move whole, never split up (M7.1 A1): one that is
    ``big.folder`` or bigger, or holds any file of ``big.file`` or more, goes
    to ``<big.to>/<A>/`` with everything in it. Only files lying directly in
    the top-level folder move on their own."""
    big = td.spec.big
    home = join_path(big.to, top.strip("/"))
    for second in tree.folders(top):
        if td.is_gone(second.path):
            continue
        total, biggest = _weight(td, tree, second.path)
        if total < big.folder and biggest < big.file:
            continue
        if await td.attempt(plan, "tidy:big-folder", second,
                            lambda s=second: td.move(s, home, "tidy:big-folder")):
            counts["big_folders"] += 1
    for node in tree.loose(top):
        if node.size < big.file or td.is_gone(node.path):
            continue
        if await td.attempt(plan, "tidy:big-file", node,
                            lambda n=node: td.move(n, home, "tidy:big-file")):
            counts["big_files"] += 1


def _emptied(td: Tidier, tree: Tree, top: str, plan: Plan, counts: dict[str, int]) -> None:
    """A folder whose every file this plan moves out or trashes goes too, so
    the next run does not find it empty (one run, one tidy tree)."""
    if not td.spec.slim.empty_folders:
        return
    for node in tree.below(top):
        if (node.is_folder and depth(node.path) >= 3 and tree.files.get(node.path, 0) > 0
                and td.leaving.get(node.path, 0) >= tree.files[node.path]
                and not td.is_gone(td.where(node.path))
                and td.where(node.path) == node.path
                and not td.protection.holds(node.path)):
            plan.actions.extend(td.trash(node, "tidy:empty"))
            counts["empty"] += 1


# --------------------------------------------------------- §2 loose files


def _short(node: FileNode) -> str:
    return re.sub(r"[^0-9a-z]", "", (node.hash or node.file_id or "x").lower())[:6] or "x"


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def nests(group: str, folder_name: str) -> bool:
    """A group named (almost) like the folder it sits in, which would only add
    a level: /Nako/Nako/… (M7.1 A4.1). Compared after name normalisation,
    each containing the other counts."""
    a = _compact(name_key(group) or group)
    b = _compact(name_key(folder_name) or folder_name)
    return bool(a and b) and (a in b or b in a)


def is_ad(node: FileNode, spec: TidySpec) -> bool:
    """M7.1 A4.2: the kind of file ads come as, *and* a name that says so."""
    ads = spec.ads
    if not ads.enabled or node.is_folder:
        return False
    kind = category_of(node)
    ext = node.extension.lower()
    small_video = kind == "video" and node.size < ads.video_max
    if not (kind in ("image", "archive") or ext in ads.executables or small_video):
        return False
    stem = unicodedata.normalize("NFKC", stem_of(node.name)).casefold()
    if ads.suffixes:
        domain = (r"(?<![a-z0-9])[a-z0-9][a-z0-9-]*\.(?:"
                  + "|".join(re.escape(x) for x in ads.suffixes) + r")(?![a-z0-9])")
        if re.search(domain, stem):
            return True
    for word in ads.words:
        folded = word.casefold()
        if not folded:
            continue
        if folded.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(folded)}(?![a-z0-9])", stem):
                return True
        elif folded in stem:
            return True
    return False


def _ads(td: Tidier, files: list[FileNode], ads: Plan | None,
         counts: dict[str, int]) -> list[FileNode]:
    """Ad-like files go to the ``ads`` plan's trash; the rest is returned."""
    if ads is None:
        return files
    kept = []
    for node in files:
        if is_ad(node, td.spec):
            ads.actions.extend(td.trash(node, "tidy:ad"))
            counts["ads"] += 1
        else:
            kept.append(node)
    return kept


async def _loose(
    td: Tidier, folder: str, files: list[FileNode], subfolders: list[FileNode],
    plan: Plan, counts: dict[str, int], *, ads: Plan | None = None, skip_big: bool = True,
) -> None:
    """Group the files lying directly in ``folder`` (current paths).

    Files of ``big.file`` or more are the big part's (``skip_big``); ad-like
    files go to ``ads``; a group named like ``folder`` itself stays put.
    """
    loose = td.spec.loose
    noise = [re.compile(p, re.IGNORECASE) for p in loose.noise]
    here = folder.rsplit("/", 1)[-1]
    if skip_big:
        files = [f for f in files if f.size < td.spec.big.file]
    files = _ads(td, files, ads, counts)
    images = [f for f in files if category_of(f) == "image"]
    others = [f for f in files if category_of(f) != "image"]

    for node in images:
        await td.attempt(plan, "tidy:image", node,
                         lambda n=node: _to_shared(td, n, loose.images_to, "tidy:image", folder))
        counts["images"] += 1

    existing: dict[str, str] = {}
    for sub in subfolders:
        if td.is_gone(sub.path) or nests(sub.name, here):
            continue
        key = name_key(sub.name, noise)
        if not is_messy(key):
            existing.setdefault(key.casefold(), sub.name)
        existing.setdefault(sub.name.casefold(), sub.name)

    names = [f.name for f in others]
    groups, ungrouped = group_names(names, min_group=loose.min_group,
                                    min_prefix=loose.min_prefix, noise=noise)
    for group in groups:
        target = existing.get(group.name.casefold(), group.name)
        if nests(target, here):
            # /Nako/Nako/… would only add a level: these stay where they are.
            counts["stayed"] += len(group.members)
            continue
        dest = join_path(folder, target)
        for index in group.members:
            node = others[index]
            if await td.attempt(plan, "tidy:group", node,
                                lambda n=node, d=dest: td.move(n, d, "tidy:group")):
                counts["grouped"] += 1
        counts["groups"] += 1
    for index in ungrouped:
        node = others[index]
        key = name_key(node.name, noise)
        if not is_messy(key) and key.casefold() in existing:
            dest = join_path(folder, existing[key.casefold()])
            work, bucket = (lambda n=node, d=dest: td.move(n, d, "tidy:group")), "grouped"
            rule = "tidy:group"
        elif category_of(node) == "video":
            dest = join_path(folder, loose.misc)
            work, bucket = (lambda n=node, d=dest: td.move(n, d, "tidy:misc")), "misc"
            rule = "tidy:misc"
        elif loose.other.startswith("/"):
            rule, bucket = "tidy:other", "other"
            work = lambda n=node: _to_shared(td, n, loose.other, "tidy:other", folder)  # noqa: E731
        else:
            dest = join_path(folder, loose.other)
            work, bucket = (lambda n=node, d=dest: td.move(n, d, "tidy:other")), "other"
            rule = "tidy:other"
        if await td.attempt(plan, rule, node, work):
            counts[bucket] += 1


async def _to_shared(td: Tidier, node: FileNode, dest: str, rule: str,
                     source: str) -> list[Action]:
    """Into a folder the whole drive shares (/写真/杂, /其他). A name already
    there gets "_<short hash>"; the audit keeps where the file came from."""
    node = td.current(node)
    if dest == parent_of(node.path):
        return []
    actions: list[Action] = []
    if await td.planner.occupant(join_path(dest, node.name)) not in (None, node.file_id):
        ext = node.name[len(stem_of(node.name)):]
        renamed, node = await td.rename(node, f"{stem_of(node.name)}_{_short(node)}{ext}", rule)
        actions.extend(renamed)
    actions.extend(await td.move(node, dest, rule, {"source_top": top_of(source),
                                                    "source_folder": source}))
    return actions


# ----------------------------------------------------------- organize-tree


COUNT_KEYS = ("empty", "junk", "flatten", "big_folders", "big_files", "groups", "grouped",
              "misc", "other", "images", "shelved", "stayed", "ads")


def _counts() -> dict[str, int]:
    return dict.fromkeys(COUNT_KEYS, 0)


def _summarize(plan: Plan, counts: dict[str, int]) -> None:
    if any(counts.values()):
        plan.note("tidy.counts", **counts)


PARTS = ("slim", "big", "loose")


async def organize_tree(
    ctx: Context, *, scope: str | None = None, at: datetime | None = None,
    spec: TidySpec | None = None, parts: set[str] | None = None,
) -> list[Plan]:
    """One plan per top-level folder (``scope`` alone when given), doing
    ``parts`` of the work (all three by default). Empty plans are left out."""
    parts = set(parts or PARTS)
    spec = spec or load_spec(ctx)
    protection = await protect.load(ctx)
    tree = Tree.build(await ctx.store.nodes_under("/"))
    special = _special(spec)
    tops = [n for n in tree.folders("/")
            if n.path not in special and not protection.covers(n.path)]
    if scope is not None:
        wanted = top_of(normalize_path(scope))
        tops = [n for n in tops if n.path == wanted]
    stamp = (at or _now()).isoformat(timespec="seconds")
    result: list[Plan] = []
    # One planner for every top-level folder: they share /其他 and /写真/杂,
    # so two folders' files of one name must not both claim it.
    td = await _tidier(ctx, spec, protection, at)
    for top in tops:
        plan = Plan(source=f"{TREE}:{top.path}", generated_at=stamp)
        ads = Plan(source=f"{TREE_ADS}:{top.path}", generated_at=stamp)
        counts = _counts()
        if "slim" in parts:
            await _slim(td, tree, top.path, plan, counts)
        if "big" in parts:
            await _big(td, tree, top.path, plan, counts)
        if parts & {"slim", "big"}:
            _emptied(td, tree, top.path, plan, counts)
        if "loose" in parts:
            await _loose(td, top.path, [td.current(f) for f in tree.loose(top.path)],
                         tree.folders(top.path), plan, counts, ads=ads)
        _summarize(plan, counts)
        if plan.actions or plan.notes:
            plan.note("tidy.scope", path=top.path, size=human_size(tree.size.get(top.path, 0)))
            result.append(plan)
        if ads.actions:
            ads.note("tidy.ads", count=len(ads.actions), path=top.path)
            result.append(ads)
    return result


# ---------------------------------------------------------- organize-inbox


def _word_pattern(word: str) -> re.Pattern[str]:
    escaped = re.escape(word.casefold())
    if word.isascii():
        return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")
    return re.compile(escaped)


def _destinations(tree: Tree, spec: TidySpec,
                  protection: protect.Protection) -> list[tuple[str, str, re.Pattern[str]]]:
    """``(top folder, word, pattern)``, longest word first, for shelving."""
    special = _special(spec)
    out: list[tuple[str, str, re.Pattern[str]]] = []
    for top in tree.folders("/"):
        if top.path in special or protection.covers(top.path):
            continue
        words = [top.name, *spec.inbox.aliases.get(top.name, [])]
        for word in words:
            word = word.strip()
            if len(word) >= spec.inbox.min_name:
                out.append((top.path, word, _word_pattern(word)))
    out.sort(key=lambda item: (-len(item[1]), item[0], item[1]))
    return out


def shelf_for(name: str, destinations: list[tuple[str, str, re.Pattern[str]]]) -> str | None:
    text = unicodedata.normalize("NFKC", name).casefold()
    for top, _word, pattern in destinations:
        if pattern.search(text):
            return top
    return None


async def organize_inbox(
    ctx: Context, *, folders: list[str] | None = None, at: datetime | None = None,
    spec: TidySpec | None = None,
) -> list[Plan]:
    """Shelve what landed in the entry folders: one plan for all of them, and
    a second one for suspected ads when there are any."""
    spec = spec or load_spec(ctx)
    protection = await protect.load(ctx)
    tree = Tree.build(await ctx.store.nodes_under("/"))
    td = await _tidier(ctx, spec, protection, at)
    stamp = (at or _now()).isoformat(timespec="seconds")
    plan = Plan(source=INBOX, generated_at=stamp)
    ads = Plan(source=INBOX_ADS, generated_at=stamp)
    counts = _counts()
    destinations = _destinations(tree, spec, protection)
    arrivals: dict[str, list[FileNode]] = defaultdict(list)
    inboxes = [normalize_path(f) for f in (folders or spec.inbox.folders)]
    for inbox in inboxes:
        if inbox not in tree.by_path or protection.covers(inbox):
            continue
        leftovers: list[FileNode] = []
        entries = tree.kids.get(inbox, [])
        clean = {n.file_id for n in _ads(td, [e for e in entries if not e.is_folder], ads, counts)}
        for entry in entries:
            if not entry.is_folder and entry.file_id not in clean:
                continue  # an ad: planned for the trash, not shelved
            top = shelf_for(entry.name, destinations)
            if top is None:
                if not entry.is_folder:
                    leftovers.append(entry)
                continue
            if await td.attempt(plan, "inbox:shelve", entry,
                                lambda e=entry, t=top: td.move(e, t, "inbox:shelve")):
                counts["shelved"] += 1
                if not entry.is_folder:
                    arrivals[top].append(replace(entry, path=join_path(top, entry.name)))
            elif not entry.is_folder:
                leftovers.append(entry)
        await _loose(td, inbox, leftovers, tree.folders(inbox), plan, counts, skip_big=False)
    for top in sorted(arrivals):
        # Newcomers join the grouping of the folder they moved into (§4 rule 1).
        files = [*tree.loose(top), *arrivals[top]]
        await _loose(td, top, files, tree.folders(top), plan, counts, ads=ads)
    _summarize(plan, counts)
    result = [plan]
    if ads.actions:
        ads.note("tidy.ads", count=len(ads.actions), path=", ".join(inboxes))
        result.append(ads)
    return result


# -------------------------------------------------------------- big report


@dataclass
class ReportItem:
    file_id: str
    path: str
    size: int
    kind: str
    modified: str | None = None


@dataclass
class BigReport:
    files: list[ReportItem] = field(default_factory=list)
    folders: list[ReportItem] = field(default_factory=list)
    stale: list[ReportItem] = field(default_factory=list)
    stale_days: int = 90
    duplicates: int = 0
    """Bytes held by duplicate copies (outside the whitelist)."""

    @property
    def reclaimable(self) -> int:
        """A rough estimate: the stale big files plus the duplicate copies."""
        return sum(item.size for item in self.stale) + self.duplicates

    def items(self) -> list[ReportItem]:
        """Everything listed, in order, each once: what the buttons refer to."""
        seen: set[str] = set()
        out: list[ReportItem] = []
        for item in (*self.files, *self.folders, *self.stale):
            if item.file_id not in seen:
                seen.add(item.file_id)
                out.append(item)
        return out

    def lines(self) -> list[str]:
        from ..i18n import t

        numbers = {item.file_id: n for n, item in enumerate(self.items(), start=1)}
        out = [t("big.files", count=len(self.files))]
        out += [t("big.line", n=numbers[i.file_id], size=human_size(i.size), path=i.path)
                for i in self.files]
        if self.folders:
            out.append(t("big.folders", count=len(self.folders)))
            out += [t("big.line", n=numbers[i.file_id], size=human_size(i.size), path=i.path)
                    for i in self.folders]
        if self.stale:
            out.append(t("big.stale", days=self.stale_days, count=len(self.stale)))
            out += [t("big.line", n=numbers[i.file_id], size=human_size(i.size), path=i.path)
                    for i in self.stale]
        out.append(t("big.reclaim", size=human_size(self.reclaimable),
                     duplicates=human_size(self.duplicates)))
        out.append(t("big.never_deletes"))
        return out


async def big_report(ctx: Context, *, scope: str = "/", at: datetime | None = None,
                     spec: TidySpec | None = None) -> BigReport:
    spec = spec or load_spec(ctx)
    protection = await protect.load(ctx)
    now = at or _now()
    nodes = [n for n in await ctx.store.nodes_under(scope) if not protection.covers(n.path)]
    tree = Tree.build(nodes)
    rep = spec.report
    report = BigReport(stale_days=rep.stale_days)

    files = sorted((n for n in nodes if not n.is_folder), key=lambda n: (-n.size, n.path))
    report.files = [ReportItem(n.file_id, n.path, n.size, "file", n.modified_time)
                    for n in files[: rep.files]]

    chosen: list[FileNode] = []
    candidates = sorted(
        (n for n in nodes if n.is_folder and depth(n.path) >= 2
         and not protection.holds(n.path)),
        key=lambda n: (-tree.size.get(n.path, 0), n.path),
    )
    for folder in candidates:
        if len(chosen) >= rep.folders:
            break
        if any(folder.path.startswith(c.path + "/") or c.path.startswith(folder.path + "/")
               for c in chosen):
            continue  # one line per branch: a folder, not also its parent
        chosen.append(folder)
    report.folders = [ReportItem(f.file_id, f.path, tree.size.get(f.path, 0), "folder")
                      for f in chosen]

    cutoff = now - timedelta(days=rep.stale_days)
    stale = [n for n in files if n.size >= spec.big.file
             and (parse_time(n.modified_time or n.created_time) or now) < cutoff]
    report.stale = [ReportItem(n.file_id, n.path, n.size, "file", n.modified_time)
                    for n in stale[: rep.stale]]

    by_hash: dict[str, list[FileNode]] = defaultdict(list)
    for node in files:
        if node.hash:
            by_hash[node.hash].append(node)
    report.duplicates = sum(
        sum(n.size for n in group[1:]) for group in by_hash.values()
        if len(group) > 1 and len({n.size for n in group}) == 1
    )
    return report


def trash_plan(node: FileNode) -> Plan:
    """The one-action plan behind a report's [trash] button."""
    plan = Plan(source="big-report", generated_at=_now().isoformat(timespec="seconds"))
    plan.actions.append(Action(ActionType.TRASH, node.file_id, before=node.snapshot(),
                               rule_name="big-report"))
    return plan


def sample_moves(plans: list[Plan], per_folder: int, *, seed: int = 7) -> list[dict[str, str]]:
    """For spot checks (M7 §8): up to ``per_folder`` moves from each plan (and
    trashes from each ads plan), picked the same way every time. Each is
    ``{"plan", "action", "rule", "from", "to"}``; ``to`` is empty for a trash."""
    import random

    out: list[dict[str, str]] = []
    for plan in plans:
        wanted = ActionType.TRASH if is_ads_plan(plan.source) else ActionType.MOVE
        pool = [a for a in plan.actions if a.type is wanted]
        chosen = random.Random(f"{seed}:{plan.source}").sample(pool, min(per_folder, len(pool)))
        out += [{"plan": plan.source, "action": str(a.type), "rule": a.rule_name,
                 "from": a.before.get("path", ""), "to": a.after.get("path", "")}
                for a in sorted(chosen, key=lambda a: a.before.get("path", ""))]
    return out


def describe_counts(counts: dict[str, Any]) -> dict[str, Any]:
    return {key: counts.get(key, 0) for key in COUNT_KEYS}
