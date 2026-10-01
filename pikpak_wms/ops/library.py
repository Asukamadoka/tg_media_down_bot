"""Where files taken out of the drive land on the NAS (docs/wms/M8.3 §H).

The NAS share ``资源库`` is mounted in the container as ``LIBRARY_DIR``
(``/library``). Two cases:

* no place named: ``LIBRARY_DIR/资源/整理/{Y}/{Y}.{M}/{Y}.{M}.{D}`` — month and day
  without zero padding (``2026.9``, ``2026.9.30``), the day being the day the
  download *runs*, in the WMS time zone;
* a place named: always a path *inside* the library. ``资源库/电影/日剧``,
  ``/电影/日剧`` and ``电影/日剧`` all mean ``LIBRARY_DIR/电影/日剧``; a path with
  ``..`` or one that points outside the library is refused.

Everything here is pure (no disk access except :func:`missing_dirs`), so the
plan can say what will happen before anything does.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from ..config import Config
from ..core.errors import WmsError

LIBRARY_NAME = "资源库"
"""How the share is called on the NAS; shown in front of every library path."""

DEFAULT_LAYOUT = "资源/整理/{Y}/{Y}.{M}/{Y}.{M}.{D}"

# A first segment that names a place in the container or on the NAS itself,
# not a folder of the library. "/电影/日剧" is a library path; "/etc/x" or
# "/volume3/x" is somebody reaching for the outside, and is refused rather
# than quietly filed under the library.
_OUTSIDE_ROOTS = frozenset({
    "etc", "usr", "var", "tmp", "root", "home", "proc", "sys", "dev", "bin", "sbin", "opt",
    "mnt", "media", "boot", "lib", "srv", "run",
})
_VOLUME = re.compile(r"^volume\d*$", re.IGNORECASE)
_DRIVE = re.compile(r"^[A-Za-z]:$")


def expand_layout(template: str, when: datetime) -> str:
    """``{Y}`` ``{M}`` ``{D}`` for ``when`` (already in the right time zone)."""
    return (template.replace("{Y}", str(when.year)).replace("{M}", str(when.month))
            .replace("{D}", str(when.day))).strip("/")


def _refuse(raw: str, why: str) -> WmsError:
    return WmsError(f"{raw!r} is outside the library ({why})", key="library.outside",
                    path=raw, why=why)


def resolve_user_path(raw: str, *, library_dir: str | Path | None = None) -> str:
    """A place a person named → its path relative to the library (no leading slash).

    Raises :class:`WmsError` (``library.outside``) for ``..`` and for absolute
    paths outside the library. An empty result means the library root.
    """
    text = (raw or "").strip().replace("\\", "/")
    if "\x00" in text:
        raise _refuse(raw, "NUL")
    if text.startswith("~"):
        raise _refuse(raw, "~")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise _refuse(raw, "..")
    if parts and _DRIVE.match(parts[0]):
        raise _refuse(raw, "drive")
    root = [p for p in str(library_dir or "").replace("\\", "/").split("/") if p]
    if root and parts[: len(root)] == root:
        # The container's own spelling of the library: /library/电影
        parts = parts[len(root):]
    elif text.startswith("/") and parts and (
        parts[0].lower() in _OUTSIDE_ROOTS or _VOLUME.match(parts[0])
    ):
        raise _refuse(raw, "absolute")
    elif parts and parts[0] == LIBRARY_NAME:
        parts = parts[1:]
    return "/".join(parts)


def display_path(relative: str) -> str:
    """What a person sees: 资源库/资源/整理/2026/2026.10/2026.10.1."""
    return "/".join(p for p in (LIBRARY_NAME, relative.strip("/")) if p)


def missing_dirs(library: Path, relative: str) -> list[str]:
    """The folders of ``relative`` that do not exist yet, outermost first (as
    library-relative paths): what the plan says will be created."""
    missing, current = [], library
    for part in [p for p in relative.split("/") if p]:
        current = current / part
        if not current.exists():
            missing.append(str(current.relative_to(library)))
    return missing


def default_dir(config: Config, when: datetime | None = None) -> str:
    """The library folder a download with no named place goes to, for the day ``when``."""
    when = (when or datetime.now(UTC)).astimezone(config.schedule.tz)
    return expand_layout(config.outbound.default_layout or DEFAULT_LAYOUT, when)


def destination(config: Config, to: str, *, when: datetime | None = None) -> tuple[str, str]:
    """Where a download lands, library mode: (path relative to the library, how to
    show it). ``to`` is what a person named ("" for nothing). Raises when it
    points outside the library."""
    named = (resolve_user_path(to, library_dir=config.outbound.library_path)
             if to.strip() else "")
    relative = named or default_dir(config, when)
    return relative, display_path(relative)
