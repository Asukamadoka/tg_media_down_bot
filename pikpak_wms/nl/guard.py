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
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

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


def guard(sentence: str, result: Query | Clarification | None) -> Query | Clarification | None:
    """``result`` with its time condition pointing the way the sentence does."""
    if not isinstance(result, Query):
        return result
    filters = result.filters
    after, before = filters.created_after, filters.created_before
    if after and before and _same_duration(after, before):
        # 「超过 7 天」 answered as both "within 7 days" and "older than 7 days".
        return Clarification(question="nl.ask.time_direction")
    wanted = direction_of(sentence)
    if wanted == "older" and after and not before:
        filters.created_before, filters.created_after = after, None
    elif wanted == "newer" and before and not after:
        filters.created_after, filters.created_before = before, None
    else:
        return result
    result.corrections.append(CORRECTED)
    return result
