"""Bot token parsing and the verification report model.

The token check is the first thing setup verification does, and the id it
extracts is what later gets compared against the account that answers, so it
has to be both strict and clear about what is wrong.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tgmd.identity import (
    BotTokenError,
    Check,
    Report,
    Status,
    account_link,
    describe_account,
    parse_bot_token,
)

VALID = "123456789:AAHfiqksKZ8wmoyzYeb1n1pbDVHQHKQ1abc"


class TestParseBotToken:
    def test_valid_token(self):
        token = parse_bot_token(VALID)
        assert token.bot_id == 123456789
        assert token.secret == VALID.split(":", 1)[1]

    def test_surrounding_whitespace_is_tolerated(self):
        assert parse_bot_token(f"  {VALID}\n").bot_id == 123456789

    def test_the_id_is_the_bots_user_id(self):
        # This is the property the identity cross-check relies on.
        assert parse_bot_token("7654321:" + "A" * 35).bot_id == 7654321

    def test_empty_token_points_at_botfather(self):
        with pytest.raises(BotTokenError, match="BotFather"):
            parse_bot_token("")

    def test_none_is_rejected(self):
        with pytest.raises(BotTokenError):
            parse_bot_token(None)  # type: ignore[arg-type]

    def test_missing_colon(self):
        with pytest.raises(BotTokenError, match="one colon"):
            parse_bot_token("123456789AAHfiqksKZ8wmoyzYeb1n1pbDVHQHKQ1abc")

    def test_too_many_colons(self):
        with pytest.raises(BotTokenError, match="one colon"):
            parse_bot_token("123:456:789")

    def test_non_numeric_id(self):
        with pytest.raises(BotTokenError, match="numeric bot id"):
            parse_bot_token("mybot:" + "A" * 35)

    def test_short_secret_reports_its_length(self):
        with pytest.raises(BotTokenError, match="8 characters"):
            parse_bot_token("123456789:tooshort")

    def test_secret_with_illegal_characters(self):
        with pytest.raises(BotTokenError):
            parse_bot_token("123456789:" + "!" * 35)


class TestDescribeAccount:
    def test_username_is_preferred(self):
        account = SimpleNamespace(id=7, username="mybot", first_name="My")
        assert describe_account(account) == "@mybot (id 7)"

    def test_falls_back_to_names(self):
        account = SimpleNamespace(id=7, username=None, first_name="Ada", last_name="L")
        assert describe_account(account) == "Ada L (id 7)"

    def test_falls_back_again_when_nameless(self):
        account = SimpleNamespace(id=7, username=None, first_name=None, last_name=None)
        assert describe_account(account) == "unnamed (id 7)"

    def test_none_is_handled(self):
        assert describe_account(None) == "unknown"

    def test_link_needs_a_username(self):
        assert account_link(SimpleNamespace(username="mybot")) == "https://t.me/mybot"
        assert account_link(SimpleNamespace(username=None)) is None


class TestStatus:
    def test_every_status_has_a_symbol_and_emoji(self):
        for status in Status:
            assert status.symbol
            assert status.emoji


class TestReport:
    def test_empty_report_is_ok(self):
        report = Report()
        assert report.ok
        assert report.render_text() == "no checks ran"

    def test_warnings_do_not_fail_the_report(self):
        report = Report()
        report.add(Check.ok("a"))
        report.add(Check.warn("b", "careful"))
        assert report.ok
        assert "1 warning(s)" in report.verdict()

    def test_a_failure_fails_the_report(self):
        report = Report()
        report.add(Check.ok("a"))
        report.add(Check.fail("b", "broken"))
        assert not report.ok
        assert len(report.failed) == 1
        assert "1 check(s) failed" in report.verdict()

    def test_all_clear_verdict(self):
        report = Report()
        report.add(Check.ok("a"))
        assert report.verdict() == "everything passed"

    def test_skips_are_neither_pass_nor_fail(self):
        report = Report()
        report.add(Check.skip("a", "not configured"))
        assert report.ok
        assert report.count(Status.SKIP) == 1

    def test_counts(self):
        report = Report()
        report.add(Check.ok("a"))
        report.add(Check.ok("b"))
        report.add(Check.warn("c"))
        assert report.count(Status.OK) == 2
        assert report.count(Status.WARN) == 1
        assert report.count(Status.FAIL) == 0

    def test_text_render_aligns_and_ends_with_the_verdict(self):
        report = Report()
        report.add(Check.ok("short", "one"))
        report.add(Check.fail("a much longer name", "two"))
        lines = report.render_text().splitlines()
        assert lines[0].startswith("✓")
        assert lines[1].startswith("✗")
        # Details line up because the names are padded to the widest one.
        assert lines[0].index("one") == lines[1].index("two")
        assert "failed" in lines[-1]

    def test_html_render_escapes_detail_text(self):
        report = Report()
        report.add(Check.fail("bad", "<script>alert(1)</script>"))
        rendered = report.render_html()
        assert "<script>" not in rendered
        assert "&lt;script&gt;" in rendered

    def test_html_render_includes_every_check(self):
        report = Report()
        report.add(Check.ok("first", "detail"))
        report.add(Check.warn("second"))
        rendered = report.render_html()
        assert "first" in rendered
        assert "second" in rendered

    def test_add_returns_the_check(self):
        report = Report()
        returned = report.add(Check.ok("a"))
        assert returned is report.checks[0]
