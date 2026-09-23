"""The progress message: throttled edits that always end on the final state."""

from __future__ import annotations

import pytest
from telethon.errors import FloodWaitError, MessageNotModifiedError

from tgmd.reporter import Reporter


class FakeMessage:
    def __init__(self, text: str, *, fail: list[Exception] | None = None) -> None:
        self.texts = [text]
        self.fail = list(fail or [])

    async def edit(self, text: str, **_kwargs) -> None:
        if self.fail:
            raise self.fail.pop(0)
        self.texts.append(text)


class FakeBot:
    def __init__(self, *, fail_send: bool = False, fail_edits=None) -> None:
        self.fail_send = fail_send
        self.fail_edits = fail_edits
        self.messages: list[FakeMessage] = []

    async def send_message(self, chat_id, text, **_kwargs):
        if self.fail_send:
            raise RuntimeError("chat not found")
        message = FakeMessage(text, fail=self.fail_edits)
        self.messages.append(message)
        return message


@pytest.fixture
def clock(monkeypatch):
    """A controllable time.monotonic for the throttle."""
    now = [1000.0]
    monkeypatch.setattr("tgmd.reporter.time.monotonic", lambda: now[0])
    return now


async def opened(bot, **kwargs) -> Reporter:
    reporter = Reporter(bot, 1, interval=5.0, **kwargs)
    await reporter.open("starting")
    return reporter


class TestThrottle:
    async def test_updates_inside_the_interval_are_dropped(self, clock):
        bot = FakeBot()
        reporter = await opened(bot)
        clock[0] += 1
        await reporter.update("10%")
        assert bot.messages[0].texts == ["starting"]

    async def test_an_update_after_the_interval_goes_through(self, clock):
        bot = FakeBot()
        reporter = await opened(bot)
        clock[0] += 6
        await reporter.update("50%")
        assert bot.messages[0].texts[-1] == "50%"

    async def test_forced_updates_ignore_the_interval(self, clock):
        bot = FakeBot()
        reporter = await opened(bot)
        await reporter.update("next file", force=True)
        assert bot.messages[0].texts[-1] == "next file"

    async def test_the_final_state_is_always_written(self, clock):
        bot = FakeBot()
        reporter = await opened(bot)
        await reporter.update("90%", force=True)
        await reporter.close("done")
        assert bot.messages[0].texts[-1] == "done"

    async def test_identical_text_is_not_re_sent(self, clock):
        bot = FakeBot()
        reporter = await opened(bot)
        await reporter.update("starting", force=True)
        assert bot.messages[0].texts == ["starting"]


class TestFailures:
    async def test_a_failed_post_makes_every_later_call_a_no_op(self, clock):
        reporter = await opened(FakeBot(fail_send=True))
        await reporter.update("x", force=True)
        await reporter.close("done")  # must not raise

    async def test_a_flood_wait_pushes_the_next_update_back(self, clock):
        bot = FakeBot(fail_edits=[FloodWaitError(request=None, capture=30)])
        reporter = await opened(bot)
        clock[0] += 6
        await reporter.update("50%")  # flood-waited
        clock[0] += 10
        await reporter.update("60%")  # still inside the 30s wait
        assert bot.messages[0].texts == ["starting"]
        clock[0] += 30
        await reporter.update("70%")
        assert bot.messages[0].texts[-1] == "70%"

    async def test_not_modified_counts_as_written(self, clock):
        bot = FakeBot(fail_edits=[MessageNotModifiedError(request=None)])
        reporter = await opened(bot)
        await reporter.update("same", force=True)
        # Recorded as the current text, so it is not retried.
        await reporter.update("same", force=True)
        assert bot.messages[0].texts == ["starting"]

    async def test_any_other_edit_failure_does_not_reach_the_job(self, clock):
        bot = FakeBot(fail_edits=[RuntimeError("message to edit not found")])
        reporter = await opened(bot)
        await reporter.close("done")  # must not raise
