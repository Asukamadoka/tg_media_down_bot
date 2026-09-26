"""Telegram Mini App initData validation.

This signature check is what stands between "Telegram told me who this is"
and "a stranger claimed to be someone". The key derivation is the subtle
part, so it is pinned explicitly below.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from tgmd.miniapp import (
    InitDataError,
    MiniAppUser,
    data_check_string,
    sign_init_data,
    validate_init_data,
)

BOT_TOKEN = "123456789:AAHfiqksKZ8wmoyzYeb1n1pbDVHQHKQ1abc"


def make_fields(**overrides) -> dict[str, str]:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(
            {"id": 4242, "first_name": "Ada", "last_name": "L", "username": "ada"}
        ),
    }
    fields.update({key: str(value) for key, value in overrides.items()})
    return fields


class TestRoundTrip:
    def test_signed_data_validates(self):
        init_data = sign_init_data(make_fields(), BOT_TOKEN)
        result = validate_init_data(init_data, BOT_TOKEN)
        assert result.user.id == 4242
        assert result.user.username == "ada"

    def test_auth_date_is_returned(self):
        stamp = int(time.time()) - 30
        init_data = sign_init_data(make_fields(auth_date=stamp), BOT_TOKEN)
        assert validate_init_data(init_data, BOT_TOKEN).auth_date == stamp

    def test_extra_fields_are_preserved(self):
        init_data = sign_init_data(make_fields(chat_type="private"), BOT_TOKEN)
        result = validate_init_data(init_data, BOT_TOKEN)
        assert result.fields["chat_type"] == "private"


class TestKeyDerivation:
    """Telegram's key is HMAC(key="WebAppData", msg=bot_token), not the reverse."""

    def test_the_documented_derivation_is_accepted(self):
        fields = make_fields()
        secret = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        signature = hmac.new(
            secret, data_check_string(fields).encode(), hashlib.sha256
        ).hexdigest()
        init_data = urlencode({**fields, "hash": signature})
        assert validate_init_data(init_data, BOT_TOKEN).user.id == 4242

    def test_the_reversed_derivation_is_refused(self):
        # Swapping key and message is the classic implementation slip; a
        # validator that accepts this would trust anyone who knows the
        # constant, which is public.
        fields = make_fields()
        wrong_secret = hmac.new(
            BOT_TOKEN.encode(), b"WebAppData", hashlib.sha256
        ).digest()
        signature = hmac.new(
            wrong_secret, data_check_string(fields).encode(), hashlib.sha256
        ).hexdigest()
        init_data = urlencode({**fields, "hash": signature})
        with pytest.raises(InitDataError, match="signature"):
            validate_init_data(init_data, BOT_TOKEN)

    def test_plain_token_as_key_is_refused(self):
        fields = make_fields()
        signature = hmac.new(
            BOT_TOKEN.encode(), data_check_string(fields).encode(), hashlib.sha256
        ).hexdigest()
        init_data = urlencode({**fields, "hash": signature})
        with pytest.raises(InitDataError):
            validate_init_data(init_data, BOT_TOKEN)


class TestDataCheckString:
    def test_hash_is_excluded(self):
        assert "hash=" not in data_check_string({"a": "1", "hash": "x"})

    def test_keys_are_sorted(self):
        assert data_check_string({"b": "2", "a": "1"}) == "a=1\nb=2"

    def test_lines_are_newline_separated(self):
        assert data_check_string({"a": "1", "b": "2"}).count("\n") == 1


class TestRejections:
    def test_empty_init_data(self):
        with pytest.raises(InitDataError, match="no initData"):
            validate_init_data("", BOT_TOKEN)

    def test_missing_bot_token(self):
        init_data = sign_init_data(make_fields(), BOT_TOKEN)
        with pytest.raises(InitDataError, match="no bot token"):
            validate_init_data(init_data, "")

    def test_another_bot_token_cannot_verify(self):
        init_data = sign_init_data(make_fields(), BOT_TOKEN)
        with pytest.raises(InitDataError, match="signature"):
            validate_init_data(init_data, "987654321:" + "B" * 35)

    def test_missing_hash(self):
        with pytest.raises(InitDataError, match="no hash"):
            validate_init_data(urlencode(make_fields()), BOT_TOKEN)

    def test_tampered_user_is_refused(self):
        """Rewriting the user id after signing must not validate."""
        fields = make_fields()
        init_data = sign_init_data(fields, BOT_TOKEN)
        forged = init_data.replace("4242", "9999")
        assert forged != init_data
        with pytest.raises(InitDataError, match="signature"):
            validate_init_data(forged, BOT_TOKEN)

    def test_stale_data_is_refused(self):
        old = int(time.time()) - 7200
        init_data = sign_init_data(make_fields(auth_date=old), BOT_TOKEN)
        with pytest.raises(InitDataError, match="older than"):
            validate_init_data(init_data, BOT_TOKEN, max_age=3600)

    def test_freshness_can_be_disabled(self):
        old = int(time.time()) - 999999
        init_data = sign_init_data(make_fields(auth_date=old), BOT_TOKEN)
        assert validate_init_data(init_data, BOT_TOKEN, max_age=None).user.id == 4242

    def test_future_dated_data_is_refused(self):
        ahead = int(time.time()) + 9999
        init_data = sign_init_data(make_fields(auth_date=ahead), BOT_TOKEN)
        with pytest.raises(InitDataError, match="future"):
            validate_init_data(init_data, BOT_TOKEN)

    def test_small_clock_skew_is_tolerated(self):
        ahead = int(time.time()) + 60
        init_data = sign_init_data(make_fields(auth_date=ahead), BOT_TOKEN)
        assert validate_init_data(init_data, BOT_TOKEN).user.id == 4242

    def test_non_numeric_auth_date(self):
        init_data = sign_init_data(make_fields(auth_date="soon"), BOT_TOKEN)
        with pytest.raises(InitDataError, match="auth_date"):
            validate_init_data(init_data, BOT_TOKEN)

    def test_missing_user(self):
        fields = make_fields()
        del fields["user"]
        init_data = sign_init_data(fields, BOT_TOKEN)
        with pytest.raises(InitDataError, match="does not name a user"):
            validate_init_data(init_data, BOT_TOKEN)

    def test_malformed_user_json(self):
        init_data = sign_init_data(make_fields(user="{not json"), BOT_TOKEN)
        with pytest.raises(InitDataError, match="malformed user"):
            validate_init_data(init_data, BOT_TOKEN)

    def test_user_without_an_id(self):
        init_data = sign_init_data(
            make_fields(user=json.dumps({"first_name": "Ada"})), BOT_TOKEN
        )
        with pytest.raises(InitDataError, match="malformed user"):
            validate_init_data(init_data, BOT_TOKEN)

    def test_user_with_a_non_numeric_id(self):
        init_data = sign_init_data(
            make_fields(user=json.dumps({"id": "abc"})), BOT_TOKEN
        )
        with pytest.raises(InitDataError, match="user id"):
            validate_init_data(init_data, BOT_TOKEN)


class TestMiniAppUser:
    def test_username_label(self):
        assert MiniAppUser(id=1, username="ada").label == "@ada"

    def test_name_label(self):
        assert MiniAppUser(id=1, first_name="Ada", last_name="L").label == "Ada L"

    def test_id_label_when_nameless(self):
        assert MiniAppUser(id=7).label == "7"
