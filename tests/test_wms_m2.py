"""WMS M2: apply, audit, undo, the five business modules, jobs, the scheduler, the CLI.

M2's acceptance (CC_BRIEF §5) is :class:`TestAcceptance`: a rules file, a
dry-run plan, apply, an audit trail, and undo of rename, move and trash.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pikpakapi.PikpakException import PikpakException
from typer.testing import CliRunner
from wms_fakes import FakeDrive, provider_for

from pikpak_wms.cli import main as cli
from pikpak_wms.config import Config, ScheduledJob
from pikpak_wms.core.client import WmsClient
from pikpak_wms.core.errors import WmsError
from pikpak_wms.core.models import ActionType
from pikpak_wms.core.ratelimit import TokenBucket
from pikpak_wms.i18n import set_language
from pikpak_wms.ops import inbound, jobs, organize, outbound, plans
from pikpak_wms.ops.context import Context
from pikpak_wms.ops.stocktake import stocktake
from pikpak_wms.rules.actions import Refused
from pikpak_wms.rules.schema import parse_rules
from pikpak_wms.scheduler.runner import WmsScheduler
from pikpak_wms.store.db import Store

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

WRITES = {"file_rename", "file_batch_move", "file_batch_copy", "delete_to_trash", "untrash",
          "delete_forever", "file_batch_star", "file_batch_unstar", "file_batch_share",
          "create_folder", "offline_download", "restore"}


def writes(drive: FakeDrive) -> list[str]:
    return [call for call in drive.calls if call in WRITES]


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language(None)


class World:
    def __init__(self, drive: FakeDrive, ctx: Context) -> None:
        self.drive = drive
        self.ctx = ctx

    @property
    def store(self) -> Store:
        return self.ctx.store

    async def sync(self) -> None:
        await stocktake(self.ctx.client, self.store, full=True)

    async def plan(self, yaml_rules: list[dict], **kw):
        ruleset = parse_rules({"rules": yaml_rules})
        return await organize.organize(self.ctx, ruleset, at=NOW, **kw)

    def exists(self, path: str) -> bool:
        try:
            self.drive.id_at(path)
        except KeyError:
            return False
        return True


@pytest.fixture
async def world(tmp_path):
    drive = FakeDrive()
    config = Config()
    async with Store(tmp_path / "wms.sqlite3") as store:
        client = WmsClient(provider_for(drive), limiter=TokenBucket(1e9, 1_000_000),
                           sleep=_no_sleep)
        yield World(drive, Context(config=config, client=client, store=store))


async def _no_sleep(_seconds: float) -> None:
    return None


SHOWS = {
    "name": "shows",
    "scope": "/Inbox",
    "match": {"kind": "file", "name_regex": r"(?P<show>.+?)\.S(?P<s>\d{2})E(?P<e>\d{2})",
              "min_size": "100MB"},
    "actions": [
        {"rename": {"template": "{show|title}.S{s}E{e}.{ext}"}},
        {"move": {"to": "/Media/{show|title}/S{s}"}},
    ],
}
ADS = {"name": "ads", "scope": "/Inbox", "match": {"kind": "file", "name_regex": "(?i)广告",
                                                   "max_size": "5MB"},
       "actions": ["trash"]}
BIG = 200 * 1024**2


# -------------------------------------------------------------- acceptance


class TestAcceptance:
    async def test_rules_plan_apply_audit_undo(self, world):
        w = world
        w.drive.add("/Inbox/lost.S01E01.mkv", size=BIG)
        w.drive.add("/Inbox/lost.S01E02.mkv", size=BIG)
        w.drive.add("/Inbox/最新地址广告.txt", size=100)
        await w.sync()

        # 1. dry run: a plan, and nothing sent to PikPak.
        plan = await w.plan([SHOWS, ADS])
        plan_id = await plans.save(w.ctx, plan)
        assert writes(w.drive) == []
        lines = plans.plan_lines(plan, plan_id=plan_id)
        assert lines[0].startswith(f"Plan {plan_id} (organize): 6 action(s) on 3 file(s)")
        assert any("mkdir   /Media/Lost/S01" in line for line in lines)

        # 2. apply: the drive now looks the way the rules say.
        report = await plans.apply(w.ctx, plan_id)
        assert (report.applied, report.failed, report.remaining) == (6, [], 0)
        assert w.exists("/Media/Lost/S01/Lost.S01E01.mkv")
        assert w.exists("/Media/Lost/S01/Lost.S01E02.mkv")
        assert not w.exists("/Inbox/最新地址广告.txt")
        # Both moves went out as one batch request.
        assert w.drive.calls.count("file_batch_move") == 1

        # 3. idempotent: the index was updated as it went, so nothing is left.
        assert (await w.plan([SHOWS, ADS])).is_empty
        with pytest.raises(WmsError, match="applied"):
            await plans.apply(w.ctx, plan_id)

        # 4. the audit trail has every change, with its before snapshot.
        audit = await w.store.audit_entries(plan_id=plan_id)
        assert sorted(e["action"] for e in audit) == [
            "create_folder", "move", "move", "rename", "rename", "trash"]
        by_type = {e["action"]: e for e in audit}
        assert by_type["rename"]["before"]["name"] in ("lost.S01E01.mkv", "lost.S01E02.mkv")

        # 5. undo: the move, then the rename, then the trash.
        move = next(e for e in audit if e["action"] == "move"
                    and e["after"]["path"].endswith("E01.mkv"))
        preview = await plans.undo(w.ctx, move["id"], apply_now=False)
        assert not preview.applied and w.exists("/Media/Lost/S01/Lost.S01E01.mkv")
        done = await plans.undo(w.ctx, move["id"], apply_now=True)
        assert done.applied and w.exists("/Inbox/Lost.S01E01.mkv")

        rename = next(e for e in audit if e["action"] == "rename"
                      and e["before"]["name"] == "lost.S01E01.mkv")
        await plans.undo(w.ctx, rename["id"], apply_now=True)
        assert w.exists("/Inbox/lost.S01E01.mkv")

        trash = by_type["trash"]
        await plans.undo(w.ctx, trash["id"], apply_now=True)
        assert w.exists("/Inbox/最新地址广告.txt")
        assert await w.store.node_at("/Inbox/最新地址广告.txt") is not None

        # Undo is recorded, and cannot run twice.
        assert (await w.store.audit_entries(limit=1))[0]["undo_of"] == trash["id"]
        with pytest.raises(Refused, match="already undone"):
            await plans.undo(w.ctx, trash["id"], apply_now=True)

    async def test_undo_refuses_when_the_file_moved_on(self, world):
        w = world
        w.drive.add("/Inbox/lost.S01E01.mkv", size=BIG)
        await w.sync()
        await plans.save_and_apply(w.ctx, await w.plan([SHOWS]))
        rename = next(e for e in await w.store.audit_entries() if e["action"] == "rename")
        # The move after the rename means the file is no longer where the
        # rename left it: undoing the rename alone would be a guess.
        with pytest.raises(Refused, match="changed"):
            await plans.undo(w.ctx, rename["id"], apply_now=True)


# ----------------------------------------------------------------- applying


class TestApplying:
    async def test_the_run_cap_and_limit_resume_where_they_stopped(self, world):
        w = world
        for n in range(5):
            w.drive.add(f"/Inbox/f{n}.txt", size=1)
        await w.sync()
        rule = {"name": "star", "scope": "/Inbox", "actions": [{"rename": {
            "template": "done-{name}"}}]}
        plan_id = await plans.save(w.ctx, await w.plan([rule]))
        first = await plans.apply(w.ctx, plan_id, limit=2)
        assert (first.applied, first.remaining) == (2, 3)
        assert (await plans.get(w.ctx, plan_id))["status"] == "partial"
        w.ctx.config.runtime.max_actions_per_run = 2
        second = await plans.apply(w.ctx, plan_id)
        assert (second.applied, second.remaining) == (2, 1)
        third = await plans.apply(w.ctx, plan_id)
        assert third.remaining == 0
        row = await plans.get(w.ctx, plan_id)
        assert row["status"] == "applied" and row["result"]["applied"] == 5
        assert w.drive.calls.count("file_rename") == 5

    async def test_an_action_already_done_is_skipped_not_repeated(self, world):
        w = world
        w.drive.add("/Inbox/a.txt", size=1)
        await w.sync()
        plan_id = await plans.save(w.ctx, await w.plan([
            {"name": "r", "scope": "/Inbox", "actions": [{"rename": {"template": "b.txt"}}]}]))
        # Someone did it by hand, and a stocktake saw it.
        w.drive.items[w.drive.id_at("/Inbox/a.txt")]["name"] = "b.txt"
        await w.sync()
        report = await plans.apply(w.ctx, plan_id)
        assert report.applied == 0 and report.skipped == {"done": 1}
        assert "file_rename" not in w.drive.calls

    async def test_a_file_that_changed_since_the_plan_is_left_alone(self, world):
        w = world
        w.drive.add("/Inbox/a.txt", size=1)
        await w.sync()
        plan_id = await plans.save(w.ctx, await w.plan([
            {"name": "r", "scope": "/Inbox", "actions": [{"rename": {"template": "b.txt"}}]}]))
        w.drive.items[w.drive.id_at("/Inbox/a.txt")]["name"] = "c.txt"
        await w.sync()
        report = await plans.apply(w.ctx, plan_id)
        assert report.skipped == {"changed": 1}

    async def test_a_failure_skips_that_files_later_steps_only(self, world):
        w = world
        w.drive.add("/Inbox/a.S01E01.mkv", size=BIG)
        w.drive.add("/Inbox/b.S01E01.mkv", size=BIG)
        await w.sync()
        plan_id = await plans.save(w.ctx, await w.plan([SHOWS]))
        w.drive.fail_next.append(PikpakException("file is locked"))
        report = await plans.apply(w.ctx, plan_id)
        assert len(report.failed) == 1 and "file is locked" in report.failed[0]["error"]
        assert report.skipped == {"after_failure": 1}
        assert w.exists("/Media/B/S01/B.S01E01.mkv")

    async def test_rate_limiting_stops_the_run_and_keeps_the_place(self, world):
        w = world
        w.drive.add("/Inbox/a.txt", size=1)
        await w.sync()
        plan_id = await plans.save(w.ctx, await w.plan([
            {"name": "r", "scope": "/Inbox", "actions": [{"rename": {"template": "b.txt"}}]}]))
        w.drive.fail_next.extend([PikpakException("operation too frequent")] * 4)
        report = await plans.apply(w.ctx, plan_id)
        assert report.stopped and report.remaining == 1
        assert (await plans.get(w.ctx, plan_id))["status"] == "partial"
        again = await plans.apply(w.ctx, plan_id)
        assert again.applied == 1 and w.exists("/Inbox/b.txt")

    async def test_many_moves_go_out_in_batches_of_a_hundred(self, world):
        w = world
        for n in range(150):
            w.drive.add(f"/Inbox/f{n:03}.bin", size=1)
        await w.sync()
        _, report = await plans.save_and_apply(w.ctx, await w.plan([
            {"name": "m", "scope": "/Inbox", "actions": [{"move": {"to": "/Done"}}]}]))
        assert report.applied == 151  # the folder, then the files
        assert w.drive.calls.count("file_batch_move") == 2

    async def test_the_same_plan_twice_is_stored_once(self, world):
        w = world
        w.drive.add("/Inbox/a.txt", size=1)
        await w.sync()
        rule = [{"name": "r", "scope": "/Inbox", "actions": [{"rename": {"template": "b.txt"}}]}]
        first = await plans.save(w.ctx, await w.plan(rule))
        assert await plans.save(w.ctx, await w.plan(rule)) == first

    async def test_star_undo_unstars_and_share_undo_is_refused(self, world):
        w = world
        w.drive.add("/Media/a.mkv", size=1)
        await w.sync()
        await plans.save_and_apply(w.ctx, await w.plan([
            {"name": "s", "scope": "/Media", "actions": ["star", {"share": {"days": 7}}]}]))
        entries = {e["action"]: e for e in await w.store.audit_entries()}
        assert entries["share"]["after"]["share_url"].startswith("https://")
        await plans.undo(w.ctx, entries["star"]["id"], apply_now=True)
        assert "file_batch_unstar" in w.drive.calls
        with pytest.raises(Refused, match="share"):
            await plans.undo(w.ctx, entries["share"]["id"], apply_now=True)


# ------------------------------------------------------------- the modules


    async def test_a_restored_folder_is_listed_again_by_the_next_stocktake(self, world):
        w = world
        w.drive.add("/Temp/box/inside.bin", size=1)
        await w.sync()
        await plans.save_and_apply(w.ctx, await w.plan([
            {"name": "t", "scope": "/Temp", "actions": ["trash"]}]))
        assert await w.store.node_at("/Temp/box/inside.bin") is None
        entry = (await w.store.audit_entries())[0]
        await plans.undo(w.ctx, entry["id"], apply_now=True)
        await stocktake(w.ctx.client, w.store)  # incremental
        assert await w.store.node_at("/Temp/box/inside.bin") is not None


class TestDedupe:
    async def test_keeps_the_preferred_copy_and_trashes_the_rest(self, world):
        w = world
        w.drive.add("/Inbox/movie.mkv", size=50, hash="H", created="2026-09-01T00:00:00Z")
        w.drive.add("/Media/Movie.mkv", size=50, hash="H", created="2026-09-10T00:00:00Z")
        w.drive.add("/Old/movie (1).mkv", size=50, hash="H", created="2026-08-01T00:00:00Z")
        w.drive.add("/x/odd.bin", size=1, hash="Z")
        w.drive.add("/y/odd.bin", size=2, hash="Z")
        await w.sync()
        plan = await organize.dedupe(w.ctx, keep_under=["/Media"])
        assert sorted(a.before["path"] for a in plan.actions) == [
            "/Inbox/movie.mkv", "/Old/movie (1).mkv"]
        lines = plan.note_lines()
        assert any("different sizes" in line for line in lines)
        # Without a preference the oldest stays.
        plan = await organize.dedupe(w.ctx)
        assert "/Old/movie (1).mkv" not in [a.before["path"] for a in plan.actions]
        await plans.save_and_apply(w.ctx, plan)
        assert (await organize.dedupe(w.ctx)).is_empty


class TestCleanupAndForever:
    RULES = parse_rules({"rules": [{"name": "temp", "stage": "cleanup", "scope": "/Temp",
                                    "match": {"older_than": "30d"}, "actions": ["trash"]}]})

    async def test_cleanup_only_trashes_and_forever_needs_the_switch(self, world):
        w = world
        w.drive.add("/Temp/old.bin", size=1, created="2026-07-01T00:00:00Z")
        w.drive.add("/Temp/new.bin", size=1, created="2026-09-20T00:00:00Z")
        await w.sync()
        plan = await organize.cleanup(w.ctx, self.RULES, at=NOW)
        assert [(a.type, a.before["path"]) for a in plan.actions] == [
            (ActionType.TRASH, "/Temp/old.bin")]

        forever = await organize.cleanup(w.ctx, self.RULES, forever=True, at=NOW)
        plan_id = await plans.save(w.ctx, forever)
        with pytest.raises(WmsError, match="not enabled"):
            await plans.apply(w.ctx, plan_id, allow_forever=True)  # config switch off
        w.ctx.config.runtime.allow_permanent_delete = True
        with pytest.raises(WmsError, match="not enabled"):
            await plans.apply(w.ctx, plan_id)  # the flag not given
        await plans.apply(w.ctx, plan_id, allow_forever=True)
        assert "delete_forever" in w.drive.calls

    async def test_no_scheduled_job_deletes_forever(self, world, tmp_path):
        w = world
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text(json.dumps({"rules": [
            {"name": "temp", "stage": "cleanup", "scope": "/Temp", "actions": ["trash"]}]}))
        w.ctx.config.rules_file = rules_file
        w.ctx.config.runtime.allow_permanent_delete = True
        w.drive.add("/Temp/old.bin", size=1)
        await jobs.run_job(w.ctx, ScheduledJob(name="cleanup", cron="* * * * *", apply=True))
        assert "delete_to_trash" in w.drive.calls
        assert "delete_forever" not in w.drive.calls


class TestLayout:
    async def test_creates_what_is_missing_and_undo_only_trashes_what_it_made(self, world):
        w = world
        w.ctx.config.layout.ensure = ["/Inbox", "/Media/电影"]
        w.drive.add("/Inbox", folder=True)
        # The index has never seen /Inbox, so the plan includes it; applying
        # finds it exists and says so, and undo will not trash it.
        _, report = await plans.save_and_apply(w.ctx, await organize.layout(w.ctx))
        assert report.applied == 2 and w.exists("/Media/电影")
        entries = {e["after"]["path"]: e for e in await w.store.audit_entries()}
        with pytest.raises(Refused, match="existed"):
            await plans.undo(w.ctx, entries["/Inbox"]["id"], apply_now=True)
        await plans.undo(w.ctx, entries["/Media/电影"]["id"], apply_now=True)
        assert not w.exists("/Media/电影")
        assert (await organize.layout(w.ctx)).actions[0].after["path"] == "/Media/电影"

    async def test_undo_will_not_trash_a_folder_that_has_filled_up(self, world):
        w = world
        w.ctx.config.layout.ensure = ["/Media"]
        await plans.save_and_apply(w.ctx, await organize.layout(w.ctx))
        w.drive.add("/Media/kept.mkv", size=1)
        await w.sync()
        entry = (await w.store.audit_entries())[0]
        with pytest.raises(Refused, match="not empty"):
            await plans.undo(w.ctx, entry["id"], apply_now=True)


class TestInbound:
    async def test_offline_download_once_per_source_and_polled(self, world):
        w = world
        magnet = "magnet:?xt=urn:btih:abc&dn=film.mkv"
        dry = await inbound.inbound(w.ctx, magnet)
        assert not dry.applied and dry.target == "/Inbox" and writes(w.drive) == []
        done = await inbound.inbound(w.ctx, magnet, apply_now=True)
        assert done.applied and done.task_id
        again = await inbound.inbound(w.ctx, magnet, apply_now=True)
        assert again.existing is not None and w.drive.calls.count("offline_download") == 1
        assert (await w.store.audit_entries())[0]["action"] == "inbound"

        w.drive.tasks[0]["phase"] = "PHASE_TYPE_COMPLETE"
        report = await inbound.poll(w.ctx)
        assert (report.checked, report.finished) == (1, 1)
        assert (await w.store.task_for("offline", magnet))["phase"] == "DONE"
        assert (await inbound.poll(w.ctx)).checked == 0

    async def test_share_links_are_restored(self, world):
        w = world
        result = await inbound.inbound(w.ctx, "https://mypikpak.com/s/VOabc123", apply_now=True)
        assert result.kind == "share_restore" and result.names == ["shared.mkv"]
        assert "restore" in w.drive.calls

    async def test_anything_else_is_refused(self, world):
        with pytest.raises(WmsError) as info:
            await inbound.inbound(world.ctx, "hello there", apply_now=True)
        assert "Not a magnet" in info.value.display()


class TestOutbound:
    async def test_links_are_shown_and_never_stored(self, world):
        w = world
        w.drive.add("/Media/a.mkv", size=3)
        await w.sync()
        plan = await outbound.plan_paths(w.ctx, ["/Media"])
        _, report = await plans.save_and_apply(w.ctx, plan,
                                               deliver=outbound.make_deliver(w.ctx))
        assert any("https://download.example/" in line for line in report.outputs)
        entry = (await w.store.audit_entries())[0]
        assert entry["action"] == "outbound"
        assert "download.example" not in json.dumps(entry)

    async def test_local_fetches_through_a_part_file_and_skips_what_is_there(
        self, world, tmp_path
    ):
        w = world
        w.drive.add("/Media/Show/a.mkv", size=3)
        await w.sync()
        w.ctx.config.outbound.local_dir = tmp_path / "media"
        fetched = []

        async def fetch(url, target):
            assert target.name.endswith(".part")
            fetched.append(url)
            target.write_bytes(b"abc")
            return 3

        deliver = outbound.make_deliver(w.ctx, downloader="local", fetch=fetch)
        plan = await outbound.plan_paths(w.ctx, ["/Media/Show/a.mkv"], to="PikPak")
        await plans.save_and_apply(w.ctx, plan, deliver=deliver)
        target = tmp_path / "media" / "PikPak" / "a.mkv"
        assert target.read_bytes() == b"abc"
        assert not list(target.parent.glob("*.part"))
        # Same file again: already there, nothing fetched.
        again = await outbound.plan_paths(w.ctx, ["/Media/Show/a.mkv"], to="PikPak")
        again.note("plan.note", text="second")  # a different plan, not the stored one
        await plans.save_and_apply(w.ctx, again, deliver=deliver)
        assert len(fetched) == 1

    async def test_a_short_download_leaves_nothing_behind(self, world, tmp_path):
        w = world
        w.drive.add("/Media/a.mkv", size=10)
        await w.sync()
        w.ctx.config.outbound.local_dir = tmp_path

        async def short(url, target):
            target.write_bytes(b"abc")
            return 3

        deliver = outbound.make_deliver(w.ctx, downloader="local", fetch=short)
        _, report = await plans.save_and_apply(
            w.ctx, await outbound.plan_paths(w.ctx, ["/Media/a.mkv"]), deliver=deliver)
        assert "3 of 10" in report.failed[0]["error"]
        assert not any(p.name.startswith("a.mkv") for p in tmp_path.iterdir())

    async def test_aria2_gets_the_secret_from_the_environment(self, world, monkeypatch):
        w = world
        w.drive.add("/Media/a.mkv", size=1)
        await w.sync()
        monkeypatch.setenv("ARIA2_SECRET", "s3cret")
        sent = []

        async def rpc(url, body):
            sent.append((url, body))
            return "gid1"

        deliver = outbound.make_deliver(w.ctx, downloader="aria2", rpc=rpc)
        await plans.save_and_apply(w.ctx, await outbound.plan_paths(w.ctx, ["/Media/a.mkv"],
                                                                   to="tv"), deliver=deliver)
        _url, body = sent[0]
        assert body["method"] == "aria2.addUri"
        assert body["params"][0] == "token:s3cret"
        assert body["params"][2] == {"dir": "/downloads/tv", "out": "a.mkv"}
        entry = (await w.store.audit_entries())[0]
        assert entry["after"]["gid"] == "gid1" and "s3cret" not in json.dumps(entry)

    def test_local_paths_cannot_escape_the_folder(self, tmp_path):
        target = outbound.local_target(tmp_path, "../../etc", "../passwd")
        assert tmp_path in target.parents


# -------------------------------------------------------------- scheduling


class TestJobs:
    async def test_organize_job_plans_or_applies_as_configured(self, world, tmp_path):
        w = world
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text(json.dumps({"rules": [SHOWS]}))
        w.ctx.config.rules_file = rules_file
        w.drive.add("/Inbox/lost.S01E01.mkv", size=BIG)

        job = ScheduledJob(name="organize", cron="15 * * * *")
        first = await jobs.run_job(w.ctx, job)  # stocktakes first, then plans
        assert first.plan_id and writes(w.drive) == []
        second = await jobs.run_job(w.ctx, job)
        assert second.plan_id == first.plan_id  # same pending plan, not a new one

        applied = await jobs.run_job(w.ctx, job.model_copy(update={"apply": True}))
        assert w.exists("/Media/Lost/S01/Lost.S01E01.mkv")
        assert applied.report is not None and applied.report.applied == 3

    async def test_a_rule_scope_missing_from_the_drive_does_not_fail_the_job(
        self, world, tmp_path
    ):
        w = world
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text(json.dumps({"rules": [SHOWS]}))  # scope /Inbox
        w.ctx.config.rules_file = rules_file
        w.drive.add("/Media/a.mkv")
        result = await jobs.run_job(w.ctx, ScheduledJob(name="organize", cron="* * * * *"))
        assert result.plan_id is None and "nothing to do" in result.summary

    async def test_stocktake_and_poll_jobs(self, world):
        w = world
        w.drive.add("/Inbox/a.mkv")
        result = await jobs.run_job(w.ctx, ScheduledJob(name="stocktake", cron="* * * * *"))
        assert "stocktake" in result.summary
        assert await w.store.node_at("/Inbox/a.mkv") is not None
        polled = await jobs.run_job(w.ctx, ScheduledJob(name="inbound-poll", cron="* * * * *"))
        assert "0 download(s) checked" in polled.summary

    def test_unknown_jobs_are_refused(self):
        with pytest.raises(ValueError, match="unknown job"):
            ScheduledJob(name="delete-everything", cron="* * * * *")


class TestScheduler:
    async def test_jobs_are_scheduled_in_the_configured_zone(self, world):
        w = world
        w.ctx.config.schedule.jobs = [
            ScheduledJob(name="stocktake", cron="*/30 * * * *"),
            ScheduledJob(name="cleanup", cron="30 4 * * *", enabled=False),
        ]
        scheduler = WmsScheduler(w.ctx)
        scheduler.start()
        try:
            assert scheduler.scheduled() == [("stocktake", "Asia/Shanghai")]
        finally:
            scheduler.shutdown()

    async def test_a_failing_job_is_logged_not_raised(self, world, caplog):
        w = world
        w.ctx.config.rules_file = Path("/nonexistent/rules.yaml")
        seen = []

        async def on_result(result):
            seen.append(result)

        scheduler = WmsScheduler(w.ctx, on_result=on_result)
        assert await scheduler.run(ScheduledJob(name="organize", cron="* * * * *")) is None
        assert "WMS job organize failed" in caplog.text
        await scheduler.run(ScheduledJob(name="stocktake", cron="* * * * *"))
        assert [r.name for r in seen] == ["stocktake"]


# ------------------------------------------------------------------ store


async def test_an_m1_database_gains_the_new_audit_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, "
        "file_id TEXT NOT NULL, before TEXT NOT NULL DEFAULT '{}', after TEXT NOT NULL "
        "DEFAULT '{}', rule_name TEXT NOT NULL DEFAULT '', dry_run INTEGER NOT NULL "
        "DEFAULT 1, at TEXT NOT NULL);"
        "INSERT INTO audit (action, file_id, at) VALUES ('rename', 'x', '2026-09-01');"
    )
    conn.commit()
    conn.close()
    async with Store(path) as store:
        entry = await store.audit_entry(1)
        assert entry["action"] == "rename" and entry["plan_id"] is None


# -------------------------------------------------------------------- CLI


runner = CliRunner()


@pytest.fixture
def cli_world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    config = tmp_path / "wms.yaml"
    config.write_text("ratelimit:\n  requests_per_second: 100000\n  burst: 100000\n")
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(json.dumps({"rules": [SHOWS]}))
    monkeypatch.setenv("WMS_CONFIG", str(config))
    monkeypatch.setenv("WMS_RULES", str(rules_file))
    monkeypatch.setenv("WMS_LANG", "en")
    drive = FakeDrive()
    drive.add("/Inbox/lost.S01E01.mkv", size=BIG)
    monkeypatch.setattr(cli.state, "provider_factory", lambda config: provider_for(drive))
    return drive


def invoke(*args: str):
    result = runner.invoke(cli.app, list(args))
    return result


class TestCommandLine:
    def test_the_whole_loop(self, cli_world):
        assert invoke("stocktake").exit_code == 0
        result = invoke("rules", "--check")
        assert "1 rule(s)" in result.output

        result = invoke("organize")
        assert result.exit_code == 0, result.output
        assert "Dry run: nothing changed. Apply it with: wms apply 1" in result.output
        assert writes(cli_world) == []

        result = invoke("plans")
        assert "pending" in result.output
        result = invoke("apply", "1")
        assert "Plan 1: 3 applied" in result.output

        result = invoke("audit")
        assert "rename" in result.output and "move" in result.output
        audit_id = next(line.split()[1] for line in result.output.splitlines()
                        if "move" in line and "│" in line)
        result = invoke("undo", audit_id)
        assert "wms undo" in result.output  # a preview first
        result = invoke("undo", audit_id, "--apply")
        assert "undone" in result.output, result.output
        assert "/Inbox/Lost.S01E01.mkv" in [cli_world.path_of(i) for i in cli_world.items]

    def test_forever_is_refused_without_the_switch(self, cli_world):
        result = invoke("cleanup", "--forever", "--apply", "--yes")
        assert result.exit_code == 1
        assert "Permanent deletion is off" in result.output

    def test_events_raw_prints_pikpaks_answer(self, cli_world):
        result = invoke("events", "--raw", "--limit", "5")
        assert result.exit_code == 0
        assert json.loads(result.output) == {"events": [], "next_page_token": ""}

    def test_inbound_is_a_dry_run_until_asked(self, cli_world):
        result = invoke("inbound", "magnet:?xt=urn:btih:abc")
        assert "Would take in" in result.output and "offline_download" not in cli_world.calls
        result = invoke("inbound", "magnet:?xt=urn:btih:abc", "--apply")
        assert "Taken in" in result.output

    def test_a_broken_rules_file_is_a_clear_error(self, cli_world, tmp_path, monkeypatch):
        bad = tmp_path / "bad.yaml"
        bad.write_text("rules:\n  - name: x\n    match: {min_sise: 1GB}\n    actions: [trash]\n")
        monkeypatch.setenv("WMS_RULES", str(bad))
        result = invoke("organize")
        assert result.exit_code == 1 and "min_sise" in result.output

    def test_chinese_plan(self, cli_world, monkeypatch):
        monkeypatch.setenv("WMS_LANG", "zh")
        set_language(None)  # follow the environment, as the real command does
        invoke("stocktake")
        result = invoke("organize")
        assert "计划 1\uff08organize\uff09" in result.output
        assert "只是计划" in result.output
