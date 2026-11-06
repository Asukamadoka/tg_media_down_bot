"""WMS M8.1 (docs/wms/M8.1-index-freshness.md): the index from the event feed.

The fault: PikPak does not touch a folder's ``modified_time`` when something
is restored into it, so the incremental stocktake, which skips folders whose
time has not moved, never saw today's transfers. ``FakeDrive.add(touch=False)``
is that behaviour.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from wms_fakes import FakeDrive, provider_for

from pikpak_wms.config import Config, ScheduledJob
from pikpak_wms.core.client import WmsClient
from pikpak_wms.core.models import render_note
from pikpak_wms.core.ratelimit import TokenBucket
from pikpak_wms.i18n import set_language
from pikpak_wms.nl.query import Filters, Query
from pikpak_wms.ops import eventsync, jobs, nl
from pikpak_wms.ops.context import Context
from pikpak_wms.ops.stocktake import stocktake
from pikpak_wms.store.db import Store


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language(None)


async def _no_sleep(_seconds: float) -> None:
    return None


class Setup:
    def __init__(self, drive: FakeDrive, ctx: Context) -> None:
        self.drive = drive
        self.ctx = ctx

    @property
    def store(self) -> Store:
        return self.ctx.store

    async def paths(self) -> set[str]:
        return {n.path for n in await self.store.nodes_under("/")}

    async def events(self, **kw) -> eventsync.EventSyncReport:
        return await eventsync.sync_events(self.ctx.client, self.store, **kw)

    async def baseline(self) -> None:
        """Index everything once, and set the cursor."""
        await stocktake(self.ctx.client, self.store, full=True)
        report = await self.events()
        assert report.baseline and report.fallback == "incremental"


@pytest.fixture
async def setup(tmp_path):
    config = Config()
    config.rules_file = tmp_path / "no-rules.yaml"
    # The fault itself: a restore does not touch the folder it lands in.
    drive = FakeDrive(propagate=False)
    async with Store(tmp_path / "wms.sqlite3") as store:
        client = WmsClient(provider_for(drive), limiter=TokenBucket(1e9, 1_000_000),
                           sleep=_no_sleep)
        yield Setup(drive, Context(config=config, client=client, store=store))


class TestTheFault:
    async def test_the_incremental_stocktake_misses_what_the_event_feed_finds(self, setup):
        setup.drive.add("/Movies/old.mkv", size=1)
        await setup.baseline()
        setup.drive.add("/Movies/restored.mkv", size=1, touch=False)
        await stocktake(setup.ctx.client, setup.store, full=False)
        assert "/Movies/restored.mkv" not in await setup.paths()  # the reported bug
        report = await setup.events()
        assert not report.fallback and report.upserted == 1
        assert "/Movies/restored.mkv" in await setup.paths()

    async def test_a_restored_folder_arrives_with_what_is_in_it(self, setup):
        setup.drive.add("/Movies/old.mkv", size=1)
        await setup.baseline()
        for name in ("a.mkv", "b.mkv"):
            setup.drive.add(f"/Movies/Trip/Day1/{name}", size=1, touch=False)
        report = await setup.events()
        assert {"/Movies/Trip", "/Movies/Trip/Day1", "/Movies/Trip/Day1/a.mkv",
                "/Movies/Trip/Day1/b.mkv"} <= await setup.paths()
        # One listing of the new folder covers the events of everything in it.
        assert report.events >= 4 and report.folders_listed <= 3

    async def test_a_file_whose_parent_is_unknown_to_the_index(self, setup):
        setup.drive.add("/Movies/old.mkv", size=1)
        await setup.baseline()
        setup.drive.emit = False
        setup.drive.add("/Fresh/Deep/file.mkv", size=1)       # folders and file, no events
        setup.drive.emit = True
        setup.drive.emit_event("TYPE_RESTORE", setup.drive.id_at("/Fresh/Deep/file.mkv"))
        await setup.events()
        assert "/Fresh/Deep/file.mkv" in await setup.paths()
        assert "/Fresh" in await setup.paths()

    async def test_the_event_type_is_not_what_decides(self, setup):
        setup.drive.add("/Movies/old.mkv", size=1)
        await setup.baseline()
        setup.drive.add("/Movies/odd.mkv", size=1, touch=False, event="TYPE_SOMETHING_NEW")
        await setup.events()
        assert "/Movies/odd.mkv" in await setup.paths()


class TestChangesAndRemovals:
    async def test_trash_and_delete_remove_from_the_index(self, setup):
        a = setup.drive.add("/Movies/a.mkv", size=1)
        b = setup.drive.add("/Movies/b.mkv", size=1)
        await setup.baseline()
        await setup.ctx.client.trash([a])
        await setup.ctx.client.delete_forever([b])
        report = await setup.events()
        assert report.removed == 2
        assert not {"/Movies/a.mkv", "/Movies/b.mkv"} & await setup.paths()

    async def test_a_rename_and_a_move_update_paths(self, setup):
        a = setup.drive.add("/Movies/a.mkv", size=1)
        folder = setup.drive.add("/Movies/Sub", folder=True)
        setup.drive.add("/Movies/Sub/inner.mkv", size=1)
        setup.drive.add("/Other", folder=True)
        await setup.baseline()
        await setup.ctx.client.rename(a, "renamed.mkv")
        await setup.ctx.client.move([folder], setup.drive.id_at("/Other"))
        await setup.events()
        paths = await setup.paths()
        assert "/Movies/renamed.mkv" in paths and "/Movies/a.mkv" not in paths
        assert {"/Other/Sub", "/Other/Sub/inner.mkv"} <= paths
        assert not {"/Movies/Sub", "/Movies/Sub/inner.mkv"} & paths

    async def test_nothing_new_costs_one_request(self, setup):
        setup.drive.add("/Movies/a.mkv", size=1)
        await setup.baseline()
        report = await setup.events()
        assert (report.events, report.requests, report.fallback) == (0, 1, "")


class TestTheCursor:
    async def test_it_carries_on_where_it_left_off(self, setup):
        setup.drive.add("/Movies/a.mkv", size=1)
        await setup.baseline()
        setup.drive.add("/Movies/b.mkv", size=1, touch=False)
        first = await setup.events()
        setup.drive.add("/Movies/c.mkv", size=1, touch=False)
        second = await setup.events()
        assert (first.events, second.events) == (1, 1)
        assert {"/Movies/b.mkv", "/Movies/c.mkv"} <= await setup.paths()

    async def test_a_page_at_a_time(self, setup):
        setup.drive.add("/Movies/a.mkv", size=1)
        await setup.baseline()
        setup.drive.page_size_cap = 3
        for n in range(8):
            setup.drive.add(f"/Movies/n{n}.mkv", size=1, touch=False)
        report = await setup.events(page_size=3)
        assert report.events == 8 and not report.fallback
        assert {f"/Movies/n{n}.mkv" for n in range(8)} <= await setup.paths()

    async def test_no_cursor_yet_sets_one_and_falls_back(self, setup):
        setup.drive.add("/Movies/a.mkv", size=1)
        report = await setup.events()
        assert report.baseline and report.fallback == "incremental"
        assert await eventsync._cursor(setup.store) is not None  # noqa: SLF001

    async def test_a_lost_cursor_means_a_full_stocktake(self, setup):
        setup.drive.add("/Movies/a.mkv", size=1)
        await setup.baseline()
        for n in range(12):
            setup.drive.add(f"/Movies/n{n}.mkv", size=1, touch=False)
        report = await setup.events(page_size=3, max_pages=2)
        assert report.fallback == "full" and "cursor is lost" in report.reason

    async def test_a_feed_that_refuses_its_next_page(self, setup):
        setup.drive.add("/Movies/a.mkv", size=1)
        await setup.baseline()
        setup.drive.cursor_expired = True
        for n in range(5):
            setup.drive.add(f"/Movies/n{n}.mkv", size=1, touch=False)
        report = await setup.events(page_size=2)
        assert report.fallback == "full"

    async def test_too_many_files_to_follow_one_by_one(self, setup):
        setup.drive.add("/Movies/a.mkv", size=1)
        await setup.baseline()
        for n in range(6):
            setup.drive.add(f"/Movies/n{n}.mkv", size=1, touch=False)
        report = await setup.events(max_files=3)
        assert report.fallback == "full"

    async def test_after_a_lost_cursor_it_starts_over_from_the_head(self, setup):
        setup.drive.add("/Movies/a.mkv", size=1)
        await setup.baseline()
        for n in range(12):
            setup.drive.add(f"/Movies/n{n}.mkv", size=1, touch=False)
        await setup.events(page_size=3, max_pages=2)
        again = await setup.events(page_size=3, max_pages=2)
        assert again.events == 0 and not again.fallback


class TestAFeedThatIsNotWhatWeExpect:
    async def test_events_that_name_no_file_fall_back_not_silently_skip(self, setup):
        setup.drive.add("/Movies/a.mkv", size=1)
        await setup.baseline()
        setup.drive.add("/Movies/b.mkv", size=1, touch=False)
        for event in setup.drive.feed:
            event.pop("file_id")
        report = await setup.events()
        assert report.fallback == "incremental" and "name no file" in report.reason

    async def test_the_file_can_be_nested_in_a_resource(self, setup):
        setup.drive.add("/Movies/a.mkv", size=1)
        await setup.baseline()
        setup.drive.add("/Movies/b.mkv", size=1, touch=False)
        last = setup.drive.feed[-1]
        last["reference_resource"] = {"id": last.pop("file_id")}
        await setup.events()
        assert "/Movies/b.mkv" in await setup.paths()

    async def test_a_feed_listing_oldest_first_is_not_followed(self, setup):
        setup.drive.add("/Movies/a.mkv", size=1)
        setup.drive.add("/Movies/b.mkv", size=1)
        await setup.baseline()
        setup.drive.feed.reverse()  # the same events, served the other way round
        report = await setup.events()
        assert report.fallback == "incremental" and "oldest first" in report.reason


class TestTheScheduledJob:
    async def test_stocktake_runs_the_event_sync(self, setup):
        setup.drive.add("/Movies/a.mkv", size=1)
        job = ScheduledJob(name="stocktake", cron="*/10 * * * *")
        first = await jobs.run_job(setup.ctx, job)            # baseline + incremental
        assert "stocktake" in first.summary.lower() and first.alert == ""
        setup.drive.add("/Movies/b.mkv", size=1, touch=False)
        second = await jobs.run_job(setup.ctx, job)
        assert "event feed" in second.summary
        assert "/Movies/b.mkv" in await setup.paths()

    async def test_a_lost_cursor_runs_the_full_stocktake_and_tells_the_admins_once(self, setup):
        setup.drive.add("/Movies/a.mkv", size=1)
        job = ScheduledJob(name="stocktake", cron="*/10 * * * *")
        await jobs.run_job(setup.ctx, job)
        setup.ctx.config.stocktake.page_size = 2
        for n in range(30):
            setup.drive.add(f"/Movies/n{n}.mkv", size=1, touch=False)
        lost = await jobs.run_job(setup.ctx, job)
        assert "PikPak's event feed" in lost.alert
        assert "/Movies/n29.mkv" in await setup.paths()       # the full stocktake found it
        for n in range(30, 60):
            setup.drive.add(f"/Movies/n{n}.mkv", size=1, touch=False)
        again = await jobs.run_job(setup.ctx, job)
        assert again.alert == ""                              # the same reason, told once a day

    async def test_the_old_way_when_events_are_off(self, setup):
        setup.ctx.config.stocktake.events = False
        setup.drive.add("/Movies/a.mkv", size=1)
        await jobs.run_job(setup.ctx, ScheduledJob(name="stocktake", cron="*/10 * * * *"))
        assert "events" not in setup.drive.calls
        setup.drive.add("/Movies/b.mkv", size=1, touch=False)
        await jobs.run_job(setup.ctx, ScheduledJob(name="stocktake", cron="*/10 * * * *"))
        assert "/Movies/b.mkv" not in await setup.paths()

    async def test_the_admins_get_the_alert_even_with_no_plan(self):
        from types import SimpleNamespace

        from tgmd.wms import WmsInBot

        bot = WmsInBot.__new__(WmsInBot)
        message = await WmsInBot._job_message(  # noqa: SLF001
            bot, SimpleNamespace(name="stocktake", alert="lost <cursor>", big=None,
                                 plan_ids=[], plan_id=None, report=None, reports=[]))
        assert message == ("lost &lt;cursor&gt;", None)


NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


class TestAskingAboutArrivals:
    def query(self, **filters) -> Query:
        return Query(intent="list", filters=Filters(**filters))

    async def test_a_time_question_syncs_first_and_says_when(self, setup):
        setup.drive.add("/Movies/old.mkv", size=1)
        await setup.baseline()
        setup.drive.add("/Movies/today.mkv", size=1, touch=False,
                        created=(NOW - timedelta(hours=1)).isoformat())
        proposal = await nl.make_proposal(setup.ctx, self.query(created_after="1d"), now=NOW)
        assert [n.name for n in proposal.matches] == ["today.mkv"]
        lines = [render_note(n) for n in proposal.notes]
        assert any(line.startswith("Index updated at") and "event sync" in line for line in lines)
        assert "events" in setup.drive.calls

    async def test_an_ordinary_question_does_not_read_the_feed(self, setup):
        setup.drive.add("/Movies/old.mkv", size=1)
        await setup.baseline()
        setup.drive.calls.clear()
        await nl.make_proposal(setup.ctx, self.query(extensions=["mkv"]), now=NOW)
        assert "events" not in setup.drive.calls

    async def test_the_entry_folders_read_the_feed_too(self, setup):
        setup.drive.add("/Telegram/old.mkv", size=1)
        await setup.baseline()
        setup.drive.add("/Telegram/new.mkv", size=1, touch=False)
        proposal = await nl.make_proposal(
            setup.ctx, Query(intent="list", scope={"path": "/Telegram"}), now=NOW)
        assert {n.name for n in proposal.matches} == {"old.mkv", "new.mkv"}

    async def test_nothing_found_says_why_it_might_be(self, setup):
        # Files that arrived two days ago, none today or yesterday.
        setup.drive.add("/Movies/a.mkv", size=1,
                        created=(NOW - timedelta(days=2)).isoformat())
        setup.drive.add("/Movies/b.mkv", size=1,
                        created=(NOW - timedelta(hours=30)).isoformat())
        setup.drive.add("/Movies/c.mkv", size=1, created=(NOW - timedelta(hours=1)).isoformat())
        await setup.baseline()
        proposal = await nl.make_proposal(
            setup.ctx, self.query(created_after=(NOW + timedelta(days=1)).isoformat()), now=NOW)
        assert proposal.count == 0
        lines = [render_note(n) for n in proposal.notes]
        assert "Nothing matches right now" in lines
        assert "By arrival in the drive: 1 file(s) today, 1 yesterday" in lines
        assert any(line.startswith("Index updated at") for line in lines)
        assert lines.index("Nothing matches right now") < lines.index(
            "By arrival in the drive: 1 file(s) today, 1 yesterday")

    async def test_a_hit_carries_no_diagnosis(self, setup):
        setup.drive.add("/Movies/a.mkv", size=1, created=(NOW - timedelta(hours=1)).isoformat())
        await setup.baseline()
        proposal = await nl.make_proposal(setup.ctx, self.query(created_after="1d"), now=NOW)
        assert proposal.count == 1
        assert not any(n["key"] == "nl.explain.none_diagnosis" for n in proposal.notes)


class TestInboxPlans:
    async def test_the_plan_says_how_current_the_index_was(self, setup):
        setup.drive.add("/Cos/keep.mp4", size=1)
        setup.drive.add("/Telegram/Cos 正片.mp4", size=1)
        await setup.baseline()
        setup.drive.add("/Telegram/Cos 新片.mp4", size=1, touch=False)
        job = ScheduledJob(name="organize-inbox", cron="0 * * * *")
        result = await jobs.run_job(setup.ctx, job)
        lines = result.plan.note_lines()
        assert any(line.startswith("Index updated at") and "event sync" in line
                   for line in lines)
        # The file restored into an existing folder is in the plan: the index had it.
        moved = [a.before.get("path") for a in result.plan.actions]
        assert "/Telegram/Cos 新片.mp4" in moved


class TestTheBuiltInSchedule:
    def test_every_ten_minutes(self):
        by_name = {job.name: job for job in Config().schedule.effective_jobs()}
        assert by_name["stocktake"].cron == "*/10 * * * *"

    def test_a_deployment_that_lists_its_own_keeps_it(self):
        config = Config.model_validate({"schedule": {"jobs": [
            {"name": "stocktake", "cron": "0 * * * *"}]}})
        stocktakes = [j for j in config.schedule.effective_jobs() if j.name == "stocktake"]
        assert [j.cron for j in stocktakes] == ["0 * * * *"]


def test_the_cursor_is_plain_json(tmp_path):
    # Stored values are never translated (the i18n rule): ids and a time.
    cursor = json.dumps({"ids": ["ev1"], "at": "2026-09-30T00:00:00+00:00"})
    assert json.loads(cursor)["ids"] == ["ev1"]
