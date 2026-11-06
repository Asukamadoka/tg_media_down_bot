"""Validation of Telegram Mini App ``initData``.

A Mini App is a web page Telegram opens inside its own client. Telegram hands
the page a signed blob describing who opened it, which lets the page be
trusted without any login link: the identity comes from Telegram, signed with
the bot's own token.

The scheme is Telegram's, and the only subtle part is the key derivation. The
HMAC key is not the bot token; it is ``HMAC_SHA256("WebAppData", bot_token)``,
with the constant as the key and the token as the message. Getting that pair
the wrong way round produces a validator that rejects everything, or worse,
one that accepts a blob signed by anyone who knows the constant.

Everything here is pure, so the signature check is directly testable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

# Telegram's fixed derivation constant.
_WEBAPP_CONSTANT = b"WebAppData"

# initData older than this is refused, so a captured blob is not reusable.
DEFAULT_MAX_AGE = 3600


class InitDataError(ValueError):
    """The initData is missing, malformed, unsigned, forged or stale."""


@dataclass(frozen=True)
class MiniAppUser:
    """The Telegram account that opened the Mini App."""

    id: int
    first_name: str = ""
    last_name: str = ""
    username: str | None = None

    @property
    def label(self) -> str:
        if self.username:
            return f"@{self.username}"
        name = " ".join(part for part in (self.first_name, self.last_name) if part)
        return name or str(self.id)


@dataclass(frozen=True)
class InitData:
    """A validated initData payload."""

    user: MiniAppUser
    auth_date: int
    fields: dict[str, str]


def _secret_key(bot_token: str) -> bytes:
    """Derive Telegram's Mini App signing key from the bot token."""
    return hmac.new(_WEBAPP_CONSTANT, bot_token.encode("utf-8"), hashlib.sha256).digest()


def data_check_string(fields: dict[str, str]) -> str:
    """Build the string Telegram signs: sorted ``key=value`` lines, no hash."""
    return "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items()) if key != "hash"
    )


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age: int | None = DEFAULT_MAX_AGE,
    now: float | None = None,
) -> InitData:
    """Verify ``init_data`` and return who opened the Mini App.

    Raises :class:`InitDataError` on anything short of a valid, fresh,
    correctly signed payload that names a user.
    """
    if not init_data:
        raise InitDataError("no initData was supplied")
    if not bot_token:
        raise InitDataError("no bot token to verify against")

    # parse_qsl percent-decodes, which is what the signature covers.
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    supplied_hash = fields.get("hash")
    if not supplied_hash:
        raise InitDataError("initData carries no hash")

    expected = hmac.new(
        _secret_key(bot_token),
        data_check_string(fields).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash):
        raise InitDataError("the initData signature does not match")

    raw_auth_date = fields.get("auth_date", "")
    try:
        auth_date = int(raw_auth_date)
    except ValueError:
        raise InitDataError("initData has no usable auth_date") from None

    if max_age is not None:
        current = time.time() if now is None else now
        age = current - auth_date
        if age > max_age:
            raise InitDataError(
                f"initData is {int(age)}s old, older than the {max_age}s limit"
            )
        # A little clock skew is normal; a lot means something is wrong.
        if age < -300:
            raise InitDataError("initData is dated in the future")

    raw_user = fields.get("user")
    if not raw_user:
        raise InitDataError("initData does not name a user")
    try:
        parsed = json.loads(raw_user)
    except json.JSONDecodeError:
        raise InitDataError("initData has a malformed user field") from None
    if not isinstance(parsed, dict) or "id" not in parsed:
        raise InitDataError("initData has a malformed user field")

    try:
        user_id = int(parsed["id"])
    except (TypeError, ValueError):
        raise InitDataError("initData has a malformed user id") from None

    return InitData(
        user=MiniAppUser(
            id=user_id,
            first_name=str(parsed.get("first_name") or ""),
            last_name=str(parsed.get("last_name") or ""),
            username=parsed.get("username") or None,
        ),
        auth_date=auth_date,
        fields=fields,
    )


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Produce a signed initData string. For tests and local development."""
    payload = dict(fields)
    payload.pop("hash", None)
    signature = hmac.new(
        _secret_key(bot_token),
        data_check_string(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return urlencode({**payload, "hash": signature})
