"""WMS M7.1 (docs/wms/M7.1-revisions.md) part A: the owner's revisions to M7,
and the two misjudgements the spot check on the real index found.

Big folders moving whole (A1) is in ``test_wms_m7.py::TestTree``.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest
from test_wms_m7 import GB, World, moves, trashed
from wms_fakes import FakeDrive, provider_for

from pikpak_wms.config import Config, ScheduledJob
from pikpak_wms.core.client import WmsClient
from pikpak_wms.core.models import ActionType
from pikpak_wms.core.ratelimit import TokenBucket
from pikpak_wms.i18n import set_language
from pikpak_wms.ops import jobs, organize, plans, tidy
from pikpak_wms.ops.context import Context
from pikpak_wms.rules.schema import TidySpec
from pikpak_wms.store.db import Store

MB = 1024**2


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language(None)


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
async def world(tmp_path):
    config = Config()
    config.rules_file = tmp_path / "no-rules.yaml"
    drive = FakeDrive()
    async with Store(tmp_path / "wms.sqlite3") as store:
        client = WmsClient(provider_for(drive), limiter=TokenBucket(1e9, 1_000_000),
                           sleep=_no_sleep)
        yield World(drive, Context(config=config, client=client, store=store))


class TestOneOtherFolder:
    """A2: everything neither video nor image goes to one /其他."""

    async def test_from_every_top_level_folder_into_one(self, world):
        world.drive.add("/A/notes.pdf", size=1, hash="aaaaaa11")
        world.drive.add("/B/notes.pdf", size=1, hash="bbbbbb22")
        world.drive.add("/B/sub.srt", size=1)
        await world.sync()
        tree = await world.tree()
        found = sorted(src_dst for plan in tree for src_dst in moves(plan))
        assert found == [
            ("/A/notes.pdf", "/其他/notes.pdf"),
            ("/B/notes_bbbbbb.pdf", "/其他/notes_bbbbbb.pdf"),  # the same name: a short hash
            ("/B/sub.srt", "/其他/sub.srt"),
        ]
        move = next(a for plan in tree for a in plan.actions
                    if a.type is ActionType.MOVE and a.before["path"] == "/A/notes.pdf")
        assert move.after["source_top"] == "/A"

    async def test_undo_puts_it_back_where_it_came_from(self, world):
        world.drive.add("/A/notes.pdf", size=1)
        await world.sync()
        (plan,) = await world.tree()
        await plans.apply(world.ctx, await plans.save(world.ctx, plan))
        assert world.exists("/其他/notes.pdf")
        entry = next(e for e in await world.store.audit_entries(limit=5, applied_only=True)
                     if e["action"] == "move")
        assert entry["after"]["source_top"] == "/A"
        await plans.undo(world.ctx, entry["id"], apply_now=True)
        assert world.exists("/A/notes.pdf") and not world.exists("/其他/notes.pdf")

    async def test_other_is_left_alone_and_made_when_missing(self, world):
        world.drive.add("/其他/old.zip", size=1)
        world.drive.add("/A/x.zip", size=1)
        await world.sync()
        (plan,) = await world.tree()
        assert plan.source == "organize-tree:/A"  # no plan for /其他 itself
        assert not [a for a in plan.actions if a.type is ActionType.CREATE_FOLDER]

    async def test_layout_ensures_it(self, world):
        # M7.1 A2: /其他 joins layout.ensure on its own.
        layout = await organize.layout(world.ctx)
        assert [a.after["path"] for a in layout.actions] == ["/其他"]
        world.drive.add("/其他", folder=True)
        await world.sync()
        assert (await organize.layout(world.ctx)).actions == []

    async def test_a_per_folder_name_is_not_ensured(self, world):
        world.ctx.config.rules_file.write_text("tidy:\n  loose:\n    other: 其他\n",
                                               encoding="utf-8")
        assert (await organize.layout(world.ctx)).actions == []

    async def test_the_plan_makes_it_when_missing(self, world):
        world.drive.add("/A/x.zip", size=1)
        await world.sync()
        (plan,) = await world.tree()
        made = [a.after["path"] for a in plan.actions if a.type is ActionType.CREATE_FOLDER]
        assert made == ["/其他"]

    def test_a_bare_name_keeps_one_per_folder(self):
        spec = TidySpec.model_validate({"loose": {"other": "其他"}})
        assert spec.loose.other == "其他"
        with pytest.raises(ValueError):
            TidySpec.model_validate({"loose": {"other": "/"}})


class TestNoNestedTwin:
    """A4.1: no /<A>/<almost A>/ folder."""

    @pytest.mark.parametrize(("group", "top", "nested"), [
        ("Nako", "Nako", True),
        ("Nako Cos", "Nako", True),
        ("Nako", "Cosplaytales Nako", True),
        ("小千", "小千合集", True),
        ("Yuki", "Nako", False),
        ("旅行", "Vlog", False),
    ])
    def test_names(self, group, top, nested):
        assert tidy.nests(group, top) is nested

    async def test_such_a_group_stays_put(self, world):
        for name in ["Nako EP01.mp4", "Nako EP02.mp4", "Yuki 01.mp4", "Yuki 02.mp4"]:
            world.drive.add(f"/Nako合集/{name}", size=1)
        await world.sync()
        (plan,) = await world.tree()
        assert sorted(moves(plan)) == [("/Nako合集/Yuki 01.mp4", "/Nako合集/Yuki/Yuki 01.mp4"),
                                       ("/Nako合集/Yuki 02.mp4", "/Nako合集/Yuki/Yuki 02.mp4")]
        assert plan.notes[0]["args"]["stayed"] == 2

    async def test_no_grouping_move_ever_nests(self, world):
        tops = ["Nako", "小千合集", "Cos", "Vlog 2024"]
        names = ["Nako EP0{}.mp4", "小千 第{}集.mp4", "Cos 花絮 {}.mp4", "Vlog {}.mp4",
                 "Rin {}.mp4"]
        for top in tops:
            for pattern in names:
                for n in (1, 2):
                    world.drive.add(f"/{top}/{pattern.format(n)}", size=1)
        await world.sync()
        for plan in await world.tree():
            for action in plan.actions:
                if action.rule_name == "tidy:group" and action.type is ActionType.MOVE:
                    top = action.after["path"].split("/")[1]
                    sub = action.after["path"].split("/")[2]
                    assert not tidy.nests(sub, top), (top, sub)


class TestAds:
    """A4.2: ad-like files go to a plan of their own, never applied by a job."""

    FILES: ClassVar[dict[str, tuple[int, bool]]] = {
        "扫码关注二维码.jpg": (100_000, True),          # an image, and 二维码
        "QR.png": (5_000, True),
        "论坛发布器.rar": (2 * MB, True),               # an archive, and 发布器
        "tuu88.com 精彩.mp4": (8 * MB, True),           # a small video with a domain
        "setup_www.xx.vip.exe": (1 * MB, True),         # an executable with a domain
        "2048.vip-某某.mp4": (900 * MB, False),         # a big video: only a site prefix
        "holiday.jpg": (100_000, False),                # an image, nothing ad-like
        "tuu88.com 正片.mkv": (2 * GB, False),          # a domain, but big
    }

    def test_detection(self):
        spec = TidySpec()
        from pikpak_wms.core.models import FileNode, Kind

        for name, (size, expected) in self.FILES.items():
            node = FileNode("x", "", name, Kind.FILE, path=f"/A/{name}", size=size)
            assert tidy.is_ad(node, spec) is expected, name

    async def test_a_plan_of_their_own(self, world):
        for name, (size, _expected) in self.FILES.items():
            world.drive.add(f"/A/{name}", size=size)
        await world.sync()
        planned = await world.tree()
        main = next(p for p in planned if p.source == "organize-tree:/A")
        ads = next(p for p in planned if p.source == "organize-tree-ads:/A")
        assert sorted(trashed(ads)) == sorted(f"/A/{n}" for n, (_s, ad) in self.FILES.items()
                                              if ad)
        assert all(a.rule_name == "tidy:ad" for a in ads.actions)
        assert not [a for a in main.actions if a.before.get("path") in trashed(ads)]
        # The big video with a site prefix is grouped or sorted like any other.
        assert any(src == "/A/2048.vip-某某.mp4" for src, _ in moves(main))
        assert ads.notes[-1]["key"] == "tidy.ads"

    async def test_a_job_never_applies_the_ads_plan(self, world):
        world.drive.add("/A/QR.png", size=5_000)
        world.drive.add("/A/film.mkv", size=10)
        job = ScheduledJob(name="organize-tree", cron="* * * * *", apply=True)
        result = await jobs.run_job(world.ctx, job)
        assert world.exists("/A/QR.png")                # waiting for a person
        assert world.exists("/A/杂/film.mkv")            # the rest was applied
        sources = {row["source"]: row["status"] for row in await plans.listing(world.ctx)}
        assert sources == {"organize-tree-ads:/A": "pending"}
        assert len(result.plan_ids) == 2

    async def test_the_inbox_screens_ads_before_shelving(self, world):
        world.drive.add("/Cos/keep.mp4", size=1)
        world.drive.add("/Telegram/Cos 最新地址.jpg", size=1_000)
        world.drive.add("/Telegram/Cos 正片.mp4", size=1)
        await world.sync()
        planned = await world.inbox()
        assert [p.source for p in planned] == ["organize-inbox", "organize-inbox-ads"]
        assert trashed(planned[1]) == ["/Telegram/Cos 最新地址.jpg"]
        assert ("/Telegram/Cos 正片.mp4", "/Cos/Cos 正片.mp4") in moves(planned[0])

    def test_the_word_list_lives_in_the_rules_file(self):
        spec = TidySpec.model_validate({"ads": {"words": ["广告"], "video_max": "10MiB"}})
        assert spec.ads.video_max == 10 * MB and spec.ads.words == ["广告"]
        off = TidySpec.model_validate({"ads": {"enabled": False}})
        from pikpak_wms.core.models import FileNode, Kind

        node = FileNode("x", "", "QR.png", Kind.FILE, path="/A/QR.png", size=1)
        assert not tidy.is_ad(node, off)


class TestSampleJson:
    """A4.3: machine-readable spot checks that no line wrap can break."""

    @pytest.fixture
    def cli_drive(self, tmp_path, monkeypatch):
        from pikpak_wms.cli import main as cli

        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        config = tmp_path / "wms.yaml"
        config.write_text("ratelimit:\n  requests_per_second: 100000\n  burst: 100000\n")
        monkeypatch.setenv("WMS_CONFIG", str(config))
        monkeypatch.setenv("WMS_RULES", str(tmp_path / "none.yaml"))
        monkeypatch.setenv("WMS_LANG", "en")
        drive = FakeDrive()
        long = "某某某某某某某某某某某某某某某某某某某某某某某某某某某某某某 very long name"
        for n in range(3):
            drive.add(f"/Cos/{long} EP0{n}.mp4", size=1)
        drive.add("/Cos/QR.png", size=10)
        monkeypatch.setattr(cli.state, "provider_factory", lambda _c: provider_for(drive))
        return drive

    def test_json_sample(self, cli_drive):
        from typer.testing import CliRunner

        from pikpak_wms.cli import main as cli

        result = CliRunner().invoke(cli.app, ["organize-tree", "--sample", "20", "--json"])
        assert result.exit_code == 0, result.output
        picked = json.loads(result.output)
        assert {item["plan"] for item in picked} == {"organize-tree:/Cos",
                                                     "organize-tree-ads:/Cos"}
        assert all(set(item) == {"plan", "action", "rule", "from", "to"} for item in picked)
        ad = next(item for item in picked if item["rule"] == "tidy:ad")
        assert ad["action"] == "trash" and ad["to"] == ""
        # A spot check stores nothing.
        listed = CliRunner().invoke(cli.app, ["plans"])
        assert "organize-tree" not in listed.output

    def test_text_sample_is_one_line_each(self, cli_drive):
        from typer.testing import CliRunner

        from pikpak_wms.cli import main as cli

        result = CliRunner().invoke(cli.app, ["organize-tree", "--sample", "20"])
        lines = [line for line in result.output.splitlines() if line]
        assert lines and all("→" in line for line in lines)
