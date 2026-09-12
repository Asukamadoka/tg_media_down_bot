"""Signed URLs: the only thing standing between the internet and the disk."""

from __future__ import annotations

import time

import pytest

from tgmd.signing import TokenError, make_token, verify_token

SECRET = "a-test-secret"


def test_round_trip():
    token = make_token(SECRET, "file123", int(time.time()) + 60)
    assert verify_token(SECRET, token) == "file123"


def test_expired_token_is_refused():
    token = make_token(SECRET, "file123", int(time.time()) - 1)
    with pytest.raises(TokenError, match="expired"):
        verify_token(SECRET, token)


def test_expiry_is_checked_against_the_supplied_clock():
    expires = 1_000_000
    token = make_token(SECRET, "f", expires)
    assert verify_token(SECRET, token, now=expires - 1) == "f"
    with pytest.raises(TokenError):
        verify_token(SECRET, token, now=expires)


def test_another_secret_cannot_verify():
    token = make_token(SECRET, "file123", int(time.time()) + 60)
    with pytest.raises(TokenError, match="signature"):
        verify_token("different-secret", token)


def test_tampered_payload_is_refused():
    token = make_token(SECRET, "file123", int(time.time()) + 60)
    payload, _, signature = token.partition(".")
    forged = f"{payload[:-2]}XY.{signature}"
    with pytest.raises(TokenError):
        verify_token(SECRET, forged)


def test_signature_cannot_be_dropped():
    token = make_token(SECRET, "file123", int(time.time()) + 60)
    with pytest.raises(TokenError, match="signature"):
        verify_token(SECRET, token.split(".")[0])


@pytest.mark.parametrize("token", ["", ".", "junk", "junk.junk", "!!!.!!!"])
def test_malformed_tokens_are_refused(token):
    with pytest.raises(TokenError):
        verify_token(SECRET, token)


def test_file_id_with_a_separator_is_rejected_at_creation():
    with pytest.raises(ValueError):
        make_token(SECRET, "bad|id", int(time.time()) + 60)


def test_tokens_are_url_safe():
    token = make_token(SECRET, "file123", int(time.time()) + 60)
    assert "/" not in token
    assert "+" not in token
    assert "=" not in token
