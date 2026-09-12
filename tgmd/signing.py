"""Signed, expiring tokens for the public file URLs handed to PikPak.

PikPak fetches files by URL, which means the bot has to expose downloaded
files over HTTP. Anything reachable from the internet gets scanned, so the
path carries an HMAC over the file id and an expiry instead of being
guessable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time


class TokenError(ValueError):
    """The token is malformed, tampered with, or expired."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise TokenError("token is not valid base64url") from exc


def _digest(secret: str, payload: bytes) -> str:
    return _b64encode(hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest())


def make_token(secret: str, file_id: str, expires_at: int) -> str:
    """Build a token proving ``file_id`` may be served until ``expires_at``."""
    if "|" in file_id:
        raise ValueError("file_id must not contain '|'")
    payload = f"{file_id}|{int(expires_at)}".encode("utf-8")
    return f"{_b64encode(payload)}.{_digest(secret, payload)}"


def verify_token(secret: str, token: str, *, now: float | None = None) -> str:
    """Return the file id carried by ``token``, or raise :class:`TokenError`."""
    encoded_payload, _, signature = token.partition(".")
    if not encoded_payload or not signature:
        raise TokenError("token is missing its signature")

    payload = _b64decode(encoded_payload)
    if not hmac.compare_digest(_digest(secret, payload), signature):
        raise TokenError("signature does not match")

    try:
        file_id, _, expires_raw = payload.decode("utf-8").rpartition("|")
        expires_at = int(expires_raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise TokenError("token payload is malformed") from exc

    if not file_id:
        raise TokenError("token payload is malformed")

    current = time.time() if now is None else now
    if expires_at <= current:
        raise TokenError("token has expired")

    return file_id
