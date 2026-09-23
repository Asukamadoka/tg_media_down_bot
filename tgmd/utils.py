"""Small pure helpers: formatting, filename building, id parsing."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from string import Formatter

# Characters that are illegal on Windows or awkward on POSIX shells.
_ILLEGAL_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_WHITESPACE = re.compile(r"\s+")

# Names Windows refuses regardless of extension.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

MAX_COMPONENT_LEN = 120


def human_size(num_bytes: float | None) -> str:
    """Format a byte count the way a person reads it."""
    if num_bytes is None:
        return "unknown size"
    value = float(num_bytes)
    if value < 1024:
        return f"{int(value)} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PiB"


def human_duration(seconds: float | None) -> str:
    """Format a duration as ``1h02m``, ``3m07s`` or ``12s``."""
    if seconds is None or seconds < 0:
        return "?"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def human_rate(bytes_per_second: float | None) -> str:
    """Format a transfer rate."""
    if not bytes_per_second or bytes_per_second <= 0:
        return "—"
    return f"{human_size(bytes_per_second)}/s"


def progress_bar(fraction: float, width: int = 12) -> str:
    """Render a text progress bar for a fraction between 0 and 1."""
    fraction = min(max(fraction, 0.0), 1.0)
    filled = round(fraction * width)
    return "█" * filled + "░" * (width - filled)


def sanitize_component(name: str, *, fallback: str = "unnamed") -> str:
    """Make one path component safe to write on any filesystem."""
    text = unicodedata.normalize("NFC", name or "")
    text = _ILLEGAL_CHARS.sub("_", text)
    text = _WHITESPACE.sub(" ", text).strip(" .")
    if not text:
        return fallback
    if text.upper() in _RESERVED_NAMES or text.split(".")[0].upper() in _RESERVED_NAMES:
        text = f"_{text}"
    if len(text) > MAX_COMPONENT_LEN:
        stem, dot, ext = text.rpartition(".")
        if dot and len(ext) <= 12:
            keep = MAX_COMPONENT_LEN - len(ext) - 1
            text = f"{stem[:keep]}.{ext}"
        else:
            text = text[:MAX_COMPONENT_LEN]
    return text or fallback


def split_extension(file_name: str) -> tuple[str, str]:
    """Split ``video.tar.gz`` into ``("video.tar", ".gz")``, safely."""
    path = PurePosixPath(file_name or "")
    suffix = path.suffix
    if len(suffix) > 12 or " " in suffix:
        return file_name, ""
    return path.stem if suffix else file_name, suffix


def template_fields(template: str) -> set[str]:
    """Return the field names referenced by a format template."""
    return {
        name.split(".")[0].split("[")[0]
        for _, name, _, _ in Formatter().parse(template)
        if name
    }


ALLOWED_TEMPLATE_FIELDS = frozenset(
    {"chat", "chat_id", "message_id", "topic_id", "name", "stem", "ext", "date"}
)


def build_relative_path(
    template: str,
    *,
    chat: str,
    chat_id: int | str,
    message_id: int,
    name: str,
    topic_id: int | None = None,
    when: datetime | None = None,
) -> Path:
    """Render ``template`` into a relative, traversal-free path.

    Every substituted value is sanitized before formatting, and the result is
    re-split on ``/`` so a template may create subdirectories while a chat
    title containing slashes cannot.
    """
    stem, ext = split_extension(name)
    when = when or datetime.now(UTC)
    values = {
        "chat": sanitize_component(chat, fallback="chat"),
        "chat_id": sanitize_component(str(chat_id), fallback="0"),
        "message_id": str(int(message_id)),
        "topic_id": str(topic_id) if topic_id else "0",
        "name": sanitize_component(name, fallback=f"{message_id}"),
        "stem": sanitize_component(stem, fallback=f"{message_id}"),
        "ext": ext,
        "date": when.strftime("%Y-%m-%d"),
    }
    try:
        rendered = template.format(**values)
    except (KeyError, IndexError, ValueError):
        rendered = f"{values['chat']}/{values['message_id']}_{values['name']}"

    parts = [sanitize_component(p) for p in rendered.split("/") if p not in ("", ".", "..")]
    if not parts:
        parts = [values["name"]]
    # A template of "{chat}/{message_id}_{stem}" drops the extension; put it back.
    if ext and not parts[-1].endswith(ext):
        parts[-1] = sanitize_component(parts[-1] + ext)
    return Path(*parts)


def unique_path(path: Path) -> Path:
    """Return ``path``, or the first ``name (n).ext`` variant that is free."""
    if not path.exists():
        return path
    stem, ext = split_extension(path.name)
    for counter in range(1, 1000):
        candidate = path.with_name(f"{stem} ({counter}){ext}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}-{int(datetime.now().timestamp())}{ext}")


def parse_id_list(raw: str | list | tuple | None) -> list[int]:
    """Parse ``"1, 2;3"`` or ``[1, "2"]`` into ``[1, 2, 3]``, skipping junk."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items: list[str] = [str(item) for item in raw]
    else:
        items = re.split(r"[,;\s]+", str(raw))
    result: list[int] = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        try:
            value = int(item)
        except ValueError:
            continue
        if value not in result:
            result.append(value)
    return result


def parse_bool(raw: object, default: bool = False) -> bool:
    """Interpret the usual truthy spellings found in env vars and YAML."""
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def truncate(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters with an ellipsis."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def escape_html(text: str) -> str:
    """Escape the three characters Telegram's HTML parse mode cares about."""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
