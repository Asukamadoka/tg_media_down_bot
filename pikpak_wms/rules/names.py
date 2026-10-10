"""Name keys and grouping for loose files (docs/wms/M7 §2.1).

A file's *key* is its name with the noise taken out: the extension, episode
and part numbers, resolution and codec tags, dates, site tags in brackets,
and punctuation at either end. Files in one folder whose keys are equal, or
share a prefix of at least ``min_prefix`` characters, form a group.

Everything here is pure and deterministic: the same names always give the
same groups, in the same order, with the same names (the plan is shown to a
person before it runs, and running it twice must plan nothing new).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_EXT = re.compile(r"\.([A-Za-z0-9]{1,5})$")
_BRACKETS = re.compile(
    r"\[[^\]]*\]|【[^】]*】|\([^)]*\)|（[^）]*）|\{[^}]*\}|「[^」]*」|《[^》]*》"
)
_BRACKET_CHARS = re.compile(r"[\[\]【】()（）{}「」《》]")
_DOMAIN = re.compile(
    r"(?i)(?:https?://)?(?:www\.)?[a-z0-9-]+\.(?:com|net|org|cc|tv|me|xyz|top|vip|info|io|co|cn|"
    r"la|in|club|site|live|fun|app)\b"
)
_NOISE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"(?<![a-z0-9])(?:2160|1440|1080|720|576|480|360)[pi](?![a-z0-9])",
        r"(?<![a-z0-9])[248]k(?![a-z0-9])",
        r"(?<![a-z0-9])(?:[xh]\.?26[45]|hevc|avc|aac|ac3|dts|flac|mp3|10bit|8bit|hdr(?:10)?|"
        r"web-?dl|web-?rip|bluray|blu-ray|bdrip|brrip|hdtv|remux|uhd|fhd|60fps|hq|sd|hd)"
        r"(?![a-z0-9])",
        r"(?:19|20)\d{2}\s*[-._/年]\s*\d{1,2}\s*[-._/月]\s*\d{1,2}\s*日?",
        r"(?:19|20)\d{2}\s*[-._/年]\s*\d{1,2}\s*月?",
        r"(?<!\d)(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)",
        r"(?<![a-z0-9])s\d{1,3}\s*e\d{1,4}(?![a-z0-9])",
        r"(?<![a-z0-9])(?:ep?|episode|part|pt|vol|cd|disc|no)\.?\s*[-_]?\s*\d+(?![a-z0-9])",
        r"第\s*[\d一二三四五六七八九十百零两]+\s*[集话話部期章回卷季篇]",
        r"(?:上|中|下)(?:集|部|篇)",
    )
]
_LEADING_NUMBER = re.compile(r"^\s*\d+\s*[-_.、)\]]+\s*")
_TRAILING_NUMBER = re.compile(r"[\s\-_.#·~+]*\d+\s*$")
_SEPARATORS = re.compile(r"[\s._\-+~·•|,，、:：;；!！?？@&=#'\"“”‘’]+")
_ENDS = " -_.·,，。!！?？~#@&+=:：;；|/\\\"'“”‘’()（）[]【】"
_HASHY = re.compile(r"[0-9a-f]{8,}", re.IGNORECASE)
_RANDOM = re.compile(r"[A-Za-z0-9]{12,}")
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f{}]')
_GENERIC = {
    # What cameras, phones and apps call files: a shared prefix, not a title.
    "img", "vid", "video", "mov", "dsc", "dscf", "dscn", "mvi", "pxl", "gopr", "dji", "wp",
    "screenrecording", "screen recording", "screenshot", "record", "rec", "clip", "file",
    "new", "untitled", "mmexport", "wx camera", "wechat", "微信", "视频", "录屏", "屏幕录制",
    "新建", "未命名", "download", "下载",
}

MAX_NAME = 80


def stem_of(name: str) -> str:
    return _EXT.sub("", name)


def _strip(text: str, noise: list[re.Pattern[str]]) -> str:
    text = _DOMAIN.sub(" ", text)
    for pattern in (*noise, *_NOISE):
        text = pattern.sub(" ", text)
    text = _LEADING_NUMBER.sub("", text)
    previous = None
    while previous != text:  # "Nako 03 (2)" loses both numbers
        previous = text
        text = _TRAILING_NUMBER.sub("", text).strip(_ENDS)
    text = _SEPARATORS.sub(" ", text)
    return text.strip(_ENDS).strip()


def name_key(name: str, noise: list[re.Pattern[str]] | None = None) -> str:
    """The display form of a file's key: noise removed, case kept."""
    noise = noise or []
    text = unicodedata.normalize("NFKC", stem_of(name))
    key = _strip(_BRACKETS.sub(" ", text), noise)
    if is_messy(key):
        # The title may be the bracketed part: "[Nako] 03.mp4" is Nako's.
        key = _strip(_BRACKET_CHARS.sub(" ", text), noise)
    return key


def is_messy(key: str) -> bool:
    """Empty, digits only, or a hash-like string: nothing to group on."""
    compact = key.replace(" ", "")
    if len(compact) < 2 or compact.isdigit() or key.casefold() in _GENERIC:
        return True
    if _HASHY.fullmatch(compact):
        return True
    return bool(
        _RANDOM.fullmatch(compact)
        and any(c.isdigit() for c in compact)
        and any(c.isalpha() for c in compact)
    )


def folder_name(key: str) -> str:
    """A key made safe as a folder name."""
    cleaned = _UNSAFE.sub(" ", key)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(_ENDS).strip()
    return cleaned[:MAX_NAME].rstrip()


def _fold(key: str) -> str:
    return key.casefold()


def _common(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def _cut_to_word(prefix: str, rest: list[str]) -> str:
    """Do not end a group name in the middle of a Latin word: "Cosplay_Nak" → "Cosplay"."""
    if not prefix:
        return prefix
    last = prefix[-1]
    continues = any(len(r) > len(prefix) and r[len(prefix)].isascii()
                    and r[len(prefix)].isalnum() for r in rest)
    if last.isascii() and last.isalnum() and continues:
        cut = re.sub(r"[A-Za-z0-9]+$", "", prefix)
        return cut
    return prefix


@dataclass
class Group:
    name: str
    members: list[int] = field(default_factory=list)
    """Indexes into the list :func:`group_names` was given."""


def group_names(
    names: list[str], *, min_group: int = 2, min_prefix: int = 4,
    noise: list[re.Pattern[str]] | None = None,
) -> tuple[list[Group], list[int]]:
    """Group ``names``; returns the groups and the indexes that joined none.

    Keys are sorted (case-folded, then by the original name); neighbours
    whose keys are equal, or share at least ``min_prefix`` characters, fall
    in one run. A run of ``min_group`` or more is a group, named after the
    common prefix (cut back to a word boundary and cleaned). Messy keys
    never group.
    """
    keys = [name_key(name, noise) for name in names]
    order = sorted(
        (i for i, key in enumerate(keys) if not is_messy(key)),
        key=lambda i: (_fold(keys[i]), names[i]),
    )
    runs: list[list[int]] = []
    for index in order:
        if runs:
            prev = runs[-1][-1]
            a, b = _fold(keys[prev]), _fold(keys[index])
            if a == b or _common(a, b) >= min_prefix:
                runs[-1].append(index)
                continue
        runs.append([index])

    groups: list[Group] = []
    loose = [i for i, key in enumerate(keys) if is_messy(key)]
    for run in runs:
        folded = [_fold(keys[i]) for i in run]
        if len(run) < min_group:
            loose.extend(run)
            continue
        first = keys[run[0]]
        if len(set(folded)) == 1:
            name = folder_name(first)
        else:
            length = min(_common(folded[0], other) for other in folded[1:])
            prefix = _cut_to_word(first[:length], [keys[i] for i in run])
            name = folder_name(prefix)
            if len(name.replace(" ", "")) < min_prefix:
                loose.extend(run)
                continue
        if not name:
            loose.extend(run)
            continue
        groups.append(Group(name=name, members=sorted(run, key=lambda i: names[i])))
    return groups, sorted(loose, key=lambda i: names[i])
