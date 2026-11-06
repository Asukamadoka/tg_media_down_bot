"""「X 下过了」「不要 X」「还没下载过的」: what a sentence says about files already on the NAS
(docs/wms/M9.2 §C).

Three things are read here, by words, before anything else looks at the sentence:

* **not downloaded yet** (「未下载」「还没下」「没下载过」「还未下载过」「相对 NAS 新的」): the
  query leaves out what the download log knows;
* **already downloaded** (「X 下过了」「X 已经下了」「X 已下载」): X is left out of the request
  and remembered in the log;
* **leave it out** (「不要 X」「除了 X」「跳过 X」「X 不用」): X is left out.

Inside a request they are lifted out of the sentence and the main intent stays. On their
own, with nothing else to do, they are a :class:`~pikpak_wms.nl.query.Remark`, which can
never become a download plan (the safety guard of docs/wms/M9.2 §C.2): the plan the bot
once made from 「abcd00123 下过了」 was a download of exactly abcd00123.

A model's answer goes through :func:`apply_remarks` as well, because a small model reads
「abcd00123 下过了」 as a request to fetch abcd00123.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .query import Prioritize, Query, Remark

# --------------------------------------------------------------------- names

_ASCII = r"(?<![0-9A-Za-z._\-])[0-9A-Za-z][0-9A-Za-z._\-\[\]()@]+"
_CJK = (r"(?<![0-9A-Za-z一-鿿._\-])"
        r"[0-9A-Za-z一-鿿._\-\[\]()@]{2,}")
_QUOTED = r"[「“\"'『][^」”\"'』]+[」”\"'』]"
_NAME = rf"(?:{_QUOTED}|{_ASCII}|{_CJK})"
_NAMES = rf"{_NAME}(?:\s*(?:、|和|及|与)\s*{_NAME})*"
_SEPARATOR = re.compile(r"\s*(?:、|和|及|与)\s*")

# 「下过了」「下载过了」「已经下了」「已下载」「下好了」: said to be on the NAS already.
_DONE = (r"(?:(?:已经|已|早就|都)\s*(?:下载|下)(?:过|好|完)?了?"
         r"|(?:下载|下)\s*(?:过|好|完)\s*了?)")
# 「不用」「不需要」「无需下载」: not wanted.
_NOT_WANTED = r"(?:不用|不需要|不必|无需)\s*(?:再)?(?:下载|下)?\s*了?"
_LEAVE_OUT = (r"(?:不要\s*(?:再)?\s*(?:下载|下)?|除了|除开|跳过|略过|排除|别下载|别下"
              r"|不下载|不下)\s*")
_TAIL = r"(?:\s*(?:以外|之外|除外|外))?(?:\s*的)?"

_AFTER_DONE = re.compile(rf"(?P<names>{_NAMES})\s*{_DONE}", re.IGNORECASE)
_AFTER_WANTED = re.compile(rf"(?P<names>{_NAMES})\s*{_NOT_WANTED}", re.IGNORECASE)
_BEFORE = re.compile(rf"{_LEAVE_OUT}(?P<names>{_NAMES}){_TAIL}", re.IGNORECASE)

# 「未下载」「还没下」「没下载过」「还未下载过的」「相对 NAS 新的」「NAS 上没有的」.
_NOT_DOWNLOADED = re.compile(
    r"(?:还|尚|都|仍)?\s*(?:未|没有|没)\s*(?:被)?(?:下载|下)(?:过|到|好)?(?:的)?"
    r"|相对\s*nas\s*(?:来说)?(?:新|没有)(?:的)?|nas\s*上\s*(?:还)?\s*(?:没有|没)(?:的)?",
    re.IGNORECASE,
)

_REFERENCE = frozenset({
    "这个", "那个", "这些", "那些", "这几个", "那几个", "它们", "他们", "上面", "前面", "刚才",
    "之前", "所有", "全部", "都", "也", "已经", "还", "我", "你",
})
_GENERIC = re.compile(
    r"视频|影片|电影|电视剧|剧集|图片|照片|文件|文档|压缩包|音频|字幕|重复|今天|昨天|最新|"
    r"全部|所有|的|个|些|吗|呢|吧|删除|删掉|下载|移动|归档|分类|整理|清理|重命名|改名|去重|取回|出库|转移")


def _clean(name: str) -> str | None:
    """One name as it was written (without quotes), or None for a word that only points
    at something (「那些」) or describes it (「示例影像的视频」): those are not names."""
    quoted = re.fullmatch(rf"{_QUOTED}", name)
    text = name[1:-1].strip() if quoted else name.strip()
    if not text:
        return None
    if not quoted:
        if text in _REFERENCE:
            return None
        if re.search(r"[一-鿿]", text) and _GENERIC.search(text):
            return None
    return text


def _split(raw: str) -> list[str]:
    names = []
    for part in _SEPARATOR.split(raw):
        cleaned = _clean(part)
        if cleaned is not None:
            names.append(cleaned)
    return names


@dataclass
class Lifted:
    text: str
    """The sentence with the phrases blanked out."""
    exclude: list[str] = field(default_factory=list)
    downloaded: list[str] = field(default_factory=list)
    """The names (a subset of ``exclude``) that were said to be downloaded already."""
    not_downloaded: bool = False

    @property
    def found(self) -> bool:
        return bool(self.exclude or self.not_downloaded)


def lift(text: str) -> Lifted:
    """Read and remove the phrases about files already on the NAS."""
    lifted = Lifted(text=unicodedata.normalize("NFKC", text))

    def blank(spans: list[tuple[int, int]]) -> None:
        for start, end in reversed(spans):
            lifted.text = lifted.text[:start] + " " + lifted.text[end:]

    def take(pattern: re.Pattern[str], *, downloaded: bool) -> None:
        found = []
        for match in pattern.finditer(lifted.text):
            names = _split(match.group("names"))
            if names:
                found.append((match.span(), names))
        blank([span for span, _names in found])
        for _span, names in found:
            for name in names:
                if name.casefold() not in {n.casefold() for n in lifted.exclude}:
                    lifted.exclude.append(name)
                if downloaded and name.casefold() not in {n.casefold() for n in lifted.downloaded}:
                    lifted.downloaded.append(name)

    # What it is not first: 「还未下载过」 must not be read as 「还未 + 下载过」.
    found = [m.span() for m in _NOT_DOWNLOADED.finditer(lifted.text)]
    blank(found)
    lifted.not_downloaded = bool(found)
    take(_BEFORE, downloaded=False)
    take(_AFTER_DONE, downloaded=True)
    take(_AFTER_WANTED, downloaded=False)
    return lifted


# What is left of a sentence once the phrases are gone, when it holds nothing to do.
_PUNCTUATION = re.compile(r"[\s,，。.、;；:：!！?？~～\-—]+")
_SPARE_WORDS = re.compile(
    r"请|帮我|麻烦|那就|那|好|嗯|哦|对|还有|另外|以及|而且|再|也|都|了|的|吧|啊|呀|呢|就|给我|我")


def only_remark(text: str) -> Remark | None:
    """The whole sentence is just a remark about files, nothing to carry out."""
    lifted = lift(text)
    if not lifted.exclude or lifted.not_downloaded:
        return None
    if _SPARE_WORDS.sub("", _PUNCTUATION.sub("", lifted.text)):
        return None
    return Remark(names=lifted.exclude, downloaded=bool(lifted.downloaded))


# --------------------------------------------------------------- priority

_PRIORITY_VERB = r"(?:先\s*下载|先\s*下|优先\s*下载|优先\s*下|优先)"
_TOP_VERB = r"置顶"
_PRIORITY_BEFORE = re.compile(rf"(?:把\s*)?{_PRIORITY_VERB}\s*(?P<names>{_NAMES})", re.IGNORECASE)
_TOP_BEFORE = re.compile(rf"(?:把\s*)?{_TOP_VERB}\s*(?P<names>{_NAMES})", re.IGNORECASE)
_TOP_AFTER = re.compile(rf"(?:把\s*)?(?P<names>{_NAMES})\s*(?:给\s*)?{_TOP_VERB}", re.IGNORECASE)


def only_priority(text: str) -> Prioritize | None:
    """「先下 X」「优先下载 X」「X 置顶」 on their own: files to move up the line. They
    never become a plan (a sentence that names a file and says 下载 must not fetch it)."""
    text = unicodedata.normalize("NFKC", text)
    for pattern, level in ((_TOP_AFTER, "top"), (_TOP_BEFORE, "top"),
                           (_PRIORITY_BEFORE, "high")):
        match = pattern.search(text)
        if match is None:
            continue
        names = _split(match.group("names"))
        rest = text[:match.start()] + " " + text[match.end():]
        if names and not _SPARE_WORDS.sub("", _PUNCTUATION.sub("", rest)):
            return Prioritize(names=names, level=level)
    return None


# ------------------------------------------------------------ a model's answer


def _literal(pattern: str) -> str:
    """A name regex a model wrote for a plain name (``(?i)abcd00123``) as the text."""
    text = re.sub(r"^\(\?i\)", "", pattern.strip())
    return re.sub(r"\\(.)", r"\1", text)


def apply_remarks(sentence: str, query: Query) -> Query:
    """A model's Query, made to say what the words say about files already on the NAS.

    The model's usual mistake is to turn 「X 下过了」 into a filter that selects X; here X
    is taken back out of the name conditions and becomes an exclusion instead."""
    lifted = lift(sentence)
    if not lifted.found:
        return query
    filters = query.filters
    if lifted.not_downloaded:
        filters.not_downloaded = True
    skip = {name.casefold() for name in lifted.exclude}
    filters.name_contains = [n for n in filters.name_contains if n.casefold() not in skip]
    if filters.name_regex and _literal(filters.name_regex).casefold() in skip:
        filters.name_regex = None
    for name in lifted.exclude:
        if name.casefold() not in {n.casefold() for n in filters.exclude_names}:
            filters.exclude_names.append(name)
    query.marked = list(dict.fromkeys([*query.marked, *lifted.downloaded]))
    return query
