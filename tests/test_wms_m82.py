"""WMS M8.2 (docs/wms/M8.2-nl-normalize-and-safety.md) and M8.1 §C.

What the Mac evaluation found, made into tests: dates written as words, sizes
of 0, destinations on intents that have none, and above all a time condition
pointing the wrong way (「超过 7 天」 answered as "within 7 days").
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pikpak_wms.config import Config
from pikpak_wms.core.models import render_note
from pikpak_wms.i18n import set_language, t
from pikpak_wms.nl import eval as nl_eval
from pikpak_wms.nl.compile import explain
from pikpak_wms.nl.guard import CORRECTED, direction_of, guard
from pikpak_wms.nl.query import (
    Clarification,
    Filters,
    Query,
    from_wire,
    normalize_wire,
    period_start,
)
from pikpak_wms.nl.rules_parser import RulesTranslator
from pikpak_wms.nl.translator import SYSTEM_PROMPT, Chain

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=TZ)  # a Thursday
TODAY = "2026-09-24T00:00:00+08:00"
TOMORROW = "2026-09-25T00:00:00+08:00"
MONTH = "2026-09-01T00:00:00+08:00"
NEXT_MONTH = "2026-10-01T00:00:00+08:00"
JAN_1 = "2026-01-01T00:00:00+08:00"


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language(None)


def wire(**overrides):
    answer = {"intent": "list", "scope": {"path": "/", "recursive": True},
              "filters": {"created_after": None, "created_before": None, "min_size": None,
                          "max_size": None, "kinds": [], "extensions": [],
                          "name_contains": [], "name_regex": None},
              "action_args": {"dest": None, "template": None, "part": None},
              "schedule": None, "needs_clarification": None}
    answer.update(overrides)
    return answer


def filters(**kw):
    base = wire()["filters"]
    base.update(kw)
    return base


def normalized(**kw):
    return normalize_wire(wire(**kw), now=NOW, tz=TZ)


class TestPeriodWords:
    """M8.1 §C and M8.2 §B3: a word where a time belongs."""

    @pytest.mark.parametrize(("word", "expected"), [
        ("today", "2026-09-24T00:00:00+08:00"),
        ("今天", "2026-09-24T00:00:00+08:00"),
        ("yesterday", "2026-09-23T00:00:00+08:00"),
        ("昨天", "2026-09-23T00:00:00+08:00"),
        ("本周", "2026-09-21T00:00:00+08:00"),
        ("this week", "2026-09-21T00:00:00+08:00"),
        ("上周", "2026-09-14T00:00:00+08:00"),
        ("本月", "2026-09-01T00:00:00+08:00"),
        ("上个月", "2026-08-01T00:00:00+08:00"),
        ("last month", "2026-08-01T00:00:00+08:00"),
        ("今年", "2026-01-01T00:00:00+08:00"),
        ("去年", "2025-01-01T00:00:00+08:00"),
    ])
    def test_in_shanghai_time(self, word, expected):
        assert period_start(word, NOW, TZ).isoformat(timespec="seconds") == expected
        for field in ("created_after", "created_before"):
            got = normalized(filters=filters(**{field: word}))["filters"][field]
            assert got == expected

    def test_january_and_the_turn_of_the_year(self):
        now = datetime(2027, 1, 5, 9, 0, tzinfo=TZ)
        assert period_start("上个月", now, TZ).isoformat() == "2026-12-01T00:00:00+08:00"
        assert period_start("去年", now, TZ).isoformat() == "2026-01-01T00:00:00+08:00"
        assert period_start("本周", now, TZ).isoformat() == "2027-01-04T00:00:00+08:00"

    def test_a_model_that_wrote_today_now_validates(self):
        query = from_wire(normalized(filters=filters(created_after="today")))
        assert query.filters.created_after == "2026-09-24T00:00:00+08:00"

    def test_durations_and_real_dates_are_left_alone(self):
        got = normalized(filters=filters(created_after="7d", created_before=JAN_1))
        assert got["filters"]["created_after"] == "7d"
        assert got["filters"]["created_before"] == JAN_1


class TestTheFiveNormalizations:
    def test_1_a_size_of_zero_means_no_limit(self):
        got = normalized(filters=filters(min_size=0, max_size=0))["filters"]
        assert got["min_size"] is None and got["max_size"] is None
        assert normalized(filters=filters(max_size=5))["filters"]["max_size"] == 5

    def test_2_a_date_alone_is_that_day_at_midnight_in_shanghai(self):
        got = normalized(filters=filters(created_after="2026-09-24"))["filters"]
        assert got["created_after"] == "2026-09-24T00:00:00+08:00"
        naive = normalized(filters=filters(created_before="2025-12-31T23:59:59"))["filters"]
        assert naive["created_before"] == "2025-12-31T23:59:59+08:00"

    def test_3_months_and_years_are_days(self):
        # "m" would be read as minutes; the prompt says a month is 30d.
        got = normalized(filters=filters(created_after="1m", created_before="2y"))["filters"]
        assert (got["created_after"], got["created_before"]) == ("30d", "730d")

    @pytest.mark.parametrize("intent", ["trash", "list", "dedupe", "organize_tree",
                                        "organize_inbox", "big_report"])
    def test_4_no_destination_where_there_is_none(self, intent):
        args = {"dest": "/Recycle Bin", "template": None, "part": None}
        assert normalized(intent=intent, action_args=args)["action_args"]["dest"] is None

    @pytest.mark.parametrize("intent", ["move", "archive", "download"])
    def test_4_but_kept_where_it_means_something(self, intent):
        args = {"dest": "/Media", "template": None, "part": None}
        assert normalized(intent=intent, action_args=args)["action_args"]["dest"] == "/Media"

    def test_5_extensions_the_kinds_already_cover_are_dropped(self):
        got = normalized(filters=filters(kinds=["video"], extensions=["mkv", "mp4"]))["filters"]
        assert got["kinds"] == ["video"] and got["extensions"] == []
        subs = normalized(filters=filters(kinds=["subtitle"], extensions=["srt"]))["filters"]
        assert subs["extensions"] == []   # ass, ssa, vtt stay covered by the kind

    def test_5_but_an_extension_outside_the_kinds_is_kept(self):
        got = normalized(filters=filters(kinds=["video"], extensions=["mkv", "srt"]))["filters"]
        assert got["extensions"] == ["mkv", "srt"]
        alone = normalized(filters=filters(extensions=["mkv"]))["filters"]
        assert alone["extensions"] == ["mkv"]


class TestTheDirectionOfTime:
    @pytest.mark.parametrize(("sentence", "direction"), [
        ("删除 /Temp 里超过 7 天的文件", "older"),
        ("超过半年的文件归档", "older"),
        ("把2026年1月1日之前的文件归档", "older"),
        ("三个月前的视频", "older"),
        ("60天前的文件", "older"),
        ("早于 2026-01-01 的文件", "older"),
        ("older than 30 days", "older"),
        ("今天之前的文件", "older"),
        ("列出最近 7 天转存的视频", "newer"),
        ("7天内的文件", "newer"),
        ("2026年9月1日以来的文件", "newer"),
        ("统计一下本月入库的视频", "newer"),
        ("今天转存的", "newer"),
        ("超过 2GB 的文件", None),          # a size, not an age
        ("列出所有视频", None),
        ("最近一周之前的文件", None),        # both: not ours to call
    ])
    def test_reading_the_words(self, sentence, direction):
        assert direction_of(sentence) == direction

    def query(self, **kw) -> Query:
        return Query(intent="trash", scope={"path": "/Temp"}, filters=Filters(**kw))

    def test_older_than_seven_days_was_written_as_within_seven_days(self):
        # The dangerous one: it would have trashed the newest week.
        result = guard("删除 /Temp 里超过 7 天的文件", self.query(created_after="7d"))
        assert result.filters.created_before == "7d" and result.filters.created_after is None
        assert result.corrections == [CORRECTED]

    def test_within_seven_days_was_written_as_older_than(self):
        result = guard("列出最近 7 天转存的文件", Query(intent="list",
                                                   filters=Filters(created_before="7d")))
        assert result.filters.created_after == "7d" and result.filters.created_before is None
        assert result.corrections == [CORRECTED]

    def test_before_a_date(self):
        result = guard("把2026年1月1日之前的文件归档",
                       Query(intent="archive", filters=Filters(
                           created_after="2026-01-01T00:00:00+08:00")))
        assert result.filters.created_before == "2026-01-01T00:00:00+08:00"
        assert result.filters.created_after is None

    def test_a_right_answer_is_left_alone(self):
        right = self.query(created_before="7d")
        assert guard("删除 /Temp 里超过 7 天的文件", right).corrections == []

    def test_no_opinion_no_change(self):
        q = self.query(created_after="7d")
        assert guard("整理一下", q).filters.created_after == "7d"

    def test_a_range_is_not_swapped(self):
        q = self.query(created_after="2026-01-01T00:00:00+08:00",
                       created_before="2026-06-01T00:00:00+08:00")
        assert guard("2026年1月1日之前的", q).corrections == []

    def test_the_same_duration_both_ways_is_a_question(self):
        q = self.query(created_after="7d", created_before="7d")
        result = guard("删除 /Temp 里超过 7 天的文件", q)
        assert isinstance(result, Clarification) and result.question == "nl.ask.time_direction"

    def test_a_question_passes_through(self):
        ask = Clarification(question="x")
        assert guard("anything", ask) is ask
        assert guard("anything", None) is None

    async def test_the_chain_guards_a_model_and_trusts_the_rules(self):
        class Model:
            name = "openai"

            async def translate(self, text, now, tz):
                return Query(intent="trash", scope={"path": "/Temp"},
                             filters=Filters(created_after="7d"))

        chain = Chain([Model()])
        result = await chain.translate("删除 /Temp 里超过 7 天的文件", NOW, TZ)
        assert result.filters.created_before == "7d"
        # The rules parser reads the same words itself and needs no second opinion.
        rules = await Chain([RulesTranslator()]).translate(
            "删除 /Temp 里超过 7 天的文件", NOW, TZ)
        assert rules.filters.created_before == "7d" and rules.corrections == []

    def test_the_correction_is_shown_in_the_plan(self):
        result = guard("删除 /Temp 里超过 7 天的文件", self.query(created_after="7d"))
        lines = [render_note(n) for n in explain(result, Config(), now=NOW, tz=TZ)]
        assert "Time direction put right to match your wording" in lines


class TestATrashSaysItInPlainWords:
    """M8.2 §C2: the direction is checked at a glance, before anything is confirmed."""

    def first(self, **kw) -> str:
        query = Query(intent="trash", scope={"path": "/Temp"}, filters=Filters(**kw))
        return render_note(explain(query, Config(), now=NOW, tz=TZ)[0])

    def test_older_than(self):
        assert self.first(created_before="7d") == (
            "Created 7 days ago or longer (earlier than 09-17 12:00)")

    def test_within(self):
        assert self.first(created_after="7d") == (
            "Created within the last 7 days (since 09-17 12:00)")

    def test_a_date(self):
        assert self.first(created_before="2026-01-01T00:00:00+08:00") == (
            "Created before 01-01 00:00")

    def test_a_range(self):
        assert "between" in self.first(created_after="2026-01-01T00:00:00+08:00",
                                       created_before="2026-06-01T00:00:00+08:00")

    def test_in_chinese(self):
        set_language("zh")
        assert self.first(created_before="7d").startswith("创建于 7 天以前")
        assert self.first(created_after="7d").startswith("最近 7 天内创建")
        assert "09-17 12:00" in self.first(created_before="7d")

    def test_only_a_trash_gets_it(self):
        query = Query(intent="list", filters=Filters(created_before="7d"))
        assert render_note(explain(query, Config(), now=NOW, tz=TZ)[0]).startswith("Understood as")


class TestTheRulesAnswerFirst:
    """M8.2 §D: not instructions, or ones never carried out: never a model's call."""

    def parse(self, sentence):
        return RulesTranslator().parse(sentence, NOW, TZ)

    @pytest.mark.parametrize("sentence", ["你好", "您好!", "hello", "谢谢", "今天天气怎么样",
                                          "讲个笑话", "你是谁"])
    def test_greetings_and_small_talk(self, sentence):
        result = self.parse(sentence)
        assert isinstance(result, Clarification) and result.question == "nl.ask.chat"

    @pytest.mark.parametrize("sentence", ["网盘还剩多少空间", "还剩多少空间", "剩余空间多少",
                                          "我的网盘用了多少空间", "查一下配额"])
    def test_space_and_quota(self, sentence):
        result = self.parse(sentence)
        assert isinstance(result, Clarification) and result.question == "nl.ask.quota"

    @pytest.mark.parametrize("sentence", ["清空回收站", "帮我清空一下回收站", "倒空垃圾桶"])
    def test_emptying_the_trash_is_left_to_a_person(self, sentence):
        result = self.parse(sentence)
        assert isinstance(result, Clarification) and result.question == "nl.ask.empty_trash"
        assert "empty the recycle bin in PikPak yourself" in t(result.question)

    def test_permanent_deletion_keeps_its_own_answer(self):
        assert self.parse("永久删除 /Temp").question == "nl.ask.forever"

    def test_a_question_about_size_is_not_a_question_about_space(self):
        result = self.parse("看看哪些文件最占空间")
        assert isinstance(result, Query) and result.intent == "big_report"

    @pytest.mark.parametrize(("sentence", "intent", "path", "part"), [
        ("整理一下 Telegram", "organize_inbox", "/Telegram", None),
        ("Pack From Shared 整理一下", "organize_inbox", "/Pack From Shared", None),
        ("把大文件单独放一起", "organize_tree", "/", "big"),
        ("把大目录集中放", "organize_tree", "/", "big"),
    ])
    async def test_these_never_reach_a_model(self, sentence, intent, path, part):
        class Boom:
            name = "openai"

            async def translate(self, *_args):
                raise AssertionError("a model was asked")

        result = await Chain([RulesTranslator(), Boom()]).translate(sentence, NOW, TZ)
        assert (result.intent, result.scope.path, result.action_args.part) == (
            intent, path, part)

    async def test_small_talk_is_not_handed_on_either(self):
        class Boom:
            name = "openai"

            async def translate(self, *_args):
                raise AssertionError("a model was asked")

        result = await Chain([RulesTranslator(), Boom()]).translate("你好", NOW, TZ)
        assert isinstance(result, Clarification)

    def test_the_prompt_teaches_both_directions_and_the_rest(self):
        assert '"created_before": "7d"' in SYSTEM_PROMPT
        assert '"created_after": "7d"' in SYSTEM_PROMPT
        assert "上个月入库的文件" in SYSTEM_PROMPT and '"name_regex": "^sample"' in SYSTEM_PROMPT


# ---------------------------------------------------------------- the eval


class TestTemplates:
    def test_a_day_gets_its_dates(self):
        values = nl_eval.template_values(NOW, TZ)
        assert values["today"] == "2026-09-24T00:00:00+08:00"
        assert values["week_start"] == "2026-09-21T00:00:00+08:00"
        assert values["last_month_start"] == "2026-08-01T00:00:00+08:00"
        assert (values["year"], values["month"], values["mm"]) == ("2026", "9", "09")

    def test_expanding_reaches_into_lists_and_dicts_and_leaves_the_unknown(self):
        values = nl_eval.template_values(NOW, TZ)
        got = nl_eval.expand({"a": ["{today}", "{nothing}"], "b": {"c": "x{yesterday}"}}, values)
        assert got == {"a": ["2026-09-24T00:00:00+08:00", "{nothing}"],
                       "b": {"c": "x2026-09-23T00:00:00+08:00"}}

    def test_the_cases_follow_now(self):
        first, *_ = nl_eval.load_cases(nl_eval.DEFAULT_CASES, now=NOW)[0]
        moved = nl_eval.load_cases(nl_eval.DEFAULT_CASES,
                                   now=datetime(2027, 3, 10, 9, 0, tzinfo=TZ))[0]
        assert first["expect"]["filters"]["created_after"] == "2026-09-24T00:00:00+08:00"
        assert moved[0]["expect"]["filters"]["created_after"] == "2027-03-10T00:00:00+08:00"

    async def test_the_rules_score_clean_whatever_day_it_is(self):
        for now in (datetime(2027, 1, 5, 9, 0, tzinfo=TZ), datetime(2026, 12, 31, 23, 0, tzinfo=TZ),
                    datetime(2028, 2, 29, 9, 0, tzinfo=TZ)):
            report = await nl_eval.evaluate(RulesTranslator(), now=now)
            assert report.wrong == [], (now, report.wrong)


class TestEquivalence:
    def same(self, expected: dict, got: dict) -> bool:
        return nl_eval.equivalent(expected, got, NOW, TZ)

    def test_a_date_without_a_time(self):
        assert self.same({"intent": "list", "filters": {"created_after": TODAY}},
                         {"intent": "list", "filters": {"created_after": "2026-09-24"}})

    def test_the_same_span_two_ways(self):
        assert self.same({"intent": "list", "filters": {"created_after": "1w"}},
                         {"intent": "list", "filters": {"created_after": "7d"}})
        assert self.same({"intent": "list", "filters": {"created_before": "1m"}},
                         {"intent": "list", "filters": {"created_before": "30d"}})

    def test_one_day_either_way_is_the_same_span_but_not_ten(self):
        assert self.same({"intent": "list", "filters": {"created_after": "7d"}},
                         {"intent": "list", "filters": {"created_after": "8d"}})
        assert not self.same({"intent": "list", "filters": {"created_after": "7d"}},
                             {"intent": "list", "filters": {"created_after": "17d"}})

    def test_a_list_in_another_order(self):
        assert self.same({"intent": "list", "filters": {"kinds": ["video", "image"]}},
                         {"intent": "list", "filters": {"kinds": ["image", "video"]}})

    def test_extensions_the_kinds_cover(self):
        assert self.same({"intent": "list", "filters": {"kinds": ["video"]}},
                         {"intent": "list", "filters": {"kinds": ["video"],
                                                        "extensions": ["mkv", "mp4"]}})
        assert not self.same({"intent": "list", "filters": {"kinds": ["video"]}},
                             {"intent": "list", "filters": {"kinds": ["video"],
                                                            "extensions": ["srt"]}})

    def test_a_destination_counts_only_where_it_moves_something(self):
        assert self.same({"intent": "trash", "scope": {"path": "/Temp"}},
                         {"intent": "trash", "scope": {"path": "/Temp"},
                          "action_args": {"dest": "/Trash"}})
        assert not self.same({"intent": "move", "action_args": {"dest": "/A"}},
                             {"intent": "move", "action_args": {"dest": "/B"}})

    def test_the_upper_bound_a_model_adds_to_today_or_this_month(self):
        def with_bound(after: str, before: str) -> dict:
            return {"intent": "list", "filters": {"created_after": after, "created_before": before}}

        assert self.same({"intent": "list", "filters": {"created_after": TODAY}},
                         with_bound(TODAY, TOMORROW))
        assert self.same({"intent": "list", "filters": {"created_after": MONTH}},
                         with_bound(MONTH, NEXT_MONTH))
        # An upper bound that is not the natural end is not free.
        assert not self.same({"intent": "list", "filters": {"created_after": TODAY}},
                             with_bound(TODAY, "2026-09-30T00:00:00+08:00"))

    def test_a_real_difference_stays_wrong(self):
        assert not self.same({"intent": "list", "filters": {"min_size": 5}},
                             {"intent": "list", "filters": {"min_size": 6}})
        assert not self.same({"intent": "list"}, {"intent": "trash"})
        assert not self.same({"intent": "list", "filters": {"created_after": "7d"}},
                             {"intent": "list", "filters": {"created_before": "7d"}})


class Fixed:
    """A translator that answers each sentence from a table."""

    name = "fixed"

    def __init__(self, answers):
        self.answers = answers

    async def translate(self, text, now, tz):
        answer = self.answers[text]
        if isinstance(answer, Exception):
            raise answer
        return answer


CASES = """\
timezone: Asia/Shanghai
now: "2026-09-24T12:00:00+08:00"
cases:
  - text: exact
    expect: {intent: list, filters: {created_after: "{today}"}}
  - text: alike
    expect: {intent: list, filters: {created_after: "1w", kinds: [video]}}
  - text: backwards trash
    expect: {intent: trash, scope: {path: /Temp}, filters: {created_before: "7d"}}
  - text: zero
    expect: {intent: trash, scope: {path: /Temp}, filters: {min_size: 5}}
  - text: plain wrong
    expect: {intent: list, filters: {min_size: 5}}
  - text: refuse
    reject: true
  - text: missed
    expect: {intent: list}
"""


class TestScoring:
    async def test_right_equivalent_wrong_and_dangerous_are_counted_apart(self, tmp_path):
        cases = tmp_path / "cases.yaml"
        cases.write_text(CASES, encoding="utf-8")
        q = Query.model_validate
        answers = {
            "exact": q({"intent": "list", "filters": {"created_after": TODAY}}),
            "alike": q({"intent": "list", "filters": {"created_after": "7d", "kinds": ["video"],
                                                      "extensions": ["mkv"]}}),
            "backwards trash": q({"intent": "trash", "scope": {"path": "/Temp"},
                                  "filters": {"created_after": "7d"}}),
            "zero": q({"intent": "trash", "scope": {"path": "/Temp"},
                       "filters": {"min_size": 5, "max_size": 0}}),
            "plain wrong": q({"intent": "list", "filters": {"min_size": 6}}),
            "refuse": q({"intent": "trash", "scope": {"path": "/"}}),   # destructive, unasked
            "missed": None,
        }
        report = await nl_eval.evaluate(Fixed(answers), cases)
        summary = report.summary()
        assert (summary["right"], summary["equivalent"], summary["wrong"],
                summary["dangerous"]) == (1, 1, 4, 3)
        assert summary["handled"] == 6 and summary["coverage"] == round(6 / 7, 3)
        assert summary["equivalent_accuracy"] == round(2 / 6, 3)
        assert summary["accuracy"] == round(1 / 6, 3)
        assert [d["text"] for d in report.dangerous] == ["backwards trash", "zero", "refuse"]

    def test_a_wrong_answer_that_is_merely_wrong_is_not_dangerous(self):
        case = {"text": "x", "expect": {"intent": "list", "filters": {"min_size": 5}}}
        got = Query.model_validate({"intent": "list", "filters": {"min_size": 6}})
        assert not nl_eval.is_dangerous(case, got, NOW, TZ)

    def test_the_command_prints_the_new_counts(self, capsys):
        assert nl_eval.main(["--backend", "rules", "--json", "--now",
                             "2026-10-03T09:00:00+08:00"]) == 0
        import json

        out = json.loads(capsys.readouterr().out)
        assert {"equivalent", "dangerous", "equivalent_accuracy"} <= set(out)
        assert out["dangerous"] == 0 and out["wrong"] == 0


class TestAModelAnswerEndToEnd:
    """What the Mac sent, through the translator, as the bot would get it."""

    async def test_today_and_a_zero_size_and_a_quartz_cron(self):
        import json

        from pikpak_wms.nl.translator import OpenAITranslator

        async def post(_url, _body, _headers):
            answer = wire(intent="trash", scope={"path": "/Temp", "recursive": True},
                          filters=filters(created_after="today", max_size=0),
                          action_args={"dest": "/Recycle Bin", "template": None, "part": None})
            return 200, {"choices": [{"message": {"content": json.dumps(answer)},
                                      "finish_reason": "stop"}]}

        async def up(*_args):
            return 200

        tr = OpenAITranslator(base_url="http://m/v1", model="q", post=post, get=up)
        result = await tr.translate("删除 /Temp 里今天的文件", NOW, TZ)
        assert result.filters.created_after == "2026-09-24T00:00:00+08:00"
        assert result.filters.max_size is None and result.action_args.dest is None
        assert tr.stats.invalid_before == 1 and tr.stats.invalid_after == 0


class TestTheChainAsTheBotRunsIt:
    async def test_with_rules_the_model_is_asked_only_what_the_rules_decline(self, tmp_path):
        cases = tmp_path / "cases.yaml"
        cases.write_text(
            "timezone: Asia/Shanghai\nnow: '2026-09-24T12:00:00+08:00'\ncases:\n"
            "  - text: 整理一下 Telegram\n"
            "    expect: {intent: organize_inbox, scope: {path: /Telegram}}\n"
            "  - text: 你好\n    reject: true\n", encoding="utf-8")

        class Model:
            name = "openai"
            asked: list[str] = []  # noqa: RUF012

            async def translate(self, text, now, tz):
                self.asked.append(text)
                return None

        model = Model()
        alone = await nl_eval.evaluate(Chain([model]), cases)
        assert model.asked == ["整理一下 Telegram", "你好"] and alone.handled == 0
        model.asked.clear()
        chained = await nl_eval.evaluate(Chain([RulesTranslator(), model]), cases)
        assert model.asked == [] and chained.right == 2

    def test_the_flag_is_there(self):
        with pytest.raises(SystemExit):   # --base-url still needs --backend openai
            nl_eval.main(["--backend", "rules", "--with-rules", "--base-url", "http://x/v1"])
