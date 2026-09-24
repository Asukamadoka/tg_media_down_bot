"""WMS M2, the rules layer: units, templates, the rules file, matchers, planning.

Nothing here applies anything; see test_wms_m2.py for apply, audit and undo.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from wms_fakes import FakeDrive, provider_for

from pikpak_wms.core.client import WmsClient
from pikpak_wms.core.models import ActionType, FileNode, Kind, Plan
from pikpak_wms.core.ratelimit import TokenBucket
from pikpak_wms.i18n import set_language
from pikpak_wms.ops.stocktake import stocktake
from pikpak_wms.rules import template
from pikpak_wms.rules.engine import evaluate
from pikpak_wms.rules.matcher import Matcher, glob_to_regex
from pikpak_wms.rules.schema import Match, RulesError, load_rules, parse_rules
from pikpak_wms.rules.units import parse_duration, parse_moment, parse_size
from pikpak_wms.store.db import Store

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language(None)


# ------------------------------------------------------------------- units


class TestUnits:
    @pytest.mark.parametrize(
        ("text", "size"),
        [("100", 100), ("1KB", 1024), ("100MB", 100 * 1024**2), ("1.5 GiB", 3 * 1024**3 // 2),
         ("2g", 2 * 1024**3), (4096, 4096)],
    )
    def test_sizes_are_binary(self, text, size):
        assert parse_size(text) == size

    @pytest.mark.parametrize("bad", ["", "big", "10 PB", "-1MB", True])
    def test_nonsense_sizes_are_refused(self, bad):
        with pytest.raises(ValueError):
            parse_size(bad)

    def test_durations(self):
        assert parse_duration("30d") == timedelta(days=30)
        assert parse_duration("2w") == timedelta(weeks=2)
        assert parse_duration("12h") == timedelta(hours=12)
        assert parse_duration(3) == timedelta(days=3)

    def test_a_bare_date_is_midnight_in_the_configured_zone(self):
        moment = parse_moment("2026-09-24", now=NOW, tz=SHANGHAI)
        assert moment == datetime(2026, 9, 23, 16, 0, tzinfo=UTC)
        assert parse_moment("7d", now=NOW, tz=SHANGHAI) == NOW - timedelta(days=7)


# ---------------------------------------------------------------- template


class TestTemplate:
    def test_fields_and_filters(self):
        values = {"show": "the.walking.dead", "s": "1", "ext": "mkv"}
        assert (
            template.render("{show|spaces|title}.S{s|pad2}.{ext}", values)
            == "The Walking Dead.S01.mkv"
        )

    def test_date_uses_the_given_zone(self):
        created = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)  # 02:00 on 1 September in Shanghai
        assert template.render("/Archive/{created|date:%Y-%m}", {"created": created},
                               tz=SHANGHAI) == "/Archive/2026-09"

    def test_escaped_braces_are_literal(self):
        assert template.render("{{x}} {name}", {"name": "a"}) == "{x} a"

    def test_a_missing_field_is_an_error_not_an_empty_string(self):
        with pytest.raises(template.TemplateError, match="show"):
            template.render("{show}", {})

    def test_unknown_filters_fail_when_the_rules_load(self):
        with pytest.raises(template.TemplateError):
            template.check("{name|shout}")
        with pytest.raises(template.TemplateError):
            template.check("{name")


# ------------------------------------------------------------------ schema


def rules(yaml_rules: list[dict]):
    return parse_rules({"version": 1, "rules": yaml_rules})


class TestRulesFile:
    def test_the_shipped_example_is_valid(self):
        ruleset = load_rules(ROOT / "config" / "rules.example.yaml")
        names = [rule.name for rule in ruleset.rules]
        assert "按月归档" in names and "按类型分类-视频" in names
        assert {rule.stage for rule in ruleset.rules} == {"organize", "cleanup"}

    def test_a_misspelt_matcher_is_refused_not_ignored(self):
        # Ignoring it would silently widen the rule to everything in scope.
        with pytest.raises(RulesError, match="min_sise"):
            rules([{"name": "x", "match": {"min_sise": "1GB"}, "actions": [{"trash": {}}]}])

    def test_permanent_deletion_cannot_come_from_a_rules_file(self):
        with pytest.raises(RulesError, match="delete_forever"):
            rules([{"name": "x", "actions": [{"delete_forever": {}}]}])

    def test_rule_names_are_unique(self):
        with pytest.raises(RulesError, match="unique"):
            rules([{"name": "x", "actions": ["trash"]}, {"name": "x", "actions": ["trash"]}])

    @pytest.mark.parametrize(
        "bad",
        [
            {"name_regex": "(unclosed"},
            {"older_than": "a while"},
            {"category": "movies"},
            {"kind": "link"},
        ],
    )
    def test_bad_matchers(self, bad):
        with pytest.raises(RulesError):
            rules([{"name": "x", "match": bad, "actions": ["trash"]}])

    def test_bad_actions(self):
        with pytest.raises(RulesError, match="absolute"):
            rules([{"name": "x", "actions": [{"move": {"to": "relative"}}]}])
        with pytest.raises(RulesError, match="name, not a path"):
            rules([{"name": "x", "actions": [{"rename": {"template": "a/{name}"}}]}])
        with pytest.raises(RulesError, match="at least 1"):
            rules([{"name": "x", "actions": []}])

    def test_a_missing_file_says_what_to_copy(self, tmp_path):
        with pytest.raises(RulesError, match=r"rules\.example\.yaml"):
            load_rules(tmp_path / "rules.yaml")


# ----------------------------------------------------------------- matcher


def file(path, *, size=10, mime="", created="2026-09-01T00:00:00+00:00", kind=Kind.FILE,
         file_id=None):
    return FileNode(file_id=file_id or path, parent_id="p", name=path.rsplit("/", 1)[-1],
                    kind=kind, path=path, size=size, mime=mime, created_time=created)


def matches(match: dict, node: FileNode, **kw) -> dict | None:
    return Matcher(Match.model_validate(match)).test(node, now=NOW, tz=SHANGHAI, **kw)


class TestMatcher:
    def test_globs(self):
        assert glob_to_regex("/Inbox/*.mkv").match("/Inbox/a.mkv")
        assert not glob_to_regex("/Inbox/*.mkv").match("/Inbox/sub/a.mkv")
        assert glob_to_regex("/Inbox/**/*.mkv").match("/Inbox/sub/deeper/a.mkv")
        assert glob_to_regex("/Inbox/**/*.mkv").match("/Inbox/a.mkv")
        assert glob_to_regex("*.srt").match("/any/where/x.srt")

    def test_every_matcher_must_hold(self):
        node = file("/Inbox/Show.S01E02.mkv", size=200 * 1024**2, mime="video/x-matroska")
        assert matches({"kind": "file", "min_size": "100MB", "mime": "video/*",
                        "path_glob": "/Inbox/*"}, node) == {}
        assert matches({"kind": "file", "max_size": "100MB"}, node) is None

    def test_named_groups_come_back_for_templates(self):
        node = file("/Inbox/Show.S01E02.mkv")
        found = matches({"name_regex": r"(?P<show>.+?)\.S(?P<s>\d+)E(?P<e>\d+)"}, node)
        assert found == {"show": "Show", "s": "01", "e": "02"}

    def test_category_uses_mime_or_extension(self):
        assert matches({"category": "video"}, file("/a.MKV")) == {}
        assert matches({"category": "video"}, file("/a.bin", mime="video/mp4")) == {}
        assert matches({"category": ["image", "audio"]}, file("/a.mkv")) is None
        assert matches({"extensions": [".mkv"]}, file("/a.mkv")) == {}

    def test_age_is_measured_on_the_created_time(self):
        old = file("/a", created="2026-08-01T00:00:00+00:00")
        new = file("/b", created="2026-09-24T01:00:00+00:00")
        assert matches({"older_than": "30d"}, old) == {}
        assert matches({"older_than": "30d"}, new) is None
        # "today" in Shanghai started at 16:00 UTC the day before.
        assert matches({"newer_than": "2026-09-24"}, new) == {}
        assert matches({"newer_than": "2026-09-24"}, old) is None
        assert matches({"older_than": "1d"}, file("/c", created=None)) is None

    def test_empty_folders(self):
        folder = file("/Media/x", kind=Kind.FOLDER, file_id="f1")
        assert matches({"kind": "folder", "empty": True}, folder, nonempty={"other"}) == {}
        assert matches({"kind": "folder", "empty": True}, folder, nonempty={"f1"}) is None


# ------------------------------------------------------------------ engine


@pytest.fixture
async def indexed(tmp_path):
    drive = FakeDrive()
    async with Store(tmp_path / "wms.sqlite3") as store:
        client = WmsClient(provider_for(drive), limiter=TokenBucket(1e9, 1_000_000))
        yield drive, client, store


async def plan_for(drive, client, store, yaml_rules) -> Plan:
    await stocktake(client, store)
    ruleset = rules(yaml_rules)
    return await evaluate(ruleset.rules, store, now=NOW, tz=SHANGHAI, source="test")


SHOWS = {
    "name": "shows",
    "scope": "/Inbox",
    "match": {"kind": "file", "name_regex": r"(?P<show>.+?)\.S(?P<s>\d{2})E(?P<e>\d{2})"},
    "actions": [
        {"rename": {"template": "{show|title}.S{s}E{e}.{ext}"}},
        {"move": {"to": "/Media/{show|title}/S{s}"}},
    ],
}


class TestPlanning:
    async def test_planning_touches_nothing(self, indexed):
        drive, client, store = indexed
        drive.add("/Inbox/lost.S01E01.mkv")
        drive.add("/Inbox/lost.S01E02.mkv")
        await stocktake(client, store)
        calls = len(drive.calls)
        plan = await evaluate(rules([SHOWS]).rules, store, now=NOW, tz=SHANGHAI, source="t")
        assert len(drive.calls) == calls
        types = [a.type for a in plan.actions]
        # Step by step: both renames, then the folder, then both moves together.
        assert types == [ActionType.RENAME, ActionType.RENAME, ActionType.CREATE_FOLDER,
                         ActionType.MOVE, ActionType.MOVE]
        assert plan.actions[2].after["path"] == "/Media/Lost/S01"
        assert plan.actions[3].before["path"] == "/Inbox/Lost.S01E01.mkv"
        assert plan.actions[3].after["path"] == "/Media/Lost/S01/Lost.S01E01.mkv"

    async def test_two_files_wanting_one_name_keep_the_second_where_it_is(self, indexed):
        drive, client, store = indexed
        drive.add("/Inbox/a/x.S01E01.mkv")
        drive.add("/Inbox/b/X.S01E01.mkv")
        plan = await plan_for(drive, client, store, [{
            "name": "flat", "scope": "/Inbox", "match": {"kind": "file"},
            "actions": [{"move": {"to": "/Media"}}],
        }])
        moves = [a for a in plan.actions if a.type is ActionType.MOVE]
        assert len(moves) == 2  # different names: both fine
        drive.add("/Inbox/c/x.S01E01.mkv")
        plan = await plan_for(drive, client, store, [{
            "name": "flat", "scope": "/Inbox", "match": {"kind": "file"},
            "actions": [{"move": {"to": "/Media"}}],
        }])
        moves = [a for a in plan.actions if a.type is ActionType.MOVE]
        assert len(moves) == 2
        assert any("is taken" in line for line in plan.note_lines())

    async def test_the_first_rule_that_matches_wins(self, indexed):
        drive, client, store = indexed
        drive.add("/Inbox/lost.S01E01.mkv")
        plan = await plan_for(drive, client, store, [
            SHOWS,
            {"name": "videos", "scope": "/Inbox", "match": {"category": "video"},
             "actions": [{"move": {"to": "/Media/Video"}}]},
        ])
        assert {a.rule_name for a in plan.actions} == {"shows"}

    async def test_a_matched_folder_carries_its_contents(self, indexed):
        drive, client, store = indexed
        drive.add("/Temp/old/a.bin")
        drive.add("/Temp/old/b.bin")
        plan = await plan_for(drive, client, store, [
            {"name": "clear", "scope": "/Temp", "recursive": True, "actions": ["trash"]},
        ])
        assert [a.before["path"] for a in plan.actions] == ["/Temp/old"]

    async def test_a_second_plan_after_the_moves_is_empty(self, indexed):
        drive, client, store = indexed
        drive.add("/Media/Lost/S01/Lost.S01E01.mkv")
        plan = await plan_for(drive, client, store, [{**SHOWS, "scope": "/Media"}])
        assert plan.is_empty

    async def test_a_template_field_that_is_missing_is_noted(self, indexed):
        drive, client, store = indexed
        drive.add("/Inbox/a.mkv")
        plan = await plan_for(drive, client, store, [{
            "name": "odd", "scope": "/Inbox", "actions": [{"move": {"to": "/M/{show}"}}],
        }])
        assert plan.is_empty
        assert "no value for {show}" in plan.note_lines()[0]

    async def test_archive_by_month_in_the_configured_zone(self, indexed):
        drive, client, store = indexed
        drive.add("/Media/v.mkv", created="2026-05-31T17:00:00+00:00")  # 1 June, Shanghai
        plan = await plan_for(drive, client, store, [{
            "name": "archive", "scope": "/Media", "match": {"kind": "file", "older_than": "90d"},
            "actions": [{"move": {"to": "/Archive/{created|date:%Y-%m}"}}],
        }])
        assert plan.actions[-1].after["path"] == "/Archive/2026-06/v.mkv"

    async def test_notes_are_stored_as_keys_and_translated_when_shown(self, indexed):
        drive, client, store = indexed
        drive.add("/Inbox/a.mkv")
        plan = await plan_for(drive, client, store, [{
            "name": "odd", "scope": "/Inbox", "actions": [{"move": {"to": "/M/{show}"}}],
        }])
        stored = Plan.from_dict(plan.to_dict())
        assert stored.notes[0]["key"] == "conflict.template"
        set_language("zh")
        assert stored.note_lines()[0].startswith("规则「odd」")
