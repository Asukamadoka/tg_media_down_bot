"""WMS M7 (docs/wms/M7-organize-rules.md): the whitelist, tidying the tree,
shelving the entry folders, the big-files report, and their jobs.

The bot side is in ``test_wms_m7_bot.py``; the §0-sized fixture and the plan
snapshots are in ``test_wms_m7_fixture.py``.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pikpakapi.PikpakException import PikpakException
from wms_fakes import FakeDrive, provider_for

from pikpak_wms.config import BUILTIN_JOBS, Config, ScheduledJob
from pikpak_wms.core.client import WmsClient
from pikpak_wms.core.errors import WmsError
from pikpak_wms.core.models import Action, ActionType, FileNode, Kind, Plan
from pikpak_wms.core.ratelimit import TokenBucket
from pikpak_wms.i18n import set_language
from pikpak_wms.ops import jobs, organize, plans, protect, tidy
from pikpak_wms.ops.context import Context
from pikpak_wms.ops.stocktake import stocktake
from pikpak_wms.rules.names import group_names, is_messy, name_key
from pikpak_wms.rules.schema import TidySpec, parse_rules
from pikpak_wms.store.db import Store

GB = 1024**3
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language(None)


async def _no_sleep(_seconds: float) -> None:
    return None


class World:
    def __init__(self, drive: FakeDrive, ctx: Context) -> None:
        self.drive = drive
        self.ctx = ctx

    @property
    def store(self) -> Store:
        return self.ctx.store

    async def sync(self) -> None:
        await stocktake(self.ctx.client, self.store, full=True)
        self.ctx.cache.clear()  # the share list is re-read after the drive changes

    def exists(self, path: str) -> bool:
        try:
            self.drive.id_at(path)
        except KeyError:
            return False
        return True

    def share(self, path: str) -> None:
        self.drive.shares.append({"share_id": f"s{len(self.drive.shares)}",
                                  "file_id": self.drive.id_at(path), "share_status": "OK"})
        self.ctx.cache.clear()

    async def tree(self, **kw) -> list[Plan]:
        return await tidy.organize_tree(self.ctx, at=NOW, spec=kw.pop("spec", TidySpec()), **kw)

    async def inbox(self, **kw) -> Plan:
        return await tidy.organize_inbox(self.ctx, at=NOW, spec=kw.pop("spec", TidySpec()), **kw)


@pytest.fixture
async def world(tmp_path):
    drive = FakeDrive()
    config = Config()
    config.rules_file = tmp_path / "no-rules.yaml"  # the defaults of TidySpec
    async with Store(tmp_path / "wms.sqlite3") as store:
        client = WmsClient(provider_for(drive), limiter=TokenBucket(1e9, 1_000_000),
                           sleep=_no_sleep)
        yield World(drive, Context(config=config, client=client, store=store))


def moves(plan: Plan) -> list[tuple[str, str]]:
    return [(a.before["path"], a.after["path"]) for a in plan.actions
            if a.type is ActionType.MOVE]


def trashed(plan: Plan) -> list[str]:
    return [a.before["path"] for a in plan.actions if a.type is ActionType.TRASH]


# ------------------------------------------------------------------ §1


class TestWhitelist:
    async def test_the_named_folders_are_protected_by_default(self, world):
        assert Config().protect.paths == ["/收藏", "/Cosplaytales Nako EP#1-24", "/小千"]
        protection = await protect.load(world.ctx)
        assert protection.covers("/小千/a.mp4")
        assert protection.covers("/收藏")
        assert not protection.covers("/小千2/a.mp4")  # a prefix is not a folder
        assert not protection.covers("/Other")

    async def test_every_shared_file_is_protected_expired_ones_too(self, world):
        world.drive.add("/A/keep.mp4", size=5)
        world.drive.add("/B/folder/x.mp4", size=5)
        world.drive.share_page = 1  # the list is paged: every page is read
        await world.sync()
        world.share("/A/keep.mp4")
        world.drive.shares.append({"share_id": "old", "share_status": "EXPIRED",
                                   "file_id": world.drive.id_at("/B/folder")})
        protection = await protect.load(world.ctx)
        assert protection.covers("/A/keep.mp4")
        assert protection.covers("/B/folder/x.mp4")  # the whole shared subtree
        assert protection.holds("/B")
        assert world.drive.calls.count("share_list") == 2

    async def test_a_share_not_in_the_index_is_counted(self, world):
        world.drive.shares.append({"share_id": "s", "file_id": "nowhere"})
        protection = await protect.load(world.ctx)
        assert {"key": "protect.shares_unresolved", "args": {"count": 1}} in protection.notes

    async def test_no_share_list_means_no_plan(self, world):
        world.drive.add("/A/x.mp4", size=1)
        await world.sync()
        world.drive.fail_next.append(PikpakException("server error"))
        with pytest.raises(WmsError) as caught:
            await protect.load(world.ctx)
        assert caught.value.key == "protect.shares_failed"

    async def test_the_last_share_list_stands_in_when_pikpak_is_down(self, world):
        world.drive.add("/A/x.mp4", size=1)
        await world.sync()
        world.share("/A/x.mp4")
        await protect.load(world.ctx)
        world.ctx.cache.clear()
        world.drive.fail_next.append(PikpakException("server error"))
        protection = await protect.load(world.ctx)
        assert protection.covers("/A/x.mp4")
        assert protection.notes[0]["key"] == "protect.shares_cached"

    def test_apply_to_drops_source_destination_and_parents(self):
        protection = protect.Protection(roots=("/收藏", "/A/shared.mp4"))
        plan = Plan(actions=[
            Action(ActionType.MOVE, "1", before={"path": "/收藏/x"},
                   after={"path": "/B/x", "parent_path": "/B"}),
            Action(ActionType.CREATE_FOLDER, "", after={"path": "/收藏/new"}),
            Action(ActionType.MOVE, "2", before={"path": "/B/y"},
                   after={"path": "/收藏/new/y", "parent_path": "/收藏/new"}),
            Action(ActionType.MOVE, "3", before={"path": "/A", "kind": "folder"},
                   after={"path": "/Z/A", "parent_path": "/Z"}),
            Action(ActionType.CREATE_FOLDER, "", after={"path": "/Z"}),
            Action(ActionType.TRASH, "4", before={"path": "/A"}),
            Action(ActionType.MOVE, "5", before={"path": "/B/z"},
                   after={"path": "/C/z", "parent_path": "/C"}),
        ])
        dropped = protect.apply_to(plan, protection)
        assert dropped == 5
        # /Z only served the dropped move of /A (which holds a shared file).
        assert [(a.type, a.file_id) for a in plan.actions] == [(ActionType.MOVE, "5")]
        assert plan.notes[-1] == {"key": "protect.skipped", "args": {"count": 5}}

    async def test_add_and_remove_at_runtime(self, world):
        assert "/Mine" in await protect.add(world.ctx, "/Mine/")
        assert "/小千" not in await protect.remove(world.ctx, "/小千")
        assert "/小千" in await protect.add(world.ctx, "/小千")
        with pytest.raises(WmsError):
            await protect.remove(world.ctx, "/never")
        with pytest.raises(WmsError):
            await protect.add(world.ctx, "/")

    async def test_plans_save_filters_every_plan(self, world):
        world.drive.add("/小千/a.mp4", size=1)
        world.drive.add("/Inbox/b.mp4", size=1)
        await world.sync()
        ruleset = parse_rules({"rules": [{"name": "all", "scope": "/",
                                          "match": {"kind": "file"}, "actions": ["trash"]}]})
        plan = await organize.organize(world.ctx, ruleset, at=NOW)
        assert len(plan) == 2
        plan_id = await plans.save(world.ctx, plan)
        row = await plans.get(world.ctx, plan_id)
        assert trashed(row["plan"]) == ["/Inbox/b.mp4"]

    async def test_something_shared_after_the_plan_is_skipped_when_applying(self, world):
        world.drive.add("/A/x.mp4", size=1)
        await world.sync()
        ruleset = parse_rules({"rules": [{"name": "t", "scope": "/A", "actions": ["trash"]}]})
        plan_id = await plans.save(world.ctx, await organize.organize(world.ctx, ruleset))
        world.share("/A/x.mp4")
        report = await plans.apply(world.ctx, plan_id)
        assert report.applied == 0 and report.skipped == {"protected": 1}
        assert world.exists("/A/x.mp4")

    async def test_dedupe_keeps_every_protected_copy(self, world):
        world.drive.add("/小千/a.mp4", size=10, hash="H", created="2026-01-02T00:00:00+00:00")
        world.drive.add("/A/a.mp4", size=10, hash="H", created="2026-01-01T00:00:00+00:00")
        world.drive.add("/B/a.mp4", size=10, hash="H", created="2026-01-03T00:00:00+00:00")
        world.drive.add("/C/b.mp4", size=10, hash="G")
        world.drive.add("/D/b.mp4", size=10, hash="G")
        await world.sync()
        world.share("/D/b.mp4")
        plan = await organize.dedupe(world.ctx)
        # The oldest (/A) would have stayed; the protected copy stays instead.
        assert sorted(trashed(plan)) == ["/A/a.mp4", "/B/a.mp4", "/C/b.mp4"]


class TestWhitelistProperty:
    """M7 §1: whatever the rules and the tree, no saved plan touches the whitelist."""

    @pytest.mark.parametrize("seed", range(60))
    async def test_no_plan_ever_touches_protected_content(self, world, seed):
        rng = random.Random(seed)
        tops = ["/收藏", "/小千", "/A", "/B", "/Telegram", "/写真", "/C D"]
        paths: list[str] = []
        for _ in range(rng.randint(15, 45)):
            top = rng.choice(tops)
            depth = rng.randint(0, 3)
            parts = [rng.choice(["x", "y", "Nako", "季", "z z"]) for _ in range(depth)]
            ext = rng.choice(["mp4", "mov", "jpg", "zip", "txt", "url", "mkv"])
            name = rng.choice(["Nako EP{n}", "小千 {n}", "a{n}b{n}c9f", "{n}", "最新地址{n}"])
            path = "/".join([top, *parts, name.format(n=rng.randint(1, 30)) + "." + ext])
            size = rng.choice([1, 10, 2 * 1024**2, 5 * GB, 60 * GB])
            world.drive.add(path, size=size, hash=rng.choice(["H1", "H2", "", "H3"]))
            paths.append(path)
        for _ in range(rng.randint(0, 3)):
            world.drive.add(f"{rng.choice(tops)}/empty{rng.randint(1, 9)}/in", folder=True)
        await world.sync()
        for path in rng.sample(paths, rng.randint(0, 4)):
            target = path if rng.random() < 0.6 else path.rsplit("/", 1)[0]
            if target.count("/") >= 1 and world.exists(target):
                world.share(target)
        if rng.random() < 0.5:
            await protect.add(world.ctx, rng.choice(["/A", "/B/x", "/Telegram"]))
        protection = await protect.load(world.ctx)

        rules = [{"name": f"r{i}", "scope": rng.choice(["/", "/A", "/B"]),
                  "actions": [rng.choice([{"move": {"to": rng.choice(["/Z", "/小千/in"])}},
                                          "trash", {"rename": {"template": "n{name}"}}])]}
                 for i in range(rng.randint(1, 3))]
        planned = [
            *await world.tree(),
            await world.inbox(),
            await organize.dedupe(world.ctx),
            await organize.organize(world.ctx, parse_rules({"rules": rules}), at=NOW),
        ]
        for plan in planned:
            plan_id = await plans.save(world.ctx, plan)
            if plan_id is None:
                continue
            stored = (await plans.get(world.ctx, plan_id))["plan"]
            touching = [a for a in stored.actions if protection.touches(a)]
            assert touching == [], (seed, plan.source, [a.to_dict() for a in touching])


# ------------------------------------------------------------------ names


class TestNames:
    @pytest.mark.parametrize(("name", "key"), [
        ("Nako EP03 1080p x265.mp4", "Nako"),
        ("[www.xx.com] Nako EP04.mp4", "Nako"),
        ("Nako_EP05-part2.mp4", "Nako"),
        ("小千 第3集.mp4", "小千"),
        ("小千（2）.mp4", "小千"),
        ("小千03.mp4", "小千"),
        ("某某合集 4K HDR.mp4", "某某合集"),
        ("旅行 2023-05-12.mov", "旅行"),
        ("[Yuki] 03.mp4", "Yuki"),
        ("Show.S01E02.WEB-DL.mkv", "Show"),
    ])
    def test_keys(self, name, key):
        assert name_key(name) == key

    @pytest.mark.parametrize("name", ["001.mp4", "a1b2c3d4e5f6a7b8.mp4", "IMG_1234.mov",
                                      "VID_20230512_101010.mp4", "20230101_123456.mp4"])
    def test_messy_names(self, name):
        assert is_messy(name_key(name))

    def test_groups_are_deterministic(self):
        names = ["Nako EP02.mp4", "001.mp4", "Nako EP01.mp4", "Cosplay_Nako_01.mp4",
                 "Cosplay_Nakamura_02.mp4", "abc.zip", "abc (1).zip", "lone.mp4"]
        first = group_names(names)
        shuffled = list(reversed(names))
        again = group_names(shuffled)
        as_names = lambda result, pool: (  # noqa: E731
            sorted((g.name, sorted(pool[i] for i in g.members)) for g in result[0]),
            sorted(pool[i] for i in result[1]))
        assert as_names(first, names) == as_names(again, shuffled)
        groups = dict(as_names(first, names)[0])
        assert groups["Nako"] == ["Nako EP01.mp4", "Nako EP02.mp4"]
        assert groups["Cosplay"] == ["Cosplay_Nakamura_02.mp4", "Cosplay_Nako_01.mp4"]
        assert groups["abc"] == ["abc (1).zip", "abc.zip"]
        assert as_names(first, names)[1] == ["001.mp4", "lone.mp4"]

    def test_a_short_prefix_is_not_a_group(self):
        groups, loose = group_names(["ab-x one.mp4", "ab-y two.mp4"])
        assert groups == [] and len(loose) == 2

    def test_a_prefix_is_cut_at_a_word(self):
        groups, _ = group_names(["Summer Beach.mp4", "Summer Bash.mp4"])
        assert [g.name for g in groups] == ["Summer"]


# ------------------------------------------------------------------ §2, §3


class TestTree:
    async def test_loose_files_are_grouped_and_sorted(self, world):
        for name in ["Nako EP01.mp4", "Nako EP02.mp4", "001.mp4", "clip.mov", "pack.zip",
                     "notes.pdf", "pic.jpg"]:
            world.drive.add(f"/Cos/{name}", size=10, hash=name)
        world.drive.add("/Cos/Yuki/old.mp4", size=10)
        world.drive.add("/Cos/Yuki 03.mp4", size=10)
        await world.sync()
        (plan,) = await world.tree()
        assert plan.source == "organize-tree:/Cos"
        assert sorted(moves(plan)) == sorted([
            ("/Cos/Nako EP01.mp4", "/Cos/Nako/Nako EP01.mp4"),
            ("/Cos/Nako EP02.mp4", "/Cos/Nako/Nako EP02.mp4"),
            ("/Cos/001.mp4", "/Cos/杂/001.mp4"),
            ("/Cos/clip.mov", "/Cos/杂/clip.mov"),
            ("/Cos/pack.zip", "/Cos/其他/pack.zip"),
            ("/Cos/notes.pdf", "/Cos/其他/notes.pdf"),
            ("/Cos/pic.jpg", "/写真/杂/pic.jpg"),
            # A single file joins the folder that already has its name.
            ("/Cos/Yuki 03.mp4", "/Cos/Yuki/Yuki 03.mp4"),
        ])

    async def test_an_image_with_a_taken_name_gets_a_short_hash(self, world):
        world.drive.add("/写真/杂/pic.jpg", size=1, hash="ffffff")
        world.drive.add("/Cos/pic.jpg", size=2, hash="abcdef123")
        await world.sync()
        (plan, *_rest) = await world.tree()
        kinds = [(a.type, a.after.get("name") or a.after.get("path")) for a in plan.actions]
        assert (ActionType.RENAME, "pic_abcdef.jpg") in kinds
        assert ("/Cos/pic_abcdef.jpg", "/写真/杂/pic_abcdef.jpg") in moves(plan)

    async def test_slimming(self, world):
        world.drive.add("/A/Show/inner/deeper/e1.mp4", size=10)
        world.drive.add("/A/Show/inner/deeper/e2.mp4", size=10)
        world.drive.add("/A/Empty/sub/subsub", folder=True)
        world.drive.add("/A/Stuff/good.mp4", size=10)
        world.drive.add("/A/Stuff/big.txt", size=50 * 1024**2)       # junk by extension
        world.drive.add("/A/Stuff/最新地址.mp4", size=100)          # junk by word, small
        world.drive.add("/A/Stuff/最新地址合集.mp4", size=5 * 1024**2)  # a word, but big
        await world.sync()
        (plan,) = await world.tree()
        assert sorted(trashed(plan)) == ["/A/Empty", "/A/Show/inner", "/A/Stuff/big.txt",
                                          "/A/Stuff/最新地址.mp4"]
        assert sorted(moves(plan)) == [
            ("/A/Show/inner/deeper/e1.mp4", "/A/Show/e1.mp4"),
            ("/A/Show/inner/deeper/e2.mp4", "/A/Show/e2.mp4"),
        ]
        order = [(a.type, a.before.get("path")) for a in plan.actions]
        # The contents go up before the empty shell goes to the trash.
        assert order.index((ActionType.TRASH, "/A/Show/inner")) > order.index(
            (ActionType.MOVE, "/A/Show/inner/deeper/e2.mp4"))

    async def test_a_chain_whose_inner_name_repeats_is_lifted_one_level_lower(self, world):
        # /A/S holds only "inner", whose innermost folder Z holds another
        # "inner": lifting Z's contents into /A/S would collide with the shell
        # still standing there, so that level is skipped (with a note) and
        # the chain below it, inner → Z, is lifted instead.
        world.drive.add("/A/S/inner/Z/inner/x.mp4", size=10)
        world.drive.add("/A/S/inner/Z/y.mp4", size=10)
        await world.sync()
        (plan,) = await world.tree()
        assert plan.notes[0]["key"] == "conflict.taken"
        assert sorted(moves(plan)) == [("/A/S/inner/Z/inner", "/A/S/inner/inner"),
                                       ("/A/S/inner/Z/y.mp4", "/A/S/inner/y.mp4")]
        assert trashed(plan) == ["/A/S/inner/Z"]

    async def test_big_folders_and_files(self, world):
        world.drive.add("/A/Huge/part1.mkv", size=30 * GB)
        world.drive.add("/A/Huge/part2.mkv", size=30 * GB)
        world.drive.add("/A/Mid/big.mkv", size=5 * GB)
        world.drive.add("/A/Mid/small.mkv", size=1)
        world.drive.add("/A/Mid/deep/deeper/also-big.mkv", size=4 * GB)
        await world.sync()
        (plan,) = await world.tree()
        assert moves(plan) == [
            # Lifted first (deep holds only deeper) ...
            ("/A/Mid/deep/deeper/also-big.mkv", "/A/Mid/deep/also-big.mkv"),
            ("/A/Huge", "/大文件/A/Huge"),
            ("/A/Mid/big.mkv", "/大文件/A/big.mkv"),
            # ... then set apart from the path it will have by then.
            ("/A/Mid/deep/also-big.mkv", "/大文件/A/also-big.mkv"),
        ]

    async def test_special_and_protected_folders_are_left_alone(self, world):
        for top in ["/Telegram", "/Pack From Shared", "/大文件/A", "/小千"]:
            world.drive.add(f"{top}/loose.mp4", size=1)
        world.drive.add("/Cos/loose.mp4", size=1)
        await world.sync()
        assert [p.source for p in await world.tree()] == ["organize-tree:/Cos"]

    async def test_one_folder_or_one_part(self, world):
        world.drive.add("/A/a.mp4", size=1)
        world.drive.add("/B/b.mp4", size=1)
        world.drive.add("/B/X/big.mkv", size=5 * GB)
        await world.sync()
        assert [p.source for p in await world.tree(scope="/B/whatever")] == ["organize-tree:/B"]
        (plan,) = await world.tree(scope="/B", parts={"big"})
        assert moves(plan) == [("/B/X/big.mkv", "/大文件/B/big.mkv")]

    async def test_applying_then_planning_again_finds_nothing(self, world):
        for name in ["Nako EP01.mp4", "Nako EP02.mp4", "x.mov", "y.jpg", "z.zip"]:
            world.drive.add(f"/Cos/{name}", size=10, hash=name)
        world.drive.add("/Cos/S/in/a.mp4", size=10)
        world.drive.add("/Cos/Big/b.mkv", size=5 * GB)
        await world.sync()
        for plan in await world.tree():
            plan_id = await plans.save(world.ctx, plan)
            report = await plans.apply(world.ctx, plan_id)
            assert report.failed == [] and report.applied == len(plan)
        assert world.exists("/Cos/Nako/Nako EP01.mp4")
        assert world.exists("/写真/杂/y.jpg")
        assert world.exists("/Cos/S/a.mp4") and not world.exists("/Cos/S/in")
        assert world.exists("/大文件/Cos/b.mkv")
        await world.sync()
        again = await world.tree()
        assert all(p.is_empty for p in again)


# ------------------------------------------------------------------ §4


class TestInbox:
    async def test_names_decide_the_shelf(self, world):
        for top in ["/Cosplay", "/Cos", "/写真", "/AB", "/杂"]:
            world.drive.add(f"{top}/keep.mp4", size=1)
        world.drive.add("/Telegram/Cosplay 合集 EP1.mp4", size=1)   # longest name wins
        world.drive.add("/Telegram/Cosplay 合集 EP2.mp4", size=1)
        world.drive.add("/Telegram/写真 folder/a.jpg", size=1)       # a folder moves whole
        world.drive.add("/Telegram/cabbage.mp4", size=1)             # "AB" is not a word here
        world.drive.add("/Telegram/杂七杂八.mp4", size=1)             # "杂" is too short
        world.drive.add("/Pack From Shared/nako cos 03.mp4", size=1)  # an alias
        await world.sync()
        spec = TidySpec.model_validate({"inbox": {"aliases": {"写真": ["nako"]}}})
        plan = await world.inbox(spec=spec)
        found = dict(moves(plan))
        assert found["/Telegram/Cosplay 合集 EP1.mp4"] == "/Cosplay/Cosplay 合集 EP1.mp4"
        # Arrived in /Cosplay, they are then grouped there with its loose files.
        assert (found["/Cosplay/Cosplay 合集 EP1.mp4"]
                == "/Cosplay/Cosplay 合集/Cosplay 合集 EP1.mp4")
        assert found["/Telegram/写真 folder"] == "/写真/写真 folder"
        assert found["/Pack From Shared/nako cos 03.mp4"] == "/写真/nako cos 03.mp4"
        assert found["/Telegram/cabbage.mp4"] == "/Telegram/杂/cabbage.mp4"
        assert found["/Telegram/杂七杂八.mp4"] == "/Telegram/杂/杂七杂八.mp4"

    async def test_one_inbox_only(self, world):
        world.drive.add("/Telegram/a.mp4", size=1)
        world.drive.add("/Pack From Shared/b.mp4", size=1)
        await world.sync()
        plan = await world.inbox(folders=["/Pack From Shared"])
        assert [src for src, _ in moves(plan)] == ["/Pack From Shared/b.mp4"]


# ------------------------------------------------------------------ §5


class TestBigReport:
    async def test_the_report(self, world):
        world.drive.add("/A/huge.mkv", size=90 * GB, created="2026-01-01T00:00:00+00:00")
        world.drive.add("/A/F/one.mkv", size=10 * GB)
        world.drive.add("/A/F/two.mkv", size=10 * GB)
        world.drive.add("/A/F/Sub/three.mkv", size=5 * GB)
        world.drive.add("/B/copy.mkv", size=3 * GB, hash="D")
        world.drive.add("/C/copy.mkv", size=3 * GB, hash="D")
        world.drive.add("/小千/secret.mkv", size=500 * GB)
        await world.sync()
        report = await tidy.big_report(world.ctx, at=NOW, spec=TidySpec())
        assert report.files[0].path == "/A/huge.mkv"
        assert all(not item.path.startswith("/小千") for item in report.items())
        assert [f.path for f in report.folders] == ["/A/F"]  # not also /A/F/Sub
        assert [s.path for s in report.stale] == ["/A/huge.mkv"]
        assert report.duplicates == 3 * GB
        assert report.reclaimable == 93 * GB
        lines = report.lines()
        assert lines[-1].startswith("Nothing is deleted automatically")


# ------------------------------------------------------------------ jobs


class TestJobs:
    def test_the_builtin_schedule(self):
        by_name = {job.name: job for job in Config().schedule.effective_jobs()}
        assert by_name["organize-inbox"].cron == "0 * * * *"
        assert by_name["organize-tree"].cron == "30 4 * * *"
        assert by_name["dedupe"].apply is True and not by_name["organize-tree"].apply
        config = Config.model_validate({"schedule": {"jobs": [
            {"name": "dedupe", "cron": "0 1 * * *", "enabled": False}]}})
        dedupe = next(j for j in config.schedule.effective_jobs() if j.name == "dedupe")
        assert dedupe.enabled is False
        assert Config.model_validate({"schedule": {"builtin": False}}).schedule.effective_jobs() \
            == []
        assert {job.name for job in BUILTIN_JOBS} == {
            "organize-inbox", "organize-tree", "dedupe", "big-report"}

    async def test_organize_tree_makes_one_plan_per_folder_and_retires_stale_ones(self, world):
        world.drive.add("/A/a1.mov", size=1)
        world.drive.add("/B/b1.mov", size=1)
        result = await jobs.run_job(world.ctx, ScheduledJob(name="organize-tree", cron="* * * * *"))
        assert len(result.plan_ids) == 2 and result.report is None
        first = set(result.plan_ids)
        world.drive.add("/B/b2.mov", size=1)
        result = await jobs.run_job(world.ctx, ScheduledJob(name="organize-tree", cron="* * * * *"))
        still_open = {row["id"] for row in await plans.listing(world.ctx)}
        assert still_open == set(result.plan_ids)
        assert len(first & still_open) == 1  # /A's plan did not change and was kept

    async def test_dedupe_applies_on_its_own_within_the_budget(self, world):
        for n in range(5):
            world.drive.add(f"/A/copy{n}.mkv", size=10, hash="SAME",
                            created=f"2026-01-0{n + 1}T00:00:00+00:00")
        world.ctx.config.runtime.max_actions_per_run = 3
        job = next(j for j in world.ctx.config.schedule.effective_jobs() if j.name == "dedupe")
        result = await jobs.run_job(world.ctx, job)
        assert sum(r.applied for r in result.reports) == 3
        assert result.summary == "dedupe: 3 action(s) carried out, 1 left for the next run"
        assert world.exists("/A/copy0.mkv")  # the oldest stays
        result = await jobs.run_job(world.ctx, job)  # the next run finishes the job
        assert sum(r.applied for r in result.reports) == 1

    async def test_big_report_job(self, world):
        world.drive.add("/A/x.mkv", size=9 * GB)
        result = await jobs.run_job(world.ctx, ScheduledJob(name="big-report", cron="* * * * *"))
        assert result.big.files[0].path == "/A/x.mkv"
        assert result.plan_id is None


class TestUndoAfterTidying:
    async def test_a_grouping_move_can_be_undone(self, world):
        world.drive.add("/Cos/Nako EP01.mp4", size=1)
        world.drive.add("/Cos/Nako EP02.mp4", size=1)
        await world.sync()
        (plan,) = await world.tree()
        plan_id = await plans.save(world.ctx, plan)
        report = await plans.apply(world.ctx, plan_id)
        entries = await world.store.audit_entries(limit=10, applied_only=True)
        move = next(e for e in entries if e["action"] == "move")
        outcome = await plans.undo(world.ctx, move["id"], apply_now=True)
        assert outcome.applied and report.applied == 3
        assert world.exists(move["before"]["path"])


class TestCommandLine:
    @pytest.fixture
    def drive(self, tmp_path, monkeypatch):
        from pikpak_wms.cli import main as cli

        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        config = tmp_path / "wms.yaml"
        config.write_text("ratelimit:\n  requests_per_second: 100000\n  burst: 100000\n")
        monkeypatch.setenv("WMS_CONFIG", str(config))
        monkeypatch.setenv("WMS_RULES", str(tmp_path / "none.yaml"))
        monkeypatch.setenv("WMS_LANG", "en")
        drive = FakeDrive()
        for n in range(3):
            drive.add(f"/Cos/Nako EP0{n}.mp4", size=1)
        drive.add("/Cos/F/big.mkv", size=5 * GB)
        drive.add("/Telegram/Cos extra.mp4", size=1)
        monkeypatch.setattr(cli.state, "provider_factory", lambda _config: provider_for(drive))
        return drive

    def invoke(self, *args):
        from typer.testing import CliRunner

        from pikpak_wms.cli import main as cli

        return CliRunner().invoke(cli.app, list(args))

    def test_organize_tree_samples_inbox_big_and_protect(self, drive):
        result = self.invoke("organize-tree", "--sample", "2")
        assert result.exit_code == 0, result.output
        lines = [line for line in result.output.splitlines() if "→" in line]
        assert len(lines) == 2  # two sample moves from the one tidied folder
        result = self.invoke("organize-tree", "--part", "big")
        assert "/大文件/Cos/big.mkv" in result.output and "Nako" not in result.output
        assert self.invoke("organize-tree", "--part", "nonsense").exit_code == 2
        result = self.invoke("organize-inbox")
        assert "/Telegram/Cos extra.mp4" in result.output
        result = self.invoke("big")
        assert "/Cos/F/big.mkv" in result.output
        result = self.invoke("protect", "add", "/Cos")
        assert "/Cos" in result.output
        result = self.invoke("organize-tree")
        assert "Nothing to do" in result.output  # /Cos is protected now
        assert self.invoke("protect", "add", "relative").exit_code == 2


def test_the_example_rules_file_has_a_valid_tidy_section():
    from pikpak_wms.rules.schema import load_rules

    ruleset = load_rules(Path(__file__).parent.parent / "config" / "rules.example.yaml")
    assert ruleset.tidy.big.folder == 50 * GB
    assert "最新地址" in ruleset.tidy.slim.junk_words


def test_a_file_node_snapshot_round_trips():
    node = FileNode("1", "", "a", Kind.FILE, path="/a", size=3)
    assert FileNode.from_snapshot(json.loads(json.dumps(node.snapshot()))) == node
