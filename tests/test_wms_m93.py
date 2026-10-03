"""M9.3 §A: pause / start / stop for each task, from the bot and from the command line;
the downloader a plan was built with; no signed link in any text."""

from __future__ import annotations

import asyncio
import logging

import pytest
from test_wms_m83 import _outbound_plan, payload
from test_wms_m92 import CdnIO, chinese, until, world  # noqa: F401 - fixtures

from pikpak_wms.core import redact as redact_module
from pikpak_wms.core.errors import WmsError
from pikpak_wms.core.redact import redact
from pikpak_wms.ops import fetch, outbound, plans, taskreq
from pikpak_wms.ops.control import Control, Gate
from pikpak_wms.ops.runs import Runs

SIGNED = ("https://dl-a10b-0861.mypikpak.com/download/abc/file.mkv?"
          "sign=SECRETSIGN&pr=1&userid=USER42&fileid=FILE9&expire=1")


async def start(world, tmp_path, names, io, monkeypatch=None, parallel=None):
    if parallel is not None:
        monkeypatch.setenv("OUTBOUND_PARALLEL_FILES", str(parallel))
    plan_id = await _outbound_plan(world, tmp_path, names=names, size=2000)
    runs = Runs(world.ctx, asyncio.Lock())
    run = await runs.start(plan_id, make_deliver=lambda progress: outbound.make_deliver(
        world.ctx, io=io, progress=progress, plan_id=plan_id))
    return plan_id, runs, run


# ------------------------------------------------------------------ the gate


class TestGate:
    async def test_a_promoted_waiter_goes_first(self):
        gate = Gate(1)
        await gate.acquire("a")
        order: list[str] = []

        async def wait(owner):
            await gate.acquire(owner)
            order.append(owner)
            gate.release()

        tasks = [asyncio.create_task(wait(o)) for o in ("b", "c", "d")]
        await asyncio.sleep(0)
        assert gate.promote("d") and not gate.promote("zzz")
        gate.release()
        await asyncio.gather(*tasks)
        assert order == ["d", "b", "c"]

    async def test_a_waiter_that_asks_for_the_front_is_ahead(self):
        gate = Gate(1)
        await gate.acquire("a")
        order: list[str] = []

        async def wait(owner, front):
            await gate.acquire(owner, front=front)
            order.append(owner)
            gate.release()

        tasks = [asyncio.create_task(wait("b", False)), asyncio.create_task(wait("c", True))]
        await asyncio.sleep(0)
        gate.release()
        await asyncio.gather(*tasks)
        assert order == ["c", "b"]


# ------------------------------------------------------------ pause and start


class TestPauseAndStart:
    async def test_pausing_gives_back_the_slot_and_the_connections(
            self, world, tmp_path, monkeypatch):
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        _pid, _runs, run = await start(world, tmp_path, ("a.mkv", "b.mkv"), io, monkeypatch, 1)
        await until(lambda: len(io.begun) == 1)
        control = run.control
        assert control.tracks[0].state == "active" and control.tracks[1].state == "queued"
        assert control.pause(0)
        await until(lambda: control.tracks[0].state == "paused")
        # The slot went to b at once; a holds no connection.
        await until(lambda: control.tracks[1].state == "active")
        assert control.counts()["paused"] == 1 and control.gate.inside == 1
        assert not control.pause(0)                       # already paused
        media = tmp_path / "media"
        assert (media / "a.mkv.part").exists() and fetch.state_path(media / "a.mkv.part").exists()
        io.gate.set()
        await until(lambda: control.tracks[1].state == "done")
        assert run.active and control.tracks[0].state == "paused"   # the run waits for 开始
        assert control.start(0)
        await asyncio.wait_for(run.finished.wait(), 5)
        assert run.report.applied == 2
        assert (media / "a.mkv").read_bytes() == payload(2000)

    async def test_a_paused_file_resumes_where_it_stopped(self, world, tmp_path):
        io = CdnIO(payload(2000))
        io.gate, io.hold_after = asyncio.Event(), 2
        _pid, _runs, run = await start(world, tmp_path, ("a.mkv",), io)
        await until(lambda: io.started(world.drive.id_at("/Media/a.mkv")))
        await asyncio.sleep(0.05)
        assert run.control.pause(0)
        await until(lambda: run.control.tracks[0].state == "paused")
        seen = len(io.requests)
        io.gate.set()
        assert run.control.start(0)
        await asyncio.wait_for(run.finished.wait(), 5)
        assert (tmp_path / "media" / "a.mkv").read_bytes() == payload(2000)
        assert all(start >= 1000 for start, _ in io.requests[seen:])   # not from byte 0

    async def test_starting_a_queued_file_puts_it_ahead_of_the_others(
            self, world, tmp_path, monkeypatch):
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        _pid, _runs, run = await start(world, tmp_path, ("a.mkv", "b.mkv", "c.mkv"), io,
                                       monkeypatch, 1)
        await until(lambda: len(io.begun) == 1)
        control = run.control
        assert control.start(2)                    # c jumps the queue
        assert not control.start(0)                # a is running: nothing to start
        io.gate.set()
        await asyncio.wait_for(run.finished.wait(), 5)
        started = sorted(control.tracks.values(), key=lambda t: t.started or 0)
        assert [t.name for t in started] == ["a.mkv", "c.mkv", "b.mkv"]

    async def test_a_started_paused_file_takes_the_next_free_slot_ahead_of_the_queue(
            self, world, tmp_path, monkeypatch):
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        _pid, _runs, run = await start(world, tmp_path, ("a.mkv", "b.mkv", "c.mkv"), io,
                                       monkeypatch, 1)
        await until(lambda: len(io.begun) == 1)
        control = run.control
        assert control.pause(0)
        await until(lambda: control.tracks[1].state == "active")
        assert control.start(0)                    # a is waiting again, ahead of c
        assert control.tracks[0].state == "queued"
        io.gate.set()
        await asyncio.wait_for(run.finished.wait(), 5)
        order = [t.name for t in sorted(control.tracks.values(), key=lambda t: t.started or 0)]
        assert order == ["b.mkv", "a.mkv", "c.mkv"]

    async def test_a_queued_file_can_be_paused_and_nothing_is_written(
            self, world, tmp_path, monkeypatch):
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        _pid, _runs, run = await start(world, tmp_path, ("a.mkv", "b.mkv"), io, monkeypatch, 1)
        await until(lambda: len(io.begun) == 1)
        assert run.control.pause(1)
        await until(lambda: run.control.tracks[1].state == "paused")
        io.gate.set()
        await until(lambda: run.control.tracks[0].state == "done")
        await asyncio.sleep(0.05)
        assert run.active and not (tmp_path / "media" / "b.mkv.part").exists()
        assert run.control.pause_all() == 0 and run.control.start_all() == 1
        await asyncio.wait_for(run.finished.wait(), 5)
        assert run.report.applied == 2

    async def test_pause_all_and_start_all(self, world, tmp_path):
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        _pid, _runs, run = await start(world, tmp_path, ("a.mkv", "b.mkv", "c.mkv"), io)
        await until(lambda: len(io.begun) == 3)
        control = run.control
        assert control.pause_all() == 3
        await until(lambda: control.counts()["paused"] == 3)
        assert control.gate.inside == 0
        io.gate.set()
        assert control.start_all() == 3
        await asyncio.wait_for(run.finished.wait(), 5)
        assert run.report.applied == 3

    async def test_a_paused_file_does_not_end_the_plan_and_stop_still_works(
            self, world, tmp_path):
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        plan_id, runs, run = await start(world, tmp_path, ("a.mkv",), io)
        await until(lambda: len(io.begun) == 1)
        assert run.control.pause(0)
        await until(lambda: run.control.tracks[0].state == "paused")
        await asyncio.sleep(0.05)
        assert run.active
        await runs.stop(plan_id)
        assert run.stopped
        row = await plans.get(world.ctx, plan_id)
        assert row["status"] == "pending" and row["progress"] == 0


# ----------------------------------------------------------------- terminate


class TestStop:
    async def test_stop_keeps_the_part_and_logs_it(self, world, tmp_path):
        io = CdnIO(payload(2000))
        io.gate, io.hold_after = asyncio.Event(), 2
        _pid, _runs, run = await start(world, tmp_path, ("a.mkv", "b.mkv"), io)
        await until(lambda: len(io.begun) == 2)
        await asyncio.sleep(0.05)
        assert run.control.cancel(0)
        await until(lambda: run.control.tracks[0].state == "cancelled")
        io.gate.set()
        await asyncio.wait_for(run.finished.wait(), 5)
        media = tmp_path / "media"
        assert (media / "a.mkv.part").exists() and fetch.state_path(media / "a.mkv.part").exists()
        assert run.control.delete_partial(0) == 2
        assert not (media / "a.mkv.part").exists()
        rows = {r["name"]: r["status"] for r in await world.ctx.store.downloads()}
        assert rows == {"a.mkv": "cancelled", "b.mkv": "done"}

    async def test_stop_with_delete_partial_removes_it(self, world, tmp_path):
        io = CdnIO(payload(2000))
        io.gate, io.hold_after = asyncio.Event(), 2
        _pid, _runs, run = await start(world, tmp_path, ("a.mkv", "b.mkv"), io)
        await until(lambda: len(io.begun) == 2)
        await asyncio.sleep(0.05)
        assert run.control.cancel(0, delete_partial=True)
        await until(lambda: run.control.tracks[0].state == "cancelled")
        io.gate.set()
        await asyncio.wait_for(run.finished.wait(), 5)
        media = tmp_path / "media"
        assert not (media / "a.mkv.part").exists()
        assert not fetch.state_path(media / "a.mkv.part").exists()
        assert (media / "b.mkv").exists()

    async def test_delete_partial_only_for_a_stopped_file(self, world, tmp_path):
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        _pid, _runs, run = await start(world, tmp_path, ("a.mkv",), io)
        await until(lambda: len(io.begun) == 1)
        assert run.control.delete_partial(0) == 0           # still running
        io.gate.set()
        await asyncio.wait_for(run.finished.wait(), 5)

    async def test_a_paused_file_can_be_stopped_and_is_logged(self, world, tmp_path):
        io = CdnIO(payload(2000))
        io.gate, io.hold_after = asyncio.Event(), 2
        _pid, _runs, run = await start(world, tmp_path, ("a.mkv",), io)
        await until(lambda: len(io.begun) == 1)
        await asyncio.sleep(0.05)
        assert run.control.pause(0)
        await until(lambda: run.control.tracks[0].state == "paused")
        assert run.control.cancel(0)
        await asyncio.wait_for(run.finished.wait(), 5)
        assert run.control.tracks[0].state == "cancelled"
        assert [r["status"] for r in await world.ctx.store.downloads()] == ["cancelled"]
        assert (tmp_path / "media" / "a.mkv.part").exists()

    async def test_a_queued_file_that_is_stopped_is_logged_too(
            self, world, tmp_path, monkeypatch):
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        _pid, _runs, run = await start(world, tmp_path, ("a.mkv", "b.mkv"), io, monkeypatch, 1)
        await until(lambda: len(io.begun) == 1)
        assert run.control.cancel(1)
        io.gate.set()
        await asyncio.wait_for(run.finished.wait(), 5)
        rows = {r["name"]: r["status"] for r in await world.ctx.store.downloads()}
        assert rows == {"a.mkv": "done", "b.mkv": "cancelled"}


# ------------------------------------------------------- the request table


class TestRequestTable:
    async def test_a_request_is_picked_up_by_the_process_running_the_plan(
            self, world, tmp_path, monkeypatch):
        monkeypatch.setattr(taskreq, "EVERY", 0.05)
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", "b.mkv"), size=2000)
        control = Control(0)
        deliver = outbound.make_deliver(world.ctx, io=io, plan_id=plan_id,
                                        progress=_Tracker(control))
        job = asyncio.create_task(plans.apply(world.ctx, plan_id, deliver=deliver,
                                              control=control))
        await until(lambda: len(io.begun) == 2)
        answered, result = await taskreq.request(world.ctx, "pause", f"{plan_id}:1", wait=3)
        assert answered and result == "ok"
        await until(lambda: control.tracks[0].state == "paused")
        again, result = await taskreq.request(world.ctx, "pause", f"{plan_id}:1", wait=3)
        assert again and result.startswith("refused")
        listed = await taskreq.listing(world.ctx, plan_id)
        assert listed[0]["live"] and {t["state"] for t in listed[0]["tasks"]} == {
            "paused", "active"}
        done, result = await taskreq.request(world.ctx, "stop", f"{plan_id}:1",
                                             delete_partial=True, wait=3)
        assert done and result == "ok"
        io.gate.set()
        report = await asyncio.wait_for(job, 5)
        assert report.applied == 1 and len(report.cancelled) == 1
        assert not (tmp_path / "media" / "a.mkv.part").exists()
        assert (tmp_path / "media" / "b.mkv").exists()
        # The snapshot is withdrawn when the plan ends.
        assert not (await taskreq.snapshots(world.ctx))

    async def test_pause_all_and_start_all_by_request(self, world, tmp_path, monkeypatch):
        monkeypatch.setattr(taskreq, "EVERY", 0.05)
        io = CdnIO(payload(2000))
        io.gate = asyncio.Event()
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", "b.mkv"), size=2000)
        control = Control(0)
        deliver = outbound.make_deliver(world.ctx, io=io, plan_id=plan_id,
                                        progress=_Tracker(control))
        job = asyncio.create_task(plans.apply(world.ctx, plan_id, deliver=deliver,
                                              control=control))
        await until(lambda: len(io.begun) == 2)
        assert (await taskreq.request(world.ctx, "pause", f"{plan_id}:all", wait=3))[0]
        await until(lambda: control.counts()["paused"] == 2)
        io.gate.set()
        assert (await taskreq.request(world.ctx, "start", f"{plan_id}:all", wait=3))[0]
        assert (await asyncio.wait_for(job, 5)).applied == 2

    async def test_a_request_for_a_plan_nothing_runs_stays_open_then_expires(self, world):
        done, _ = await taskreq.request(world.ctx, "pause", "77:1")
        assert not done
        listed = await world.ctx.store.take_requests("task", {"77:1"}, now=10**10)
        assert listed == []                                  # too old to act on
        assert (await world.ctx.store.request_outcome(1)) == (True, "expired")

    async def test_bad_targets_and_verbs(self, world):
        for target in ("x", "5", "5:0", "5:x", ":3"):
            with pytest.raises(WmsError):
                await taskreq.request(world.ctx, "pause", target)
        with pytest.raises(WmsError):
            await taskreq.request(world.ctx, "dance", "5:1")

    async def test_the_saved_state_of_a_plan_nothing_runs(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", "b.mkv"), size=2000)
        (live,) = await taskreq.listing(world.ctx, plan_id)
        assert not live["live"] and [t["state"] for t in live["tasks"]] == ["queued", "queued"]


class _Tracker:
    """What :class:`Runs` hands the deliver function: bytes and the file's track."""

    def __init__(self, control: Control) -> None:
        self.control = control

    def __call__(self, name, received, size) -> None:
        pass

    def track(self, file_id):
        return self.control.track_for(file_id)


# ------------------------------------------------- the stored downloader


class TestStoredDownloader:
    async def test_a_plan_keeps_the_downloader_it_was_built_with(self, world, tmp_path):
        world.drive.add("/Media/a.mkv", size=2000)
        await world.sync()
        plan = await outbound.plan_paths(world.ctx, ["/Media/a.mkv"], downloader="local")
        assert plan.actions[0].after["via"] == "local"
        plain = await outbound.plan_paths(world.ctx, ["/Media/a.mkv"])
        assert "via" not in plain.actions[0].after

    async def test_apply_without_a_flag_uses_it(self, world, tmp_path, monkeypatch):
        world.ctx.config.outbound.local_dir = tmp_path / "media"
        world.drive.add("/Media/a.mkv", size=2000)
        await world.sync()
        plan = await outbound.plan_paths(world.ctx, ["/Media/a.mkv"], downloader="local")
        plan_id = await plans.save(world.ctx, plan)
        monkeypatch.setattr(fetch, "AiohttpIO", lambda: CdnIO(payload(2000)))
        # The way the command line applies a plan: no downloader given, config says "none".
        assert world.ctx.config.outbound.downloader == "none"
        report = await plans.apply(world.ctx, plan_id, deliver=outbound.make_deliver(world.ctx))
        assert report.applied == 1 and not report.outputs
        assert (tmp_path / "media" / "a.mkv").read_bytes() == payload(2000)
        # ...and a download row was written, as for the bot.
        rows = await world.ctx.store.downloads()
        assert [(r["name"], r["status"], r["plan_id"]) for r in rows] == [
            ("a.mkv", "done", plan_id)]


# ---------------------------------------------------------------- redaction


class TestRedaction:
    def test_the_query_goes_and_the_host_stays(self):
        out = redact(f"failed: {SIGNED} (503)")
        assert "SECRETSIGN" not in out and "USER42" not in out and "FILE9" not in out
        assert "https://dl-a10b-0861.mypikpak.com/…?<redacted>" in out and out.endswith("(503)")

    def test_text_without_a_query_is_left_alone(self):
        assert redact("https://example.com/a/b and no query") == (
            "https://example.com/a/b and no query")
        assert redact("plain") == "plain"

    def test_an_error_carries_no_signed_link(self):
        exc = WmsError(f"503 for {SIGNED}", key="outbound.failed", path="/a", error=SIGNED)
        assert "SECRETSIGN" not in str(exc) and "SECRETSIGN" not in exc.display()

    async def test_the_links_mode_hides_the_query_unless_asked(self, world):
        world.drive.add("/Media/a.mkv", size=10)
        world.drive.download_urls = {}
        await world.sync()
        node = await world.ctx.store.node_at("/Media/a.mkv")

        async def signed(_file_id):
            return SIGNED

        world.ctx.client.download_url = signed
        for shown, want in ((False, False), (True, True)):
            deliver = outbound.make_deliver(world.ctx, downloader="none", show_links=shown)
            out = (await deliver(node, ""))["_output"]
            assert ("SECRETSIGN" in out) is want
            assert "dl-a10b-0861.mypikpak.com" in out

    async def test_a_failure_from_the_network_is_redacted_everywhere(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv",), size=2000)

        class Broken(CdnIO):
            async def probe(self, url):
                raise RuntimeError(f"403, message='Forbidden', url='{SIGNED}'")

        deliver = outbound.make_deliver(world.ctx, io=Broken(b""), plan_id=plan_id)
        report = await plans.apply(world.ctx, plan_id, deliver=deliver)
        assert len(report.failed) == 1
        blob = str(report.failed) + report.summary()
        row = await plans.get(world.ctx, plan_id)
        blob += str(row["result"]) + str(await world.ctx.store.downloads())
        assert "SECRETSIGN" not in blob and "USER42" not in blob and "redacted" in blob

    def test_log_records_are_redacted(self, caplog):
        redact_module.install_log_redaction()
        log = logging.getLogger("redaction-test")
        with caplog.at_level(logging.INFO):
            log.info("fetching %s", SIGNED)
            try:
                raise RuntimeError(SIGNED)
            except RuntimeError:
                log.exception("boom")
        assert "SECRETSIGN" not in caplog.text and "<redacted>" in caplog.text


# ------------------------------------------------- the download log, tidied


class TestLogCleanup:
    async def test_duplicates_are_merged_keeping_the_best_and_what_the_others_knew(
            self, tmp_path):
        import sqlite3

        from pikpak_wms.store.db import Store

        path = tmp_path / "wms.sqlite3"
        async with Store(path) as store:
            await store.add_download(name="a.mkv", size=5, status="done", dest_path="/x/a.mkv",
                                     source="scan", file_id="F1", hash="H1")
            await store.add_download(name="a.mkv", size=5, status="done", dest_path="/x/a.mkv",
                                     source="plan", plan_id=7, avg_mib_s=3.0)
            await store.add_download(name="a.mkv", size=5, status="done", dest_path="/x/a.mkv",
                                     source="backfill")
            # Different path, different size, failed attempts: none of those are duplicates.
            await store.add_download(name="a.mkv", size=5, status="done", dest_path="/y/a.mkv")
            await store.add_download(name="a.mkv", size=6, status="done", dest_path="/x/a.mkv")
            await store.add_download(name="a.mkv", size=5, status="failed")
            await store.add_download(name="a.mkv", size=5, status="failed")
        async with Store(path) as store:
            rows = await store.downloads(limit=50)
        assert len(rows) == 5
        (keeper,) = [r for r in rows if r["dest_path"] == "/x/a.mkv" and r["size"] == 5]
        assert (keeper["source"], keeper["plan_id"], keeper["file_id"], keeper["hash"]) == (
            "plan", 7, "F1", "H1")
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 5
        conn.close()
        async with Store(path) as store:                   # a second open changes nothing
            assert len(await store.downloads(limit=50)) == 5

    async def test_the_audit_backfill_does_not_repeat_what_a_scan_logged(self, tmp_path):
        import json
        import sqlite3

        from pikpak_wms.store.db import AUDIT_BACKFILLED, Store

        path = tmp_path / "wms.sqlite3"
        async with Store(path) as store:
            await store.add_download(name="v.mp4", size=9, status="done",
                                     dest_path="/lib/v.mp4", source="scan")
        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM meta WHERE key = ?", (AUDIT_BACKFILLED,))
        conn.execute(
            "INSERT INTO audit (file_id, action, before, after, at, dry_run, rule_name) "
            "VALUES ('F', 'outbound', ?, ?, '2026-10-01T00:00:00+00:00', 0, 'outbound')",
            (json.dumps({"name": "v.mp4", "size": 9}), json.dumps({"path": "/lib/v.mp4"})))
        conn.commit()
        conn.close()
        async with Store(path) as store:
            rows = await store.downloads(limit=10)
        assert [(r["name"], r["source"]) for r in rows] == [("v.mp4", "scan")]

    async def test_failures_in_old_plan_results_become_failed_rows_once(self, tmp_path):
        import json
        import sqlite3

        from pikpak_wms.store.db import FAILED_BACKFILLED, Store

        path = tmp_path / "wms.sqlite3"
        async with Store(path):
            pass
        action = {"type": "outbound", "file_id": "", "before": {
            "name": "wxyz04567.part2.mp4", "path": "/R/wxyz04567.part2.mp4", "size": 77},
            "after": {}, "rule_name": "outbound"}
        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM meta WHERE key = ?", (FAILED_BACKFILLED,))
        conn.execute(
            "INSERT INTO plans (id, source, status, fingerprint, body, progress, result, "
            "created_at, updated_at) VALUES (66, 'outbound', 'applied', 'x', ?, 1, ?, "
            "'2026-10-02T01:00:00+00:00', '2026-10-02T02:00:00+00:00')",
            (json.dumps({"actions": [action]}), json.dumps({"failed": [
                {"path": "/R/wxyz04567.part2.mp4", "action": "outbound",
                 "error": f"Cannot connect to host {SIGNED}"},
                {"path": "/R/x", "action": "rename", "error": "not a download"}]})))
        conn.commit()
        conn.close()
        for _ in range(2):
            async with Store(path) as store:
                rows = await store.downloads(status=["failed"])
        (row,) = rows
        assert (row["name"], row["size"], row["plan_id"], row["source"]) == (
            "wxyz04567.part2.mp4", 77, 66, "backfill")
        assert row["finished_at"].startswith("2026-10-02T02") and "SECRETSIGN" not in row["reason"]

    async def test_a_vanished_source_is_named_in_the_summary(self, world, tmp_path):
        plan_id = await _outbound_plan(world, tmp_path, names=("a.mkv", "gone.part1.mkv"),
                                       size=2000)
        node = await world.ctx.store.node_at("/Media/gone.part1.mkv")
        await world.ctx.store.forget([node.file_id])        # the drive no longer has it
        deliver = outbound.make_deliver(world.ctx, io=CdnIO(payload(2000)), plan_id=plan_id)
        report = await plans.apply(world.ctx, plan_id, deliver=deliver)
        assert report.applied == 1 and report.skipped.get("gone") == 1
        assert report.gone == ["gone.part1.mkv"]
        assert "源文件已不在网盘：gone.part1.mkv" in report.summary()
        row = await plans.get(world.ctx, plan_id)
        assert row["result"]["gone"] == ["gone.part1.mkv"]


# ------------------------------------------------------- the rules layer


class TestSentenceWithAClockTime:
    """`wms do` timed out on the model for this one (M9.2 evidence); the rules take it."""

    def parse(self, text):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from pikpak_wms.nl.rules_parser import RulesTranslator

        tz = ZoneInfo("Asia/Shanghai")
        return RulesTranslator().parse(text, datetime(2026, 10, 3, 10, 0, tzinfo=tz), tz)

    def test_the_sentence_that_timed_out(self):
        query = self.parse("下载 2026年10月2日 02:50 之后入库、还未下载过的视频")
        assert query.intent == "download" and query.filters.kinds == ["video"]
        assert query.filters.not_downloaded
        assert query.filters.created_after == "2026-10-02T02:50:00+08:00"
        assert query.filters.created_before is None

    @pytest.mark.parametrize(("text", "after"), [
        ("下载2026-10-02 02:50之后入库的还没下载的视频", "2026-10-02T02:50:00+08:00"),
        ("下载 10月2日 02:50 之后入库的视频", "2026-10-02T02:50:00+08:00"),
        ("下载昨天 02:50 之后入库的视频", "2026-10-02T02:50:00+08:00"),
        ("下载今天 02:50 之后入库的视频", "2026-10-03T02:50:00+08:00"),
        ("下载 2026年10月2日 之后入库的视频", "2026-10-02T00:00:00+08:00"),
    ])
    def test_the_same_with_other_wordings(self, text, after):
        query = self.parse(text)
        assert query.intent == "download" and query.filters.created_after == after

    def test_a_whole_day_without_a_time_is_still_that_day(self):
        query = self.parse("下载10月2日的视频")
        assert query.filters.created_after == "2026-10-02T00:00:00+08:00"
        assert query.filters.created_before == "2026-10-03T00:00:00+08:00"


# ----------------------------------------------------------- whose clock


def test_nothing_in_the_warehouse_reads_the_machines_local_time():
    """「今天」 is Asia/Shanghai whatever the machine's clock zone says (M9.3 §B.5): every
    read of the time names a zone, and the zone comes from the configuration."""
    import ast
    from pathlib import Path

    import pikpak_wms

    offenders = []
    for path in Path(pikpak_wms.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            name, owner = node.func.attr, ast.unparse(node.func.value)
            bare = not node.args and not node.keywords
            if (name in ("now", "today") and owner.endswith(("datetime", "date")) and bare) \
                    or name in ("utcnow", "localtime", "ctime") or (
                        owner == "time" and name in ("strftime", "mktime")):
                offenders.append(f"{path.name}:{node.lineno} {owner}.{name}()")
    assert offenders == []


class TestDedupeSpellings:
    """Live: `wms downloads --name abcd00123` showed part1 and part2 twice after M9.3."""

    async def test_the_same_file_from_the_container_and_from_the_host_is_merged(self, tmp_path):
        from pikpak_wms.store.db import Store

        path = tmp_path / "wms.sqlite3"
        day = "资源/整理/2026/2026.10/2026.10.2"
        async with Store(path) as store:
            for name, size in (("a.part1.mp4", 10615381232), ("a.part2.mp4", 7256590320)):
                kw = {"name": name, "size": size, "status": "done",
                      "finished_at": "2026-10-02T02:10:00+00:00"}
                await store.add_download(dest_path=f"/library/{day}/{name}", source="backfill",
                                         file_id="F" + name, plan_id=60, **kw)
                await store.add_download(dest_path=f"/volume9/Share/{day}/{name}",
                                         source="scan", **kw)
                # another spelling of the first: doubled and trailing slashes
                await store.add_download(dest_path=f"/library//{day}//{name}", source="scan",
                                         **kw)
        async with Store(path) as store:
            rows = await store.downloads(name="a.part", limit=50)
        assert sorted(r["name"] for r in rows) == ["a.part1.mp4", "a.part2.mp4"]
        assert {r["source"] for r in rows} == {"backfill"} and {r["plan_id"] for r in rows} == {60}

    async def test_different_folders_and_other_sizes_are_not_merged(self, tmp_path):
        from pikpak_wms.store.db import Store, same_place

        assert not same_place("/library/2026/10/1/x.mp4", "/library/2026/10/2/x.mp4")
        assert not same_place("", "/a/b/c/d/x.mp4")
        assert same_place("/library/资源/整理/2026/2026.10/2026.10.2/x",
                          "/vol/资源/整理/2026/2026.10/2026.10.2/x")
        path = tmp_path / "wms.sqlite3"
        async with Store(path) as store:
            for size, folder in ((5, "c"), (5, "d"), (6, "c")):
                await store.add_download(name="x.mp4", size=size, status="done",
                                         dest_path=f"/l/a/b/{folder}/x.mp4")
        async with Store(path) as store:
            assert len(await store.downloads(limit=10)) == 3

    async def test_a_row_without_a_path_joins_the_one_place(self, tmp_path):
        from pikpak_wms.store.db import Store

        path = tmp_path / "wms.sqlite3"
        async with Store(path) as store:
            await store.add_download(name="x.mp4", size=5, status="done", source="scan",
                                     dest_path="/l/a/b/c/x.mp4")
            await store.add_download(name="x.mp4", size=5, status="done", source="manual")
        async with Store(path) as store:
            (row,) = await store.downloads(limit=10)
        assert row["dest_path"] == "/l/a/b/c/x.mp4"

    async def test_a_scan_does_not_log_what_the_audit_logged_under_another_spelling(
            self, tmp_path):
        from pikpak_wms.store.db import Store

        async with Store(tmp_path / "w.sqlite3") as store:
            await store.add_download(name="x.mp4", size=5, status="done", source="backfill",
                                     dest_path="/library/资源/整理/2026/2026.10/2026.10.2/x.mp4")
            assert await store.download_placed(
                "/host/Share/资源/整理/2026/2026.10/2026.10.2/x.mp4", "x.mp4", 5)
            assert not await store.download_placed("/host/other/x.mp4", "x.mp4", 5)
