"""M7 §8: a drive built from the §0 numbers, and a snapshot of every plan.

The snapshots in ``tests/snapshots/m7/`` hold, for each rule of M7, how many
actions of each kind it plans over the fixture, the first lines of the plan
as a person reads them, and a hash of the whole plan. A change to the rules
that moves one file differently changes the hash: look at the new lines,
and if they are right, rewrite the snapshots with

    WMS_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_wms_m7_fixture.py
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from pathlib import Path

import pytest
from m7_fixture import INBOXES, OTHERS, PROTECTED, GiB, build, load_into
from wms_fakes import FakeDrive, provider_for

from pikpak_wms.config import Config
from pikpak_wms.core.client import WmsClient
from pikpak_wms.core.models import ActionType, Plan
from pikpak_wms.core.ratelimit import TokenBucket
from pikpak_wms.i18n import set_language
from pikpak_wms.ops import organize, plans, protect, tidy
from pikpak_wms.ops.context import Context
from pikpak_wms.rules.schema import TidySpec
from pikpak_wms.store.db import Store

SNAPSHOTS = Path(__file__).parent / "snapshots" / "m7"
UPDATE = os.environ.get("WMS_UPDATE_SNAPSHOTS") == "1"


@pytest.fixture(scope="module")
def fixture():
    return build()


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language(None)


def test_the_fixture_has_the_numbers_of_section_0(fixture):
    stats = fixture.stats
    assert stats["entries"] == 80_964
    assert stats["tops"] == 45 and stats["root_files"] == 0
    assert (stats["loose"], stats["loose_tops"], stats["loose_max"]) == (751, 19, 467)
    assert (stats["loose_mp4"], stats["loose_mov"], stats["loose_archives"],
            stats["loose_jpg"]) == (698, 28, 16, 2)
    assert stats["seconds"] == 435
    assert stats["big_seconds"] == 40 and round(stats["big_seconds_gib"] / 1024, 1) == 6.4
    assert (stats["huge_files"], stats["huge_files_gib"]) == (155, 1070)
    assert round(stats["total_gib"] / 1024, 1) == 9.7
    assert len(fixture.shared) == 20


def summary(plan: Plan) -> dict:
    body = json.dumps([a.to_dict() for a in plan.actions], sort_keys=True, ensure_ascii=False)
    return {
        "source": plan.source,
        "actions": len(plan.actions),
        "by_rule": dict(sorted(Counter(
            f"{a.rule_name}:{a.type}" for a in plan.actions).items())),
        "first": plans.plan_lines(plan, limit=12)[1:13],
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
    }


def check(name: str, data: dict) -> None:
    path = SNAPSHOTS / f"{name}.json"
    text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if UPDATE or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if not UPDATE:
            pytest.fail(f"wrote a new snapshot {path.name}; check it and run again")
        return
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(text), (
        f"{path.name} changed; if the new plan is right, rerun with WMS_UPDATE_SNAPSHOTS=1")


@pytest.fixture
async def drive_ctx(tmp_path, fixture):
    drive = FakeDrive()
    # 40 shares on 20 files, as measured (expired ones are listed too).
    drive.shares = [{"share_id": f"s{i}", "file_id": fid,
                     "share_status": "OK" if i % 2 else "EXPIRED"}
                    for i, fid in enumerate(fixture.shared * 2)]
    config = Config()
    config.rules_file = tmp_path / "no-rules.yaml"
    async with Store(tmp_path / "wms.sqlite3") as store:
        await load_into(store, fixture.nodes)
        client = WmsClient(provider_for(drive), limiter=TokenBucket(1e9, 1_000_000))
        yield Context(config=config, client=client, store=store)


class TestPlansOverTheFixture:
    async def test_every_rule_against_its_snapshot(self, drive_ctx, fixture):
        ctx = drive_ctx
        spec = TidySpec()
        protection = await protect.load(ctx)
        assert len(protection.roots) == 3 + 20
        timings = {}

        # organize-tree, one part at a time, so each rule has its own snapshot.
        for part in tidy.PARTS:
            started = time.perf_counter()
            planned = await tidy.organize_tree(ctx, spec=spec, parts={part})
            timings[f"tree:{part}"] = round(time.perf_counter() - started, 2)
            for plan in planned:
                protect.apply_to(plan, protection)
            check(f"tree-{part}", {
                "plans": len(planned),
                "actions": sum(len(p) for p in planned),
                "by_rule": dict(sorted(Counter(
                    f"{a.rule_name}:{a.type}" for p in planned for a in p.actions).items())),
                "sha256": hashlib.sha256("".join(summary(p)["sha256"]
                                                 for p in planned).encode()).hexdigest(),
                "first_plan": summary(planned[0]) if planned else None,
            })

        started = time.perf_counter()
        inbox = await tidy.organize_inbox(ctx, spec=spec)
        timings["inbox"] = round(time.perf_counter() - started, 2)
        protect.apply_to(inbox, protection)
        check("inbox", summary(inbox))

        started = time.perf_counter()
        dedupe = await organize.dedupe(ctx)
        timings["dedupe"] = round(time.perf_counter() - started, 2)
        protect.apply_to(dedupe, protection)
        check("dedupe", summary(dedupe))

        started = time.perf_counter()
        report = await tidy.big_report(ctx, spec=spec)
        timings["big-report"] = round(time.perf_counter() - started, 2)
        check("big-report", {"lines": report.lines(),
                             "reclaimable_gib": report.reclaimable // GiB})
        print("M7 planning times over 80,964 entries (s):", json.dumps(timings))

    async def test_what_the_plans_promise(self, drive_ctx, fixture):
        """The invariants behind the snapshots, stated outright."""
        ctx = drive_ctx
        planned = await tidy.organize_tree(ctx, spec=TidySpec())
        ids = [await plans.save(ctx, plan) for plan in planned]
        stored = [(await plans.get(ctx, i))["plan"] for i in ids if i is not None]
        protection = await protect.load(ctx)
        actions = [a for plan in stored for a in plan.actions]

        # §1: nothing protected is touched; no protected or entry folder is tidied.
        assert [a for a in actions if protection.touches(a)] == []
        sources = {plan.source.partition(":")[2] for plan in stored}
        assert not sources & {f"/{name}" for name in PROTECTED + INBOXES}

        # §3.2: every big second-level folder outside the whitelist and the
        # entry folders moves to /大文件/<A>/, whole.
        big_moves = [a for a in actions if a.rule_name == "tidy:big-folder"
                     and a.type is ActionType.MOVE]
        assert len(big_moves) == 27
        assert all(a.after["path"].startswith("/大文件/") for a in big_moves)
        huge = [a for a in actions if a.rule_name == "tidy:big-file"
                and a.type is ActionType.MOVE]
        assert huge and all(int(a.before["size"]) >= 4 * GiB for a in huge)

        # §2: no loose file is left in a tidied top-level folder, bar shared ones.
        loose_left = {n.path for n in fixture.nodes if not n.is_folder
                      and n.path.count("/") == 2 and n.path.split("/")[1] in OTHERS}
        moved = {a.before["path"] for a in actions if a.type in (ActionType.MOVE,
                                                                  ActionType.RENAME)}
        assert {p for p in loose_left - moved if not protection.covers(p)} == set()

        # The same index gives the same plans.
        again = await tidy.organize_tree(ctx, spec=TidySpec())
        assert [summary(p)["sha256"] for p in again] == [
            summary(p)["sha256"] for p in await tidy.organize_tree(ctx, spec=TidySpec())]

    async def test_spot_check_samples(self, drive_ctx):
        planned = await tidy.organize_tree(drive_ctx, spec=TidySpec())
        sample = tidy.sample_moves(planned, 20)
        assert sample == tidy.sample_moves(planned, 20)  # the same every time
        per_folder = Counter(src.split("/")[1] for src, _ in sample)
        assert max(per_folder.values()) <= 20
