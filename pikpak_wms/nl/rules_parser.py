"""The ``rules`` backend: a deterministic parser for the common Chinese phrasings.

Zero cost, milliseconds, and — the one hard requirement (docs/wms/M6 §7) —
never a wrong parse. It gets there by being strict rather than clever:

* every phrase it understands is *consumed* from the sentence;
* if anything is left that is not filler (的、把、所有、网盘里 …), the
  sentence is not understood and :meth:`RulesTranslator.parse` returns
  ``None`` — "not mine", for a model backend to try;
* a sentence it understands but that cannot be acted on without guessing
  (「移到哪？」「多大算大？」) becomes a :class:`Clarification`.

Times and sizes follow fixed conventions, shown to the person in the plan:
sizes are binary (1GB = 1024³ bytes); 「最近 N 个月」 is N×30 days; a bare
date is that whole day in the configured time zone.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any

from pydantic import ValidationError

from ..rules.schema import CATEGORIES
from .query import Clarification, Query, as_result

_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}
NUM = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十]+)"


def number(text: str) -> float:
    """``12``, ``1.5``, ``三``, ``十五``, ``二十``, ``两`` → a number."""
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    if "十" in text:
        tens, _, ones = text.partition("十")
        return (_DIGITS.get(tens, 1) if tens else 1) * 10 + (_DIGITS.get(ones, 0) if ones else 0)
    value = 0
    for char in text:
        if char not in _DIGITS:
            raise ValueError(text)
        value = value * 10 + _DIGITS[char]
    return value


_UNITS = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "千": 1, "兆": 2, "吉": 3}
_SIZE_UNIT = r"(?:个)?\s*(k|m|g|t|兆|吉)(?:i?b)?(?![a-z])"


def size(amount: str, unit: str) -> int:
    return int(number(amount) * 1024 ** _UNITS[unit.lower()])


_KIND_WORDS = [
    ("video", "视频|影片|影视|电影|电视剧|剧集|动漫|番剧"),
    ("image", "图片|照片|相片|图像|截图|壁纸"),
    ("audio", "音频|音乐|歌曲|有声书"),
    ("document", "文档|电子书"),
    ("archive", "压缩包|压缩文件"),
    ("subtitle", "字幕"),
]
_EXTENSIONS = sorted({ext for _prefix, exts in CATEGORIES.values() for ext in exts},
                     key=len, reverse=True)

_WEEKDAYS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 0, "天": 0}

# Words that carry no meaning here once everything else has been read.
_FILLER = re.compile(
    "|".join(sorted([
        "请", "帮我", "帮忙", "麻烦", "给我", "我想", "我要", "一下", "吧", "吗", "呢", "啊", "呀",
        "把", "将", "对", "所有的", "所有", "全部的", "全部", "一切", "整个", "全",
        "网盘里面的", "网盘里面", "网盘里的", "网盘里", "网盘中的", "网盘中", "网盘上的", "网盘上",
        "网盘内", "网盘", "pikpak里的", "pikpak里", "pikpak上的", "pikpak", "云盘",
        "里面的", "里面", "里的", "中的", "目录下的", "下面的", "之内", "以内",
        "的", "文件夹", "文件", "东西", "内容", "资源", "那些", "这些", "些", "都", "和", "与",
        "及", "并且", "并", "然后", "再", "之后", "一共", "个", "有", "是", "到", "进", "去",
        "里", "中", "内", "所", "给", "它们", "他们", "掉", "了", "吗", "下来",
    ], key=len, reverse=True)),
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[\s,，。.、;；:：!！?？~～\-—]+")


@dataclass
class _State:
    text: str
    now: datetime
    tz: tzinfo
    intents: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=lambda: {
        "scope": {}, "filters": {}, "action_args": {}})
    ask: str | None = None
    conflict: bool = False
    ask_args: dict[str, Any] = field(default_factory=dict)
    new: bool = False
    period: str | None = None
    kinds: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)

    def take(self, pattern: str, handler=None, *, flags: int = re.IGNORECASE) -> list[re.Match]:
        """Find every match, hand it to ``handler``, blank it out of the text."""
        found = list(re.finditer(pattern, self.text, flags))
        for match in reversed(found):
            start, end = match.span()
            self.text = self.text[:start] + " " + self.text[end:]
        for match in found:
            if handler is not None:
                handler(match)
        return found

    def put(self, section: str, key: str, value: Any) -> None:
        """Set one field; a second, different value makes the sentence contradictory
        (「今天和昨天」, two folders), which is not ours to resolve."""
        target = self.data[section]
        if key in target and target[key] != value:
            self.conflict = True
        target[key] = value

    def put_schedule(self, value: dict[str, str]) -> None:
        if self.data.get("schedule") not in (None, value):
            self.conflict = True
        self.data["schedule"] = value

    def need(self, key: str, **args: Any) -> None:
        if self.ask is None:
            self.ask, self.ask_args = key, args

    @property
    def filters(self) -> dict[str, Any]:
        return self.data["filters"]

    def day(self, offset: int = 0) -> datetime:
        local = self.now.astimezone(self.tz)
        return local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


class RulesTranslator:
    name = "rules"

    async def translate(self, text: str, now: datetime, tz: tzinfo) -> Query | Clarification | None:
        return self.parse(text, now, tz)

    def parse(self, text: str, now: datetime, tz: tzinfo) -> Query | Clarification | None:
        # Matching ignores case; paths and names keep theirs.
        s = _State(text=unicodedata.normalize("NFKC", text).strip(), now=now, tz=tz)
        if not s.text:
            return None
        if re.search(r"永久删除|彻底删除|清空回收站|直接删除", s.text):
            return Clarification(question="nl.ask.forever")

        for step in (_tidy, _names, _paths, _schedule, _sizes, _dates, _relative_days,
                     _windows, _kinds, _extensions, _arrival, _intents, _scope_words):
            step(s)

        leftover = _PUNCT.sub("", _FILLER.sub("", s.text))
        if leftover or s.conflict:
            return None  # not understood, or contradictory: not ours to guess

        intents = [i for i in dict.fromkeys(s.intents) if i != "list"] or s.intents[:1]
        if len(intents) != 1:
            return None
        intent = intents[0]
        if intent == "organize":
            # 「整理一下 /Cos」: a top-level folder is organize-tree's unit.
            path = s.data["scope"].get("path")
            if path is None or path == "/":
                return Clarification(question="nl.ask.organize_how")
            if path.strip("/").casefold() in INBOXES:
                intent = "organize_inbox"
            elif path.count("/") == 1:
                intent = "organize_tree"
            else:
                return Clarification(question="nl.ask.organize_top", args={"path": path})

        if s.new:
            if s.period is None:
                s.need("nl.ask.new_since")
            elif "created_after" not in s.filters:
                s.filters["created_after"] = s.period
        if s.kinds:
            s.put("filters", "kinds", list(dict.fromkeys(s.kinds)))
        if s.extensions:
            s.put("filters", "extensions", list(dict.fromkeys(s.extensions)))

        data = dict(s.data, intent=intent)
        scope = data["scope"]
        if intent == "move" and not data["action_args"].get("dest"):
            s.need("nl.ask.move_where")
        if intent == "rename" and not data["action_args"].get("template"):
            s.need("nl.ask.rename_how")
        if intent == "archive" and "created_before" not in s.filters:
            s.need("nl.ask.archive_age")
        if (intent in ("trash", "download", "move", "rename") and not s.filters
                and scope.get("path", "/") == "/"):
            s.need(f"nl.ask.{intent}_all")
        if intent == "list" and data.get("schedule"):
            s.need("nl.ask.schedule_what")
        if s.ask is not None:
            return Clarification(question=s.ask, args=s.ask_args)
        try:
            return as_result(Query.model_validate(data))
        except (ValidationError, ValueError):
            return None


# ------------------------------------------------------------------ steps


INBOXES = ("telegram", "pack from shared")
"""The entry folders by name (docs/wms/M7 §4), for 「整理一下 Pack From Shared」."""

_INBOX_NAME = r"/?(?P<box>pack\s*from\s*shared|telegram)(?:\s*(?:目录|文件夹))?"


def _tidy(s: _State) -> None:
    """The M7 jobs by their everyday names. Runs first, so that 「大文件」 here
    is not taken as a size to ask about, nor 「删除重复」 as a plain delete."""

    def intent(name: str, **args: Any):
        def handler(match: re.Match) -> None:
            s.intents.append(name)
            for key, value in args.items():
                s.put("action_args", key, value)
            box = match.groupdict().get("box")
            if box:
                s.put("scope", "path", "/Pack From Shared" if "pack" in box.lower()
                      else "/Telegram")
        return handler

    s.take(r"(?:把\s*)?(?:网盘里的?|所有的?)?(?:重复(?:的)?(?:文件|副本)?"
           r"\s*(?:去掉|删掉|删除|清理掉?|清掉|去重)|(?:去除|删除|删掉|清理|清掉)\s*重复(?:的)?"
           r"(?:文件|副本)?|去重|查重)", intent("dedupe"))
    s.take(r"(?:看看|看一下|看下|列出|列一下|找出|查一下|显示)?\s*(?:网盘里)?"
           r"(?:(?:最大|最占空间|占空间最多)的?\s*(?:\d+\s*个)?\s*(?:文件|目录|文件夹)"
           r"(?:\s*(?:和|与)\s*(?:最大的?)?\s*(?:目录|文件夹))?|哪些(?:文件|东西)(?:最大|最占空间)"
           r"|大文件报告|空间占用报告|空间报告)", intent("big_report"))
    s.take(r"(?:把\s*)?大(?:文件|目录)(?:\s*(?:和|与)\s*大(?:文件|目录))?\s*(?:都)?\s*"
           r"(?:单独(?:放|存放|放到)?\s*(?:在)?\s*一起|单独(?:放|存放)|(?:放|挪|移|归)(?:到|在)?\s*一起"
           r"|集中(?:起来|放|存放)?|归集)", intent("organize_tree", part="big"))
    s.take(r"(?:整理|收拾|理)\s*(?:一下)?\s*(?:整个网盘|全网盘|全盘|所有目录|所有一级目录|一级目录|"
           r"各个目录|每个目录|散落的?文件)|全盘整理|归集散落的?文件|把散落的?文件归集(?:一下)?",
           intent("organize_tree"))
    s.take(r"(?:整理|收拾|上架)\s*(?:一下)?\s*(?:入口目录|入口|收件箱|inbox)|入口目录\s*(?:整理|上架)",
           intent("organize_inbox"))
    s.take(r"(?:整理|收拾|上架)\s*(?:一下)?\s*" + _INBOX_NAME, intent("organize_inbox"))
    s.take(_INBOX_NAME + r"\s*(?:整理|上架)\s*(?:一下)?", intent("organize_inbox"))


def _names(s: _State) -> None:
    quoted = r"[「“\"'『]([^」”\"'』]+)[」”\"'』]"

    def rename_to(match: re.Match) -> None:
        s.intents.append("rename")
        s.put("action_args", "template", match.group(1))

    s.take(r"(?:重命名|改名|命名)(?:为|成)\s*" + quoted, rename_to)
    bare = r"([0-9a-z一-鿿._\-\[\]()]+?)(?=的|，|,|。|\s|$|并|然后)"
    keyword = r"(?:文件名|名字|名称|标题)(?:里|中)?(?:包含|包括|含有|带有|含|带|有)\s*"

    def contains(match: re.Match) -> None:
        s.filters.setdefault("name_contains", []).append(match.group(1))

    s.take(keyword + quoted, contains)
    s.take(keyword + bare, contains)
    s.take(r"(?:叫|名为|名叫)\s*" + quoted, contains)

    starts = s.take(r"以\s*[「“\"'『]?([^」”\"'』\s]+?)[」”\"'』]?\s*开头")
    ends = s.take(r"以\s*[「“\"'『]?([^」”\"'』\s]+?)[」”\"'』]?\s*结尾")
    if starts or ends:
        head = "^" + re.escape(starts[0].group(1)) if starts else ""
        tail = re.escape(ends[0].group(1)) + "$" if ends else ""
        s.put("filters", "name_regex", head + (".*" if head and tail else "") + tail)


# Where a path written inside a sentence ends. 「下」 ends it only before a
# number, 的 or the end, so a folder such as /下载 survives.
_PATH_STOP = re.compile(
    r"[\s，,。；;、!！?？「」“”\"']|目录|文件夹|里面|里|中|下面|下的|之下|的"
    r"|下(?=$|[\s，,。的面0-9零〇一二两三四五六七八九十])"
    r"|移动到|移到|放到|挪到|转移到|搬到|放进|移进|归档到|到(?=\s*/)|并|然后|或者|或(?=\s*/)"
)
# A path that still holds an instruction word was probably two things run
# together (「/Media并删除图片」): decline rather than create that folder.
_PATH_SUSPECT = re.compile(r"和|与|及|或|再|删|移|下载|归档|分类|整理|重命名|改名|每天|每周|每月")
_DEST_BEFORE = re.compile(r"(移动到|移到|放到|挪到|转移到|搬到|放进|移进|归档到|到|至|进)\s*$")


def _paths(s: _State) -> None:
    index = 0
    while (start := s.text.find("/", index)) != -1:
        stop = _PATH_STOP.search(s.text, start + 1)
        end = stop.start() if stop else len(s.text)
        raw = s.text[start:end]
        before = s.text[:start]
        after = re.match(r"\s*(?:目录|文件夹)?\s*(?:里面|里|中|下面|之下|下)?\s*(?:的)?",
                         s.text[end:])
        consumed_to = end + (after.end() if after else 0)
        if _PATH_SUSPECT.search(raw):
            s.conflict = True
        is_dest = bool(_DEST_BEFORE.search(before))
        role_word = re.search(r"(在|从)\s*$", before)
        cut_from = start - (len(role_word.group(0)) if role_word and not is_dest else 0)
        if is_dest:
            s.put("action_args", "dest", raw)
        else:
            s.put("scope", "path", raw)
        s.text = s.text[:cut_from] + " " + s.text[consumed_to:]
        index = cut_from + 1


def _schedule(s: _State) -> None:
    def hour_of(match: re.Match, default: int) -> tuple[int, int]:
        part = match.group("part") or ""
        if match.group("hour"):
            hour = int(number(match.group("hour")))
            if part in ("下午", "晚上", "傍晚") and hour < 12:
                hour += 12
            return hour % 24, 30 if match.group("half") else 0
        return {"凌晨": 3, "早上": 8, "早晨": 8, "上午": 9, "中午": 12, "下午": 15,
                "傍晚": 18, "晚上": 22}.get(part, default), 0

    clock = (r"(?P<part>凌晨|早上|早晨|上午|中午|下午|傍晚|晚上)?\s*"
             r"(?:(?P<hour>" + NUM + r")\s*点(?P<half>半)?)?")

    def daily(match: re.Match) -> None:
        hour, minute = hour_of(match, 22 if match.group(1) == "每晚" else 3)
        s.put_schedule({"cron": f"{minute} {hour} * * *"})
        s.period = "1d"

    def weekly(match: re.Match) -> None:
        hour, minute = hour_of(match, 3)
        day = _WEEKDAYS.get(match.group("wd") or "一", 1)
        s.put_schedule({"cron": f"{minute} {hour} * * {day}"})
        s.period = "7d"

    def monthly(match: re.Match) -> None:
        hour, minute = hour_of(match, 3)
        day = int(number(match.group("md"))) if match.group("md") else 1
        s.put_schedule({"cron": f"{minute} {hour} {day} * *"})
        s.period = "31d"

    def hourly(_match: re.Match) -> None:
        s.put_schedule({"cron": "0 * * * *"})
        s.period = "1h"

    s.take(r"(每天|每日|每晚)\s*" + clock, daily)
    s.take(r"(?:每周|每星期|每个星期|每礼拜)(?P<wd>[一二三四五六日天])?\s*" + clock, weekly)
    s.take(r"(?:每月|每个月)(?:(?P<md>" + NUM + r")\s*[号日])?\s*" + clock, monthly)
    s.take(r"每(?:个)?小时|每隔一小时", hourly)


def _sizes(s: _State) -> None:
    def at_least(match: re.Match) -> None:
        s.put("filters", "min_size", size(match.group("n"), match.group("u")))

    def at_most(match: re.Match) -> None:
        s.put("filters", "max_size", size(match.group("n"), match.group("u")))

    def between(match: re.Match) -> None:
        unit = match.group("u2")
        s.put("filters", "min_size", size(match.group("n1"), match.group("u1") or unit))
        s.put("filters", "max_size", size(match.group("n2"), unit))

    amount = r"(?P<n>" + NUM + r")\s*" + _SIZE_UNIT.replace("(k|m", "(?P<u>k|m")
    s.take(r"(?:在|介于)?\s*(?P<n1>" + NUM + r")\s*(?:(?:个)?\s*(?P<u1>k|m|g|t|兆|吉)(?:i?b)?)?"
           r"\s*(?:到|至|-|~)\s*(?P<n2>" + NUM + r")\s*(?:个)?\s*(?P<u2>k|m|g|t|兆|吉)(?:i?b)?"
           r"(?![a-z])\s*(?:之间)?", between)
    s.take(r"(?:大于|超过|多于|高于|不小于|至少|>=|≥|>)\s*" + amount, at_least)
    s.take(amount + r"\s*(?:以上|及以上|或以上|或更大|起)", at_least)
    s.take(r"(?:小于|不到|少于|低于|不超过|不大于|至多|最多|<=|≤|<)\s*" + amount, at_most)
    s.take(amount + r"\s*(?:以下|及以下|或以下|以内|或更小)", at_most)
    if s.take(r"大文件|小文件|很大的|很小的|比较大|比较小"):
        s.need("nl.ask.size")


def _date(s: _State, match: re.Match, prefix: str) -> datetime:
    year = match.group(prefix + "y")
    local_now = s.now.astimezone(s.tz)
    return datetime(int(year) if year else local_now.year, int(match.group(prefix + "m")),
                    int(match.group(prefix + "d")), tzinfo=s.tz)


def _dates(s: _State) -> None:
    def date(p: str) -> str:
        return (rf"(?:(?P<{p}y>\d{{4}})\s*[-/.年]\s*)?(?P<{p}m>\d{{1,2}})\s*[-/.月]\s*"
                rf"(?P<{p}d>\d{{1,2}})\s*[日号]?")

    def span(match: re.Match) -> None:
        s.put("filters", "created_after", _iso(_date(s, match, "a")))
        s.put("filters", "created_before", _iso(_date(s, match, "b") + timedelta(days=1)))

    def since(match: re.Match) -> None:
        s.put("filters", "created_after", _iso(_date(s, match, "a")))

    def until(match: re.Match) -> None:
        s.put("filters", "created_before", _iso(_date(s, match, "a")))

    def one_day(match: re.Match) -> None:
        start = _date(s, match, "a")
        s.put("filters", "created_after", _iso(start))
        s.put("filters", "created_before", _iso(start + timedelta(days=1)))

    try:
        s.take(r"(?:从)?\s*" + date("a") + r"\s*(?:到|至|~|—)\s*" + date("b")
               + r"\s*(?:之间|为止)?", span)
        s.take(date("a") + r"\s*(?:之后|以后|以来|起|开始)", since)
        s.take(date("a") + r"\s*(?:之前|以前)", until)
        s.take(r"(?:在\s*)?" + date("a") + r"\s*(?:当天)?", one_day)
    except ValueError:  # 2月30日
        s.need("nl.ask.date")


def _relative_days(s: _State) -> None:
    def days(after: int, before: int | None):
        def handler(_match: re.Match) -> None:
            s.put("filters", "created_after", _iso(s.day(after)))
            if before is not None:
                s.put("filters", "created_before", _iso(s.day(before)))
        return handler

    local = s.now.astimezone(s.tz)
    weekday = local.weekday()  # Monday 0
    month_start = s.day(0).replace(day=1)
    previous_month = (month_start - timedelta(days=1)).replace(day=1)
    year_start = s.day(0).replace(month=1, day=1)

    def fixed(after: datetime, before: datetime | None):
        def handler(_match: re.Match) -> None:
            s.put("filters", "created_after", _iso(after))
            if before is not None:
                s.put("filters", "created_before", _iso(before))
        return handler

    s.take(r"今天|今日", days(0, None))
    s.take(r"昨天|昨日", days(-1, 0))
    s.take(r"前天", days(-2, -1))
    s.take(r"本周|这周|这个星期|本星期|这星期", days(-weekday, None))
    s.take(r"上周|上个星期|上星期|上礼拜", days(-weekday - 7, -weekday))
    s.take(r"本月|这个月|当月", fixed(month_start, None))
    s.take(r"上个月|上月", fixed(previous_month, month_start))
    s.take(r"今年|本年", fixed(year_start, None))
    s.take(r"去年", fixed(year_start.replace(year=year_start.year - 1), year_start))


_SPAN_UNITS = {"天": "d", "日": "d", "周": "w", "星期": "w", "个星期": "w", "礼拜": "w",
               "个月": "m", "月": "m", "小时": "h", "个小时": "h", "年": "y"}
_SPAN = r"(?P<n>" + NUM + r")\s*(?P<u>个星期|个月|个小时|星期|礼拜|小时|天|日|周|月|年)"


def _duration(match: re.Match) -> str:
    amount = int(number(match.group("n")))
    unit = _SPAN_UNITS[match.group("u")]
    if unit == "m":
        return f"{amount * 30}d"
    if unit == "y":
        return f"{amount * 365}d"
    return f"{amount}{unit}"


def _windows(s: _State) -> None:
    def within(match: re.Match) -> None:
        s.put("filters", "created_after", _duration(match))

    def older(match: re.Match) -> None:
        s.put("filters", "created_before", _duration(match))

    s.take(r"(?:最近|近|过去|这)\s*" + _SPAN + r"\s*(?:内|以内|之内|里|来)?", within)
    s.take(_SPAN + r"\s*(?:内|以内|之内)(?:的)?", within)
    s.take(r"(?:超过|多于|大于|早于)\s*" + _SPAN + r"\s*(?:前|以前|之前)?", older)
    s.take(_SPAN + r"\s*(?:前|以前|之前)", older)
    if s.take(r"旧文件|老文件|很久以前|以前的|很久没") and "created_before" not in s.filters:
        s.need("nl.ask.old_age")


def _kinds(s: _State) -> None:
    for kind, words in _KIND_WORDS:
        if s.take(words):
            s.kinds.append(kind)


def _extensions(s: _State) -> None:
    pattern = (r"(?<![0-9a-z])\.?(" + "|".join(_EXTENSIONS) + r")(?![0-9a-z])"
               r"\s*(?:格式|后缀|类型|扩展名)?")
    for match in s.take(pattern):
        s.extensions.append(match.group(1).lower())


def _arrival(s: _State) -> None:
    # Words that say "arrived in the drive": the created time, nothing to add.
    s.take(r"新(?:下载|转存|入库|增加?|加入?|上传|存入?|进)?(?:的)?", lambda _m: _set_new(s))
    s.take(r"(?:离线下载|转存|入库|存进|存入|保存到网盘|保存进网盘|添加|加入|上传|下载到网盘|"
           r"下载进网盘|进网盘|放进网盘)(?:到网盘|进网盘|到云盘)?(?:的)?")
    # 「昨天下载的图片」: 下载的 describes when files arrived, it is not a command.
    s.take(r"下载(?:好|完)?的")


def _set_new(s: _State) -> None:
    s.new = True


def _intents(s: _State) -> None:
    def add(intent: str):
        return lambda _m: s.intents.append(intent)

    s.take(r"(?:放进|丢进|移到|扔进|扔到|放到|移进|挪到)\s*回收站|删除|删掉|删了|删去|清理掉|清掉|清理|扔掉|丢掉",
           add("trash"))
    s.take(r"按(?:文件)?(?:类型|种类|格式)\s*(?:分类|归类|整理|归档|分好|放好|放|分开)?|分类|归类",
           add("classify"))
    s.take(r"归档", add("archive"))
    s.take(r"(?:下载|下|拉|取回|保存|存)(?:到|回)?\s*(?:本地|nas|媒体目录|电脑)|下载|取回|出库",
           add("download"))
    s.take(r"移动|移到|挪到|转移到|转移|放到|搬到|挪", add("move"))
    s.take(r"重命名|改名(?:为|成)?", add("rename"))
    s.take(r"整理", add("organize"))
    s.take(r"列出|列一下|列个|看看|看一下|有哪些|有什么|找出|找一下|找找|查找|查一下|显示|统计|多少个|多少|哪些",
           add("list"))


def _scope_words(s: _State) -> None:
    def root(_match: re.Match) -> None:
        s.data["scope"].setdefault("path", "/")

    s.take(r"整个网盘|全网盘|全盘|所有目录|网盘根目录|根目录|所有文件夹", root)
