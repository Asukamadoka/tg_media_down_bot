"""Evaluate a rule's ``match:`` against one entry of the local index (rule 4)."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Collection
from datetime import datetime, tzinfo
from typing import Any

from ..core.models import FileNode
from .schema import CATEGORIES, Match
from .units import parse_moment


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """A path glob: ``*`` stays inside one folder, ``**`` crosses folders.

    A pattern without a leading ``/`` may match at any depth.
    """
    if not pattern.startswith("/"):
        pattern = "/**/" + pattern
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def category_of(node: FileNode) -> str:
    """The first category the file belongs to, or ``other``."""
    if node.is_folder:
        return "folder"
    for name in CATEGORIES:
        if _in_category(node, name):
            return name
    return "other"


def _in_category(node: FileNode, name: str) -> bool:
    prefix, extensions = CATEGORIES[name]
    return bool(prefix and node.mime.startswith(prefix)) or node.extension.lower() in extensions


class Matcher:
    def __init__(self, match: Match) -> None:
        self.match = match
        self._regex = re.compile(match.name_regex) if match.name_regex else None
        self._globs = [glob_to_regex(p) for p in match.path_glob or []]

    def test(
        self,
        node: FileNode,
        *,
        now: datetime,
        tz: tzinfo,
        nonempty: Collection[str] = (),
    ) -> dict[str, Any] | None:
        """The named groups of ``name_regex`` when every matcher holds, else None."""
        m = self.match
        if m.kind is not None and str(node.kind) != m.kind:
            return None
        if m.exclude_paths and any(
            node.path == folder or node.path.startswith(folder.rstrip("/") + "/")
            for folder in m.exclude_paths
        ):
            return None
        if self._globs and not any(g.match(node.path) for g in self._globs):
            return None
        if m.mime and not any(fnmatch.fnmatchcase(node.mime, p) for p in m.mime):
            return None
        if m.extensions is not None and node.extension.lower() not in m.extensions:
            return None
        if m.category and (node.is_folder or not any(_in_category(node, c) for c in m.category)):
            return None
        if m.min_size is not None and (node.is_folder or node.size < m.min_size):
            return None
        if m.max_size is not None and (node.is_folder or node.size > m.max_size):
            return None
        if m.empty is not None:
            empty = node.file_id not in nonempty if node.is_folder else node.size == 0
            if empty != m.empty:
                return None
        if m.older_than is not None or m.newer_than is not None:
            stamp = node.created if m.time_field == "created" else node.modified
            if stamp is None:
                return None
            if m.older_than is not None and stamp >= parse_moment(m.older_than, now=now, tz=tz):
                return None
            if m.newer_than is not None and stamp < parse_moment(m.newer_than, now=now, tz=tz):
                return None
        if self._regex is None:
            return {}
        found = self._regex.search(node.name)
        if found is None:
            return None
        return {key: value for key, value in found.groupdict().items() if value is not None}
