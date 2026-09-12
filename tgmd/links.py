"""Parsing of the link forms a user can send to the bot.

Everything in this module is pure: no network, no Telegram client. That keeps
the messy part of the project (there are a lot of link shapes in the wild)
directly unit-testable.

Supported Telegram message links::

    https://t.me/channelname/123              public channel or group
    https://t.me/channelname/12/123           message 123 in forum topic 12
    https://t.me/s/channelname/123            web-preview form
    https://t.me/c/1234567890/123             private chat, internal id
    https://t.me/c/1234567890/12/123          private forum topic
    https://t.me/channelname/100-120          inclusive range of messages
    https://t.me/channelname/123?single       one album item instead of the album
    https://t.me/channelname/123?comment=45   comment 45 under post 123
    https://t.me/+AbCdEf...                   invite link (no message)
    https://t.me/joinchat/AbCdEf...           legacy invite link
    tg://resolve?domain=channelname&post=123
    tg://privatepost?channel=1234567890&post=123

Also recognised, for PikPak transfers that need no Telegram download at all:
magnet links, plain HTTP(S) URLs and PikPak share links.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import parse_qs, unquote, urlparse

# Hard ceiling on how many message ids a single range link may expand to. The
# queue layer applies the (much smaller) configured limit on top of this; this
# one only exists so a hostile "t.me/x/1-999999999" cannot exhaust memory.
MAX_RANGE_SPAN = 10_000

TELEGRAM_HOSTS = frozenset(
    {"t.me", "www.t.me", "telegram.me", "www.telegram.me", "telegram.dog"}
)

PIKPAK_HOSTS = frozenset({"mypikpak.com", "www.mypikpak.com", "mypikpak.net"})

# First path segment values that are not chat usernames.
RESERVED_PATHS = frozenset(
    {
        "a",
        "addemoji",
        "addlist",
        "addstickers",
        "addtheme",
        "bg",
        "boost",
        "confirmphone",
        "contact",
        "giftcode",
        "invoice",
        "iv",
        "k",
        "login",
        "m",
        "nft",
        "proxy",
        "setlanguage",
        "share",
        "socks",
        "web",
    }
)

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,31}$")
_RANGE_RE = re.compile(r"^(\d{1,12})\s*[-~]\s*(\d{1,12})$")
_INVITE_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

MAGNET_RE = re.compile(r"magnet:\?xt=urn:[a-z0-9]+:[^\s<>\"]+", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
# Bare t.me/... and tg:// links, which URL_RE would miss.
_BARE_TG_RE = re.compile(
    r"(?:(?<=^)|(?<=[\s,;()\[\]]))"
    r"(?:(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>\"]+|tg://[^\s<>\"]+)",
    re.IGNORECASE,
)


class LinkError(ValueError):
    """The text looked like a Telegram link but cannot be acted on."""


@dataclass(frozen=True)
class MessageRef:
    """A resolved pointer to one or more Telegram messages."""

    chat: str | int
    """Username for public chats, internal numeric id for ``t.me/c`` links."""

    ids: tuple[int, ...] = ()
    """Target message ids. Empty for invite-only links."""

    topic_id: int | None = None
    """Forum topic the message lives in, when the link carried one."""

    comment_id: int | None = None
    """Comment id from ``?comment=``, resolved in the discussion group."""

    invite_hash: str | None = None
    """Invite hash from ``t.me/+hash``, used to join before reading."""

    single: bool = False
    """``?single`` was present: deliver only this item, not the whole album."""

    raw: str = ""
    """The original text the reference was parsed from."""

    @property
    def is_private(self) -> bool:
        """True when the chat is identified by internal id rather than username."""
        return isinstance(self.chat, int)

    @property
    def is_invite_only(self) -> bool:
        """True for invite links that do not point at any message."""
        return not self.ids and self.invite_hash is not None

    def with_id(self, message_id: int) -> MessageRef:
        """Return a copy narrowed to a single message id."""
        return MessageRef(
            chat=self.chat,
            ids=(message_id,),
            topic_id=self.topic_id,
            comment_id=self.comment_id,
            invite_hash=self.invite_hash,
            single=self.single,
            raw=self.raw,
        )

    def describe(self) -> str:
        """Short human label, used in progress and log messages."""
        chat = f"c/{self.chat}" if self.is_private else f"@{self.chat}"
        if not self.ids:
            return chat
        if len(self.ids) == 1:
            return f"{chat}/{self.ids[0]}"
        return f"{chat}/{self.ids[0]}-{self.ids[-1]}"


@dataclass
class LinkBundle:
    """Everything actionable found in one incoming message."""

    messages: list[MessageRef] = field(default_factory=list)
    magnets: list[str] = field(default_factory=list)
    pikpak_shares: list[str] = field(default_factory=list)
    direct_urls: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.messages or self.magnets or self.pikpak_shares or self.direct_urls)

    @property
    def total(self) -> int:
        """Number of actionable items, counting each message id separately."""
        return (
            sum(max(len(ref.ids), 1) for ref in self.messages)
            + len(self.magnets)
            + len(self.pikpak_shares)
            + len(self.direct_urls)
        )


def normalize_chat_id(value: str | int) -> int:
    """Turn any spelling of a channel id into its internal (positive) form.

    Telegram links use the internal id (``t.me/c/1234567890``) while the API
    and most tooling use the ``-100``-prefixed form. Both are accepted here.
    """
    text = str(value).strip()
    negative = text.startswith("-")
    digits = text.lstrip("-")
    if not digits.isdigit():
        raise LinkError(f"not a numeric chat id: {value!r}")
    if negative and digits.startswith("100"):
        digits = digits[3:]
    if not digits:
        raise LinkError(f"not a numeric chat id: {value!r}")
    return int(digits)


def to_peer_id(internal_id: int) -> int:
    """Return the ``-100``-prefixed id matching an internal channel id."""
    return int(f"-100{internal_id}")


def _parse_ids(segment: str) -> tuple[int, ...] | None:
    """Parse a trailing path segment into message ids, honouring ranges."""
    segment = segment.strip()
    if segment.isdigit():
        return (int(segment),)
    match = _RANGE_RE.match(segment)
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    if start > end:
        start, end = end, start
    if end - start + 1 > MAX_RANGE_SPAN:
        raise LinkError(
            f"range {start}-{end} covers too many messages "
            f"(limit {MAX_RANGE_SPAN})"
        )
    return tuple(range(start, end + 1))


def _query_flags(query: str) -> tuple[bool, int | None, int | None]:
    """Extract ``single``, ``comment`` and ``thread`` from a link query string."""
    params = parse_qs(query, keep_blank_values=True)
    single = "single" in params
    comment = None
    thread = None
    if "comment" in params and params["comment"][0].isdigit():
        comment = int(params["comment"][0])
    for key in ("thread", "topic"):
        if key in params and params[key][0].isdigit():
            thread = int(params[key][0])
            break
    return single, comment, thread


def _parse_tg_scheme(url: str) -> MessageRef | None:
    """Parse the ``tg://`` deep-link forms that point at a message."""
    parsed = urlparse(url)
    action = (parsed.netloc or parsed.path.lstrip("/")).lower()
    params = parse_qs(parsed.query, keep_blank_values=True)

    def first(name: str) -> str | None:
        values = params.get(name)
        return values[0] if values else None

    post = first("post") or first("message_id")
    single, comment, thread = _query_flags(parsed.query)

    if action == "resolve":
        domain = first("domain")
        if not domain or not _USERNAME_RE.match(domain):
            return None
        ids = _parse_ids(post) if post else None
        return MessageRef(
            chat=domain,
            ids=ids or (),
            topic_id=thread,
            comment_id=comment,
            single=single,
            raw=url,
        )

    if action == "privatepost":
        channel = first("channel")
        if not channel or not post:
            return None
        ids = _parse_ids(post)
        if not ids:
            return None
        return MessageRef(
            chat=normalize_chat_id(channel),
            ids=ids,
            topic_id=thread,
            comment_id=comment,
            single=single,
            raw=url,
        )

    return None


def parse_message_link(text: str) -> MessageRef | None:
    """Parse one Telegram link into a :class:`MessageRef`.

    Returns ``None`` when the text is not a Telegram link at all. Raises
    :class:`LinkError` when it is one but cannot be used (an unsupported form,
    or a range that is far too wide).
    """
    text = text.strip().strip("<>").rstrip(".,;)")
    if not text:
        return None

    if text.lower().startswith("tg://"):
        return _parse_tg_scheme(text)

    candidate = text if "//" in text else f"https://{text}"
    parsed = urlparse(candidate)
    if parsed.hostname is None or parsed.hostname.lower() not in TELEGRAM_HOSTS:
        return None

    parts = [unquote(p) for p in parsed.path.split("/") if p]
    if not parts:
        raise LinkError("the link has no path, so it points at no chat")

    single, comment, thread = _query_flags(parsed.query)

    # Invite links: t.me/+hash and t.me/joinchat/hash.
    invite_hash: str | None = None
    if parts[0] == "joinchat" and len(parts) >= 2:
        invite_hash = parts[1]
        parts = parts[2:]
    elif parts[0].startswith("+"):
        invite_hash = parts[0][1:]
        parts = parts[1:]

    if invite_hash is not None:
        if invite_hash.isdigit():
            raise LinkError(
                "that is a phone-number link, not an invite link"
            )
        if not _INVITE_HASH_RE.match(invite_hash):
            raise LinkError(f"malformed invite hash: {invite_hash!r}")
        ids = _parse_ids(parts[-1]) if parts else None
        return MessageRef(
            chat=0,
            ids=ids or (),
            topic_id=thread,
            comment_id=comment,
            invite_hash=invite_hash,
            single=single,
            raw=text,
        )

    # Web-preview prefix: t.me/s/channel/123
    if parts[0] == "s" and len(parts) >= 2:
        parts = parts[1:]

    if parts[0] == "c":
        if len(parts) < 3:
            raise LinkError(
                "a t.me/c link needs both a chat id and a message id"
            )
        chat: str | int = normalize_chat_id(parts[1])
        rest = parts[2:]
    else:
        username = parts[0]
        if username.lower() in RESERVED_PATHS:
            raise LinkError(f"t.me/{username} is not a chat link")
        if not _USERNAME_RE.match(username):
            raise LinkError(f"{username!r} is not a valid Telegram username")
        chat = username
        rest = parts[1:]

    if not rest:
        raise LinkError(
            f"{parse_message_link_target(chat)} has no message id in the link"
        )

    ids = _parse_ids(rest[-1])
    if ids is None:
        raise LinkError(f"{rest[-1]!r} is not a message id or range")

    topic_id = thread
    if len(rest) >= 2:
        topic_segment = _parse_ids(rest[-2])
        if topic_segment and len(topic_segment) == 1:
            topic_id = topic_segment[0]

    return MessageRef(
        chat=chat,
        ids=ids,
        topic_id=topic_id,
        comment_id=comment,
        single=single,
        raw=text,
    )


def parse_message_link_target(chat: str | int) -> str:
    """Render a chat identifier the way a user would recognise it."""
    return f"c/{chat}" if isinstance(chat, int) else f"@{chat}"


def is_pikpak_share(url: str) -> bool:
    """True for a PikPak share link, which can be saved without downloading."""
    parsed = urlparse(url if "//" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    return host in PIKPAK_HOSTS and "/s/" in parsed.path


def iter_candidates(text: str) -> Iterable[str]:
    """Yield every URL-ish token in a block of text, longest matches first."""
    seen: set[str] = set()
    for pattern in (MAGNET_RE, URL_RE, _BARE_TG_RE):
        for match in pattern.finditer(text):
            token = match.group(0).rstrip(".,;)")
            if token not in seen:
                seen.add(token)
                yield token


def extract_links(text: str) -> LinkBundle:
    """Classify everything actionable in an incoming message.

    Telegram message links, magnet links, PikPak share links and plain HTTP
    URLs are separated out; anything that looks like a Telegram link but is
    unusable is reported in :attr:`LinkBundle.errors`.
    """
    bundle = LinkBundle()
    if not text:
        return bundle

    seen_refs: set[tuple] = set()
    for token in iter_candidates(text):
        lowered = token.lower()

        if lowered.startswith("magnet:"):
            if token not in bundle.magnets:
                bundle.magnets.append(token)
            continue

        try:
            ref = parse_message_link(token)
        except LinkError as exc:
            bundle.errors.append(f"{token} — {exc}")
            continue

        if ref is not None:
            key = (ref.chat, ref.ids, ref.topic_id, ref.comment_id, ref.invite_hash)
            if key not in seen_refs:
                seen_refs.add(key)
                bundle.messages.append(ref)
            continue

        if not lowered.startswith(("http://", "https://")):
            continue

        if is_pikpak_share(token):
            if token not in bundle.pikpak_shares:
                bundle.pikpak_shares.append(token)
        elif token not in bundle.direct_urls:
            bundle.direct_urls.append(token)

    return bundle
