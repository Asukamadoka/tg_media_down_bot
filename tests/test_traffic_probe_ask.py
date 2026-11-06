"""M9.1b / M9.2 §F: the bot asks before a scheduled speed test spends proxy traffic."""

from __future__ import annotations

import asyncio
import urllib.error

import pytest
from test_traffic_nodes import SPEEDS, FakeEvent, labels, make

from tgmd import i18n
from tgmd.config import TrafficConfig, load_config
from tgmd.db import Database
from tgmd.traffic import proxy_ui as ui
from tgmd.traffic.mihomo import MihomoClient
from tgmd.traffic.store import TrafficStore

ASK = "要现在给节点测速吗？最多约 150 MB 代理流量（≈¥0.01）"
HOUR = 3600.0


@pytest.fixture
def store(tmp_path):
    handle = TrafficStore(tmp_path / "t.sqlite3")
    handle.open()
    yield handle
    handle.close()


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "bot.sqlite3")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture(autouse=True)
def chinese():
    previous = i18n.language()
    i18n.set_language("zh")
    yield
    i18n.set_language(previous)


def zh():
    i18n.set_language("zh")


def asking(store, db=None, **settings):
    """A manager whose messages (with their buttons) are kept, on a clock we move."""
    zh()
    clock = {"now": 1_000_000.0}
    settings.setdefault("probe_hours", 6)
    w = make(store, db, clock=lambda: clock["now"], **settings)
    w.asked = []
    w.clock = clock

    async def notify(text, buttons=None):
        w.asked.append((text, buttons))

    w.manager._notify = notify  # noqa: SLF001
    return w


class TestTheAsk:
    async def test_the_first_scheduled_probe_asks_instead_of_running(self, store, db):
        w = asking(store, db)
        await w.manager.tick(1_000_100.0)                      # too early: 5 minutes after start
        assert w.asked == []
        await w.manager.tick(1_000_301.0)
        ((text, buttons),) = w.asked
        assert text == ASK
        assert labels(buttons) == ["立即测速", "跳过本次"]
        assert store.last_probe() == 0 and not w.manager.probing      # nothing was measured
        assert w.manager.ask_pending

    async def test_only_one_ask_is_pending_at_a_time(self, store, db):
        w = asking(store, db)
        for moment in (1_000_301.0, 1_000_400.0, 1_000_000.0 + 5 * HOUR):
            await w.manager.tick(moment)
        assert len(w.asked) == 1
        assert await w.manager.ask_probe() is False                    # not by hand either

    async def test_immediate_speed_test_runs_and_edits_the_message_into_the_result(
            self, store, db):
        w = asking(store, db, speeds=dict(SPEEDS))
        await w.manager.tick(1_000_301.0)
        event = FakeEvent("proxy:ask:go")
        await ui.handle_button(event, w.manager, None)
        assert event.edits[0][0] == "⏳ 正在逐个测速，需要几分钟…"
        result = event.edits[-1][0]
        assert "<b>测速结果</b>" in result and "东京01" in result and "80" in result
        assert "测了 4 个节点，约用了 " in result
        assert event.answers == ["测速完成"]
        assert not w.manager.ask_pending and store.last_probe() > 0

    async def test_skipping_waits_for_the_next_schedule(self, store, db):
        w = asking(store, db)
        await w.manager.tick(1_000_301.0)
        event = FakeEvent("proxy:ask:skip")
        await ui.handle_button(event, w.manager, None)
        assert event.edits[0][0] == "已跳过，下次到点再问你。" and event.answers == ["已跳过"]
        assert not w.manager.ask_pending and store.last_probe() == 0
        await w.manager.tick(1_000_301.0 + 2 * HOUR)
        await w.manager.tick(1_000_301.0 + 5 * HOUR)
        assert len(w.asked) == 1                                         # still quiet
        await w.manager.tick(1_000_301.0 + 6 * HOUR + 1)
        assert len(w.asked) == 2                                         # the next schedule

    async def test_the_state_survives_a_restart(self, store, db):
        w = asking(store, db)
        await w.manager.tick(1_000_301.0)
        again = asking(store, db)
        await again.manager.load()
        assert again.manager.ask_pending                                 # no second message
        await again.manager.tick(1_000_400.0)
        assert again.asked == []

    async def test_an_ask_nobody_answers_is_asked_again_after_an_interval(self, store, db):
        w = asking(store, db)
        await w.manager.tick(1_000_301.0)
        await w.manager.tick(1_000_301.0 + 6 * HOUR + 10)
        assert len(w.asked) == 2

    async def test_pressing_run_twice_is_not_two_probes(self, store, db):
        w = asking(store, db, speeds=dict(SPEEDS))
        await w.manager.tick(1_000_301.0)
        first = asyncio.create_task(ui.handle_button(FakeEvent("proxy:ask:go"), w.manager, None))
        await asyncio.sleep(0)
        second = FakeEvent("proxy:ask:go")
        await ui.handle_button(second, w.manager, None)
        await first
        assert second.answers == ["这个问题已经处理过了。"] and second.edits == []
        assert len(w.net.downloaded) == 4                             # one probe, four nodes

    async def test_an_old_button_after_skipping_costs_nothing(self, store, db):
        w = asking(store, db, speeds=dict(SPEEDS))
        await w.manager.tick(1_000_301.0)
        await ui.handle_button(FakeEvent("proxy:ask:skip"), w.manager, None)
        late = FakeEvent("proxy:ask:go")
        await ui.handle_button(late, w.manager, None)
        assert late.answers == ["这个问题已经处理过了。"] and w.net.downloaded == []

    async def test_a_probe_that_cannot_run_says_why_in_the_same_message(self, store, db):
        w = asking(store, db)

        def refuse(method, url, body):
            raise urllib.error.HTTPError(url, 404, "no such group", {}, None)

        w.manager._prober._client = MihomoClient(  # noqa: SLF001
            "http://x", fetch=w.controller.fetch, send=refuse)
        await w.manager.tick(1_000_301.0)
        event = FakeEvent("proxy:ask:go")
        await ui.handle_button(event, w.manager, None)
        assert event.edits[-1][0].startswith("测速没有跑起来：")


class TestSettings:
    async def test_without_confirmation_the_schedule_runs_the_probe_itself(self, store, db):
        w = asking(store, db, probe_confirm=False, speeds=dict(SPEEDS))
        await w.manager.tick(1_000_301.0)
        assert w.asked == []
        await asyncio.wait_for(w.manager._probe_task, 5)             # noqa: SLF001
        assert store.last_probe() > 0

    def test_the_default_is_to_ask(self, monkeypatch):
        assert TrafficConfig().probe_confirm is True
        assert load_config().traffic.probe_confirm is True
        monkeypatch.setenv("PROXY_PROBE_CONFIRM", "false")
        assert load_config().traffic.probe_confirm is False

    async def test_no_schedule_no_ask(self, store, db):
        w = asking(store, db, probe_hours=0)
        await w.manager.tick(1_000_000.0 + 100 * HOUR)
        assert w.asked == []


class TestTheCommandLineFlag:
    async def test_the_running_bot_picks_the_request_up_and_sends_the_same_ask(self, store, db):
        w = asking(store, db, probe_hours=0)          # nothing is scheduled: only the flag asks
        await w.manager.tick(1_000_000.0)             # the first look finds nothing
        assert w.asked == []
        store.request_probe_ask(1_000_005.0)
        await w.manager.tick(1_000_020.0)             # less than 30 s since the last look
        assert w.asked == []
        await w.manager.tick(1_000_031.0)
        ((text, buttons),) = w.asked
        assert text == ASK and labels(buttons) == ["立即测速", "跳过本次"]
        await w.manager.tick(1_000_100.0)             # the row was taken: no repeat
        assert len(w.asked) == 1

    async def test_a_request_while_one_is_open_sends_nothing_new(self, store, db):
        w = asking(store, db, probe_hours=0)
        assert await w.manager.ask_probe() is True
        store.request_probe_ask(1_000_001.0)
        await w.manager.tick(1_000_040.0)
        assert len(w.asked) == 1 and store.take_probe_ask(1_000_050.0) is False

    def test_the_command_writes_the_row(self, tmp_path, monkeypatch, capsys):
        from tgmd.traffic import __main__ as cli

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        assert cli.main(["ask-probe"]) == 0
        assert "asked" in capsys.readouterr().out
        handle = TrafficStore(load_config().download.traffic_db_path)
        handle.open()
        try:
            assert handle.take_probe_ask(1.0) is True
            assert handle.take_probe_ask(2.0) is False
        finally:
            handle.close()

    async def test_the_store_marks_a_batch_of_requests_handled_together(self, store):
        store.request_probe_ask(1.0)
        store.request_probe_ask(2.0)
        assert store.take_probe_ask(3.0) is True and store.take_probe_ask(4.0) is False
