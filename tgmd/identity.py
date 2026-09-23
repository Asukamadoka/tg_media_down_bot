"""Identity primitives and the check-report model used by verification.

Two ideas here are worth stating plainly, because the whole verification
command rests on them:

* A BotFather token is ``<bot id>:<secret>``. The digits before the colon are
  the bot's own Telegram user id, so a token can be checked for shape offline
  and then cross-checked against what ``get_me()`` reports. If those two
  disagree, the token does not belong to the bot that answered.
* The bot account and the reading account must be different accounts. The bot
  talks to users; the user account reads history. Confusing the two is the
  most common way to end up with a bot that cannot download anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .utils import escape_html

_TOKEN_RE = re.compile(r"^(?P<bot_id>\d{5,16}):(?P<secret>[A-Za-z0-9_-]{30,64})$")


class BotTokenError(ValueError):
    """The bot token is not shaped like one BotFather issues."""


@dataclass(frozen=True)
class BotToken:
    """A parsed BotFather token."""

    bot_id: int
    secret: str

    @property
    def redacted(self) -> str:
        """The token as it is safe to print: id, then a stub of the secret."""
        return f"{self.bot_id}:{self.secret[:4]}…{self.secret[-2:]}"


def parse_bot_token(token: str) -> BotToken:
    """Parse and validate a bot token without contacting Telegram.

    Raises :class:`BotTokenError` with a message that says what is wrong,
    since a mistyped token is by far the most common setup failure.
    """
    candidate = (token or "").strip()
    if not candidate:
        raise BotTokenError("the bot token is empty; get one from @BotFather")
    if candidate.count(":") != 1:
        raise BotTokenError(
            "a bot token looks like 123456789:AA... — one colon, id first"
        )

    match = _TOKEN_RE.match(candidate)
    if match is None:
        bot_id, _, secret = candidate.partition(":")
        if not bot_id.isdigit():
            raise BotTokenError(
                f"the part before the colon should be the numeric bot id, got {bot_id!r}"
            )
        raise BotTokenError(
            f"the secret after the colon is {len(secret)} characters; "
            "BotFather issues about 35"
        )

    return BotToken(bot_id=int(match.group("bot_id")), secret=match.group("secret"))


def describe_account(entity) -> str:
    """A one-line human label for a Telegram account."""
    if entity is None:
        return "unknown"
    username = getattr(entity, "username", None)
    if username:
        name = f"@{username}"
    else:
        parts = [
            getattr(entity, "first_name", None) or "",
            getattr(entity, "last_name", None) or "",
        ]
        name = " ".join(part for part in parts if part).strip() or "unnamed"
    return f"{name} (id {getattr(entity, 'id', '?')})"


def account_link(entity) -> str | None:
    """A ``t.me`` link for an account that has a username."""
    username = getattr(entity, "username", None)
    return f"https://t.me/{username}" if username else None


class Status(Enum):
    """Outcome of a single check."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"

    @property
    def symbol(self) -> str:
        return {"ok": "✓", "warn": "!", "fail": "✗", "skip": "-"}[self.value]

    @property
    def emoji(self) -> str:
        return {"ok": "✅", "warn": "⚠️", "fail": "❌", "skip": "⏭"}[self.value]


@dataclass
class Check:
    """One verification result."""

    name: str
    status: Status
    detail: str = ""

    @classmethod
    def ok(cls, name: str, detail: str = "") -> Check:
        return cls(name, Status.OK, detail)

    @classmethod
    def warn(cls, name: str, detail: str = "") -> Check:
        return cls(name, Status.WARN, detail)

    @classmethod
    def fail(cls, name: str, detail: str = "") -> Check:
        return cls(name, Status.FAIL, detail)

    @classmethod
    def skip(cls, name: str, detail: str = "") -> Check:
        return cls(name, Status.SKIP, detail)


@dataclass
class Report:
    """A collection of checks, with an overall verdict."""

    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    def count(self, status: Status) -> int:
        return sum(1 for check in self.checks if check.status is status)

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if check.status is Status.FAIL]

    @property
    def ok(self) -> bool:
        """True when nothing failed. Warnings do not block startup."""
        return not self.failed

    def verdict(self) -> str:
        """One sentence summarising the run."""
        if self.failed:
            return f"{len(self.failed)} check(s) failed — the bot will not work as configured"
        warnings = self.count(Status.WARN)
        if warnings:
            return f"everything essential passed, with {warnings} warning(s)"
        return "everything passed"

    def render_text(self) -> str:
        """Aligned plain-text report for a terminal."""
        if not self.checks:
            return "no checks ran"
        width = max(len(check.name) for check in self.checks)
        lines = [
            f"{check.status.symbol} {check.name.ljust(width)}  {check.detail}".rstrip()
            for check in self.checks
        ]
        lines.append("")
        lines.append(self.verdict())
        return "\n".join(lines)

    def render_html(self) -> str:
        """Telegram-flavoured HTML, for the in-chat /verify command."""
        lines = [
            f"{check.status.emoji} <b>{escape_html(check.name)}</b>"
            + (f"\n    <code>{escape_html(check.detail)}</code>" if check.detail else "")
            for check in self.checks
        ]
        return "\n".join(lines) + f"\n\n<i>{escape_html(self.verdict())}</i>"
