"""A second opinion on which way a model pointed the time (docs/wms/M8.2 §C).

The worst thing a small model did in the Mac evaluation was turn
「删除 /Temp 里超过 7 天的文件」 into ``created_after: 7d``: a plan that trashes
the files of the last seven days instead of the older ones. The model cannot
be made never to do that, so the sentence is read again here, by words, and
the time condition brought into line with it:

* 「之前」「以前」「前的」「N 天前」「超过 N 天」「早于」 and no word that says
  "since" (「最近」「以来」「之内」「N 天内」「以后」): the sentence means *older*,
  so the condition is ``created_before``;
* 「最近」「之内」「以来」「今天」「本周」 …: the sentence means *newer*, so it is
  ``created_after``;
* anything else: no opinion, nothing is touched.

A correction is noted on the query (shown in the plan). A condition that
cannot be read either way is handed back as a question: the same duration
as both lower and upper bound means the model did not understand the
sentence.

It runs on what a model returned, not on the rules parser, which reads the
same words itself.

**Conditions with no basis** (docs/wms/M8.3 §C). A model also invents filters:
a size range, a time window, a name, that nobody mentioned. :func:`ground` reads
the sentence (the original and what was added to it afterwards, together) for
words that could be the source of each condition, and drops the ones with none:

* no size word (GB, MB, 大于, 超过 N G, 大文件 …) → ``min_size`` and ``max_size`` go;
* no time word (天, 周, 月, 年, 今天, 之前, 以来, 最近, a date …) → ``created_after``
  and ``created_before`` go;
* a name condition whose text is nowhere in the sentence → it goes. (A name that
  *is* in the sentence stays even without 「名为」「包含」: 「下载印象足拍的视频」
  names the files by saying what they are called. Dropping it would widen what
  a delete matches.)
* ``min_size == max_size`` and no 「等于」 → both go.

Each drop is noted on the query, so the plan says so.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, tzinfo
from typing import Literal

from ..rules.units import parse_moment
from .query import Clarification, Query

Direction = Literal["older", "newer"]

CORRECTED = "nl.explain.direction_fixed"

_NUM = r"[0-9零一二两三四五六七八九十半]+(?:\.\d+)?"
_UNIT = r"(?:个)?(?:天|日|周|星期|礼拜|月|年|小时)"

_OLDER = re.compile(
    r"之前|以前|前的|早于|older\s+than|earlier\s+than|before\b"
    rf"|超过\s*{_NUM}\s*{_UNIT}|超过半年|超过一年"
    rf"|{_NUM}\s*{_UNIT}\s*前|半年前"
    r"|很久没|久未|旧文件|老文件",
    re.IGNORECASE,
)
# Words that say "since": they veto the reading "older".
_SINCE = re.compile(
    r"最近|以来|之内|以内|[天日周月年]内|小时内|以后|之后|过去|within|since|\blast\b|\bpast\b",
    re.IGNORECASE,
)
# Words that only make sense for a window up to now.
_WINDOW = re.compile(r"今天|今日|本周|本月|昨天|昨日|今年|这周|这个月", re.IGNORECASE)

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdwmy])\s*$", re.IGNORECASE)


def direction_of(sentence: str) -> Direction | None:
    """Which way the sentence's time condition points, or None when unclear."""
    text = unicodedata.normalize("NFKC", sentence)
    older, since = _OLDER.search(text), _SINCE.search(text)
    if older and not since:
        return "older"
    if older:
        return None  # 「最近……之前」: both; not ours to call
    if since or _WINDOW.search(text):
        return "newer"
    return None


def _same_duration(first: str, second: str) -> bool:
    a, b = _DURATION.match(first or ""), _DURATION.match(second or "")
    return bool(a and b and a.group(1) == b.group(1) and a.group(2).lower() == b.group(2).lower())


DROPPED = "nl.explain.dropped"

_SIZE_WORDS = re.compile(
    r"\d\s*(?:个)?\s*(?:[kmgt](?:i?b)?(?![a-z])|兆|吉)|\b[kmgt]i?b\b"
    r"|大于|小于|多于|少于|不小于|不大于|至少|至多|最多|大小|多大|容量|体积|大文件|小文件"
    r"|很大|很小|比较大|比较小|larger|smaller|bigger|size",
    re.IGNORECASE,
)
_TIME_WORDS = re.compile(
    r"[天日周月年]|星期|礼拜|小时|今天|昨天|前天|之前|以前|之后|以后|以来|最近|刚|早于|晚于"
    r"|新(?:下载|转存|入库|增|加|上传|进)"
    r"|\d{4}\s*[-/.]\s*\d|\d{1,2}\s*[-/.]\s*\d{1,2}|凌晨|早上|早晨|上午|中午|下午|傍晚|晚上"
    r"|today|yesterday|week|month|year|day|hour|ago|since|before|after|recent|last|older|newer",
    re.IGNORECASE,
)
_EQUALS = re.compile(r"等于|恰好|正好|刚好|整整|equals?\b|exactly", re.IGNORECASE)
_QUOTED = re.compile(r"[「“\"'『][^」”\"'』]*[」”\"'』]")
_FILENAME = re.compile(r"\S+\.[A-Za-z0-9]{2,5}\b")


def _evidence_text(sentence: str) -> str:
    """The sentence without file names and quoted strings: a year or a size inside a
    name is part of the name, not a condition."""
    text = unicodedata.normalize("NFKC", sentence)
    return _FILENAME.sub(" ", _QUOTED.sub(" ", text))


def _literal_parts(pattern: str) -> list[str]:
    """The readable pieces of a name regex (``^sample`` → ``sample``)."""
    return [p for p in re.split(r"[\\^$.*+?()\[\]{}|]+", pattern) if len(p) >= 2]


def ground(sentence: str, result: Query | Clarification | None) -> Query | Clarification | None:
    """``result`` without the conditions the sentence gives no basis for."""
    if not isinstance(result, Query):
        return result
    filters, dropped = result.filters, []
    evidence = _evidence_text(sentence)
    if (filters.min_size is not None and filters.min_size == filters.max_size
            and not _EQUALS.search(unicodedata.normalize("NFKC", sentence))):
        filters.min_size = filters.max_size = None
        dropped.append("size")
    if (filters.min_size is not None or filters.max_size is not None) \
            and not _SIZE_WORDS.search(evidence):
        filters.min_size = filters.max_size = None
        if "size" not in dropped:
            dropped.append("size")
    if (filters.created_after or filters.created_before) and not _TIME_WORDS.search(evidence):
        filters.created_after = filters.created_before = None
        dropped.append("time")
    text = unicodedata.normalize("NFKC", sentence).casefold()
    named = [item for item in filters.name_contains if item.casefold() in text]
    regex_ok = filters.name_regex is None or any(
        part.casefold() in text for part in _literal_parts(filters.name_regex))
    if len(named) != len(filters.name_contains) or not regex_ok:
        filters.name_contains = named
        if not regex_ok:
            filters.name_regex = None
        dropped.append("name")
    if dropped:
        result.corrections.append(f"{DROPPED}:{','.join(dropped)}")
    return result


def _near_now(value: str, now: datetime, tz: tzinfo) -> bool:
    """The upper bound is (within a day of) the present: 「半年前到现在」."""
    try:
        return parse_moment(value, now=now, tz=tz) >= now - timedelta(days=1)
    except (ValueError, TypeError):
        return False


def guard(
    sentence: str, result: Query | Clarification | None, *, now: datetime | None = None,
    tz: tzinfo | None = None,
) -> Query | Clarification | None:
    """``result`` with its time condition pointing the way the sentence does.

    Given both bounds, an "older" sentence still means one bound: 「超过半年」 answered
    as ``created_after: 半年前`` + ``created_before: 现在`` is "the last half year"; it
    becomes ``created_before: 半年前`` (M8.3 §B). It takes ``now`` to see that the upper
    bound is the present.
    """
    if not isinstance(result, Query):
        return result
    filters = result.filters
    after, before = filters.created_after, filters.created_before
    if after and before and _same_duration(after, before):
        # 「超过 7 天」 answered as both "within 7 days" and "older than 7 days".
        return Clarification(question="nl.ask.time_direction")
    wanted = direction_of(sentence)
    # Only a lower bound, or one with an upper bound that is just "now": both mean the
    # window up to the present, where the sentence says "older than".
    flipped = bool(after) and (not before or (
        now is not None and _near_now(before, now, tz or now.tzinfo)))
    if wanted == "older" and flipped:
        filters.created_before, filters.created_after = after, None
    elif wanted == "newer" and before and not after:
        filters.created_after, filters.created_before = before, None
    else:
        return result
    result.corrections.append(CORRECTED)
    return result
