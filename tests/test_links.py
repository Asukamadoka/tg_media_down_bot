"""Link parsing: the part of the project most exposed to messy real input."""

from __future__ import annotations

import pytest

from tgmd.links import (
    LinkError,
    extract_links,
    normalize_chat_id,
    parse_message_link,
    to_peer_id,
)


class TestPublicLinks:
    def test_simple_channel_link(self):
        ref = parse_message_link("https://t.me/durov/123")
        assert ref is not None
        assert ref.chat == "durov"
        assert ref.ids == (123,)
        assert ref.topic_id is None
        assert not ref.is_private

    def test_scheme_is_optional(self):
        bare = parse_message_link("t.me/durov/123")
        full = parse_message_link("https://t.me/durov/123")
        assert bare is not None and full is not None
        # `raw` deliberately keeps whatever the user typed, so compare the rest.
        assert (bare.chat, bare.ids, bare.topic_id) == (full.chat, full.ids, full.topic_id)
        assert bare.raw == "t.me/durov/123"

    @pytest.mark.parametrize(
        "host", ["t.me", "www.t.me", "telegram.me", "telegram.dog"]
    )
    def test_alternate_hosts(self, host):
        ref = parse_message_link(f"https://{host}/durov/7")
        assert ref is not None and ref.ids == (7,)

    def test_web_preview_prefix_is_stripped(self):
        ref = parse_message_link("https://t.me/s/durov/123")
        assert ref is not None
        assert ref.chat == "durov"
        assert ref.ids == (123,)

    def test_topic_link(self):
        ref = parse_message_link("https://t.me/mygroup/12/345")
        assert ref is not None
        assert ref.topic_id == 12
        assert ref.ids == (345,)

    def test_trailing_punctuation_is_ignored(self):
        ref = parse_message_link("https://t.me/durov/123.")
        assert ref is not None and ref.ids == (123,)


class TestPrivateLinks:
    def test_internal_id(self):
        ref = parse_message_link("https://t.me/c/1234567890/55")
        assert ref is not None
        assert ref.chat == 1234567890
        assert ref.ids == (55,)
        assert ref.is_private

    def test_marked_id_is_normalized(self):
        ref = parse_message_link("https://t.me/c/-1001234567890/55")
        assert ref is not None and ref.chat == 1234567890

    def test_private_topic_link(self):
        ref = parse_message_link("https://t.me/c/1234567890/9/55")
        assert ref is not None
        assert ref.topic_id == 9
        assert ref.ids == (55,)

    def test_missing_message_id_is_rejected(self):
        with pytest.raises(LinkError):
            parse_message_link("https://t.me/c/1234567890")


class TestQueryParameters:
    def test_single_suppresses_album_expansion(self):
        ref = parse_message_link("https://t.me/durov/123?single")
        assert ref is not None and ref.single

    def test_comment_link(self):
        ref = parse_message_link("https://t.me/durov/123?comment=45")
        assert ref is not None
        assert ref.ids == (123,)
        assert ref.comment_id == 45

    def test_thread_parameter_sets_topic(self):
        ref = parse_message_link("https://t.me/c/123/456?thread=7")
        assert ref is not None and ref.topic_id == 7

    def test_video_timestamp_is_ignored(self):
        ref = parse_message_link("https://t.me/durov/123?t=90")
        assert ref is not None and ref.ids == (123,)


class TestRanges:
    def test_inclusive_range(self):
        ref = parse_message_link("https://t.me/durov/100-103")
        assert ref is not None and ref.ids == (100, 101, 102, 103)

    def test_tilde_separator(self):
        ref = parse_message_link("https://t.me/durov/5~7")
        assert ref is not None and ref.ids == (5, 6, 7)

    def test_reversed_range_is_normalized(self):
        ref = parse_message_link("https://t.me/durov/103-100")
        assert ref is not None and ref.ids == (100, 101, 102, 103)

    def test_absurd_range_is_refused(self):
        with pytest.raises(LinkError, match="too many messages"):
            parse_message_link("https://t.me/durov/1-999999")


class TestInviteLinks:
    def test_plus_form(self):
        ref = parse_message_link("https://t.me/+AbCdEf0123456789")
        assert ref is not None
        assert ref.invite_hash == "AbCdEf0123456789"
        assert ref.is_invite_only

    def test_joinchat_form(self):
        ref = parse_message_link("https://t.me/joinchat/AbCdEf0123456789")
        assert ref is not None and ref.invite_hash == "AbCdEf0123456789"

    def test_phone_number_link_is_rejected(self):
        with pytest.raises(LinkError, match="phone-number"):
            parse_message_link("https://t.me/+79991234567")


class TestDeepLinks:
    def test_resolve(self):
        ref = parse_message_link("tg://resolve?domain=durov&post=5")
        assert ref is not None
        assert ref.chat == "durov"
        assert ref.ids == (5,)

    def test_privatepost(self):
        ref = parse_message_link("tg://privatepost?channel=1234567890&post=5")
        assert ref is not None
        assert ref.chat == 1234567890
        assert ref.ids == (5,)


class TestRejections:
    def test_non_telegram_url(self):
        assert parse_message_link("https://example.com/durov/1") is None

    def test_empty_text(self):
        assert parse_message_link("   ") is None

    @pytest.mark.parametrize("path", ["addstickers/foo", "proxy/x", "share/url"])
    def test_reserved_paths(self, path):
        with pytest.raises(LinkError, match="not a chat link"):
            parse_message_link(f"https://t.me/{path}")

    def test_username_without_message_id(self):
        with pytest.raises(LinkError, match="no message id"):
            parse_message_link("https://t.me/durov")

    def test_invalid_username(self):
        with pytest.raises(LinkError):
            parse_message_link("https://t.me/1bad/5")


class TestChatIdNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1234567890", 1234567890),
            ("-1001234567890", 1234567890),
            (1234567890, 1234567890),
            ("100123", 100123),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_chat_id(raw) == expected

    def test_round_trip_to_peer_id(self):
        assert to_peer_id(normalize_chat_id("-1001234567890")) == -1001234567890

    def test_rejects_non_numeric(self):
        with pytest.raises(LinkError):
            normalize_chat_id("abc")


class TestExtractLinks:
    def test_mixed_message(self):
        text = (
            "grab these:\n"
            "https://t.me/durov/1\n"
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=x\n"
            "https://mypikpak.com/s/VO8BcRbShare\n"
            "https://example.com/video.mp4\n"
        )
        bundle = extract_links(text)
        assert [ref.ids for ref in bundle.messages] == [(1,)]
        assert len(bundle.magnets) == 1
        assert bundle.pikpak_shares == ["https://mypikpak.com/s/VO8BcRbShare"]
        assert bundle.direct_urls == ["https://example.com/video.mp4"]
        assert bundle.total == 4

    def test_duplicate_links_are_collapsed(self):
        bundle = extract_links("https://t.me/durov/1 https://t.me/durov/1")
        assert len(bundle.messages) == 1

    def test_bare_link_without_scheme(self):
        bundle = extract_links("see t.me/durov/9 please")
        assert [ref.ids for ref in bundle.messages] == [(9,)]

    def test_unusable_link_is_reported(self):
        bundle = extract_links("https://t.me/durov")
        assert not bundle.messages
        assert bundle.errors and "no message id" in bundle.errors[0]

    def test_plain_text_yields_nothing(self):
        bundle = extract_links("hello there")
        assert not bundle
        assert bundle.total == 0

    def test_range_counts_every_message(self):
        bundle = extract_links("https://t.me/durov/1-5")
        assert bundle.total == 5


class TestMessageRefHelpers:
    def test_describe_single(self):
        ref = parse_message_link("https://t.me/durov/12")
        assert ref is not None and ref.describe() == "@durov/12"

    def test_describe_private_range(self):
        ref = parse_message_link("https://t.me/c/99/10-12")
        assert ref is not None and ref.describe() == "c/99/10-12"

    def test_with_id_narrows_the_reference(self):
        ref = parse_message_link("https://t.me/durov/1-3")
        assert ref is not None
        narrowed = ref.with_id(2)
        assert narrowed.ids == (2,)
        assert narrowed.chat == ref.chat
