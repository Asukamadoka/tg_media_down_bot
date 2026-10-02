"""WMS M8.3: model runaway (A), time direction (B), unfounded conditions (C), the rules
(D, K), who understood (I), and the new models (J). No test reaches a real host."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from test_wms_m8 import MAC, NOW, Clock, Lan, ok, wire
from wms_fakes import FakeDrive, provider_for

from pikpak_wms.config import Config
from pikpak_wms.core.client import WmsClient
from pikpak_wms.core.ratelimit import TokenBucket
from pikpak_wms.i18n import set_language
from pikpak_wms.nl import eval as nl_eval
from pikpak_wms.nl.guard import ground, guard
from pikpak_wms.nl.hosts import BOARD, HostBoard, parse_hosts
from pikpak_wms.nl.query import Clarification, Query, from_wire, normalize_wire
from pikpak_wms.nl.rules_parser import RulesTranslator
from pikpak_wms.nl.translator import (
    Chain,
    CutOff,
    GenerationTimeout,
    OpenAITranslator,
    TranslationError,
    strip_thinking,
)
from pikpak_wms.ops import nl as nl_ops
from pikpak_wms.ops import plans
from pikpak_wms.ops.context import Context
from pikpak_wms.ops.stocktake import stocktake
from pikpak_wms.store.db import Store

SH = ZoneInfo("Asia/Shanghai")
TODAY = datetime(2026, 10, 1, 12, 0, tzinfo=SH)


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language(None)


@pytest.fixture
def clock():
    return Clock()


def parse(text):
    return RulesTranslator().parse(text, TODAY, SH)


def model(lan, clock, **kw):
    kw.setdefault("urls", MAC)
    urls, models = kw.pop("urls"), kw.pop("models", "m")
    return OpenAITranslator(base_url=urls, model=models, api_key="", post=lan.post, get=lan.get,
                            board=HostBoard(clock=clock), **kw)


# ------------------------------------------------------------------ A


class TestRunaway:
    async def test_the_request_caps_the_answer(self, clock):
        lan, bodies = Lan(MAC), []
        original = lan.post

        async def post(url, body, headers):
            bodies.append(body)
            return await original(url, body, headers)

        lan.post = post
        await model(lan, clock).translate("去重", NOW, UTC)
        assert bodies[0]["max_tokens"] == 512
        await model(lan, clock, max_tokens="400").translate("去重", NOW, UTC)
        assert bodies[1]["max_tokens"] == 400

    def test_a_limit_for_all_hosts_or_one_each(self):
        assert [h.max_tokens for h in parse_hosts("http://a/v1,http://b/v1", "m", "", "300")] == [
            300, 300]
        assert [h.max_tokens for h in parse_hosts("http://a/v1,http://b/v1", "m", "", "300,900")
                ] == [300, 900]

    async def test_a_cut_off_answer_fails_that_sentence_and_not_the_host(self, clock):
        lan = Lan(MAC)
        posts = []

        async def post(url, body, headers):
            posts.append(body)
            if len(posts) == 1:
                return 200, {"choices": [{"message": {"content": '{"intent": "tr'},
                                          "finish_reason": "length"}]}
            return ok(wire())

        lan.post = post
        board = HostBoard(clock=clock)
        tr = OpenAITranslator(base_url=MAC, model="m", api_key="", post=post, get=lan.get,
                              board=board)
        with pytest.raises(CutOff) as caught:
            await tr.translate("清理一下", NOW, UTC)
        assert caught.value.display() == "I did not get that one right. Try putting it another way."
        assert board.states[MAC].online is True
        assert (await tr.translate("去重", NOW, UTC)).intent == "dedupe"  # the next one is fine
        assert len(posts) == 2  # not retried

    async def test_a_timeout_with_a_live_host_is_only_that_sentence(self, clock):
        lan = Lan(MAC)
        calls = []

        async def post(url, body, headers):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("no answer in 30 s")
            return ok(wire())

        board = HostBoard(clock=clock)
        tr = OpenAITranslator(base_url=MAC, model="m", api_key="", post=post, get=lan.get,
                              board=board, timeout=30)
        with pytest.raises(GenerationTimeout) as caught:
            await tr.translate("清理一下", NOW, UTC)
        assert "30 s" in caught.value.display()
        assert len(calls) == 1  # no second try of the same sentence
        assert board.states[MAC].online is True
        assert (await tr.translate("去重", NOW, UTC)).intent == "dedupe"

    async def test_a_timeout_when_the_probe_fails_too_is_a_host_that_went_away(self, clock):
        lan = Lan(MAC)

        async def post(url, body, headers):
            lan.up.clear()  # the host dies while generating
            raise TimeoutError("no answer")

        tr = OpenAITranslator(base_url=MAC, model="m", api_key="", post=post, get=lan.get,
                              board=HostBoard(clock=clock))
        with pytest.raises(TranslationError) as caught:
            await tr.translate("去重", NOW, UTC)
        assert caught.value.key == "nl.error.offline"

    async def test_a_refused_connection_still_means_offline(self, clock):
        lan = Lan(MAC)

        async def post(url, body, headers):
            lan.up.clear()
            raise ConnectionRefusedError("refused")

        tr = OpenAITranslator(base_url=MAC, model="m", api_key="", post=post, get=lan.get,
                              board=HostBoard(clock=clock))
        with pytest.raises(TranslationError) as caught:
            await tr.translate("去重", NOW, UTC)
        assert caught.value.key == "nl.error.offline"

    def test_the_default_wait_is_thirty_seconds(self, monkeypatch):
        monkeypatch.delenv("NL_OPENAI_TIMEOUT", raising=False)
        assert OpenAITranslator(base_url=MAC, model="m").timeout == 30

    async def test_the_eval_resets_the_hosts_before_every_sentence_and_counts_kinds(
        self, tmp_path
    ):
        cases = tmp_path / "cases.yaml"
        cases.write_text(
            "timezone: Asia/Shanghai\nnow: '2026-10-01T12:00:00+08:00'\ncases:\n"
            "  - {text: 'one', expect: {intent: dedupe}}\n"
            "  - {text: 'two', expect: {intent: dedupe}}\n"
            "  - {text: 'three', expect: {intent: dedupe}}\n"
            "  - {text: 'four', expect: {intent: dedupe}}\n", encoding="utf-8")

        class Fails:
            name = "fake"

            def __init__(self):
                self.seen = []

            async def translate(self, text, now, tz):
                self.seen.append(dict(BOARD.states))  # what the board knew on arrival
                BOARD.states["x"] = object()  # something this sentence taught it
                kinds = {"one": CutOff("c", key="nl.error.cut_off"),
                         "two": GenerationTimeout("t", key="nl.error.timeout", seconds="30"),
                         "three": TranslationError("o", key="nl.error.offline", hosts="")}
                if text in kinds:
                    raise kinds[text]
                return Query.model_validate({"intent": "dedupe"})

        fake = Fails()
        report = await nl_eval.evaluate(fake, cases)
        assert fake.seen == [{}, {}, {}, {}]  # every sentence starts clean
        assert report.summary()["errors_by_kind"] == {
            "cut_off": 1, "timeout": 1, "offline": 1, "other": 0}
        assert report.right == 1


# ------------------------------------------------------------------ B


class TestBothBounds:
    @pytest.mark.parametrize(("sentence", "after", "before"), [
        ("把 /Media 里超过半年的文件归档", "180d", "now"),
        ("三个月前的视频", "90d", "2026-10-01T00:00:00+08:00"),
        ("30 天以前的文件", "30d", "0d"),
    ])
    def test_an_older_sentence_with_both_bounds_means_before(self, sentence, after, before):
        data = normalize_wire({"intent": "archive", "filters": {
            "created_after": after, "created_before": before}}, now=TODAY, tz=SH)
        result = guard(sentence, from_wire(data), now=TODAY, tz=SH)
        assert result.filters.created_before == after and result.filters.created_after is None
        assert result.corrections == ["nl.explain.direction_fixed"]

    def test_a_real_window_is_left_alone(self):
        # 「三个月前到一个月前」: the upper bound is a month ago, not now.
        query = Query.model_validate({"intent": "list", "filters": {
            "created_after": "90d", "created_before": "30d"}})
        result = guard("三个月前到一个月前的文件", query, now=TODAY, tz=SH)
        assert (result.filters.created_after, result.filters.created_before) == ("90d", "30d")


# ------------------------------------------------------------------ C


class TestUnfoundedConditions:
    def make(self, **filters):
        return Query.model_validate({"intent": "download", "filters": filters})

    def test_the_k_flow_does_not_invent_a_time_or_a_size(self):
        # Sentence + the filename it was completed with; the model added both ranges.
        query = self.make(created_after="2026-10-01T06:00:00+08:00",
                          created_before="2026-10-01T23:59:00+08:00",
                          min_size=1073741824, max_size=10737418240, kinds=["video"])
        sentence = "只下载一个示例影像的视频，【示例影像】31号模特_2026_样片合集.mp4"
        result = ground(sentence, query)
        f = result.filters
        assert (f.created_after, f.created_before, f.min_size, f.max_size) == (None,) * 4
        assert f.kinds == ["video"]
        assert result.corrections == ["nl.explain.dropped:size,time"]

    def test_words_in_the_sentence_are_a_basis(self):
        query = self.make(created_after="7d", min_size=1073741824)
        result = ground("下载最近7天大于1GB的视频", query)
        assert result.filters.created_after == "7d" and result.filters.min_size == 1073741824
        assert result.corrections == []

    def test_a_name_that_is_not_in_the_sentence_goes_but_one_that_is_stays(self):
        invented = ground("下载视频", self.make(name_contains=["Lost"]))
        assert invented.filters.name_contains == [] and invented.corrections == [
            "nl.explain.dropped:name"]
        stays = ground("下载示例影像的视频", self.make(name_contains=["示例影像"]))
        assert stays.filters.name_contains == ["示例影像"] and stays.corrections == []
        regex = ground("以 sample 开头的文件", self.make(name_regex="^sample"))
        assert regex.filters.name_regex == "^sample"
        assert ground("下载视频", self.make(name_regex="^zzz")).filters.name_regex is None

    def test_one_size_given_as_both_bounds_without_equals_goes(self):
        query = self.make(min_size=1073741824, max_size=1073741824)
        assert ground("下载大于1GB的视频", query).filters.min_size is None
        equal = self.make(min_size=1073741824, max_size=1073741824)
        assert ground("下载等于1GB的视频", equal).filters.min_size == 1073741824

    async def test_the_chain_applies_it_to_models_only_and_says_who(self, clock):
        lan = Lan(MAC, answer=wire(intent="download", filters={
            **wire()["filters"], "min_size": 1073741824, "kinds": ["video"]}))
        chain = Chain([RulesTranslator(), model(lan, clock, names="Mac", models="qwen-x")])
        result = await chain.translate("来点视频", TODAY, SH)
        assert result.filters.min_size is None
        assert "nl.explain.dropped:size" in result.corrections
        assert chain.last_label == "rules + local model qwen-x (Mac)"

    async def test_the_plan_says_what_was_dropped(self, tmp_path):
        drive = FakeDrive()
        async with Store(tmp_path / "w.sqlite3") as store:
            client = WmsClient(provider_for(drive), limiter=TokenBucket(1e9, 10**6),
                               sleep=_no_sleep)
            ctx = Context(config=Config(), client=client, store=store)
            query = Query.model_validate({"intent": "list", "filters": {"kinds": ["video"]}})
            query.corrections.append("nl.explain.dropped:size,time")
            proposal = await nl_ops.make_proposal(ctx, query, now=TODAY)
        assert "Dropped conditions the sentence does not mention: size, time" in "\n".join(
            nl_ops.proposal_lines(proposal))


async def _no_sleep(_s):
    return None


# ------------------------------------------------------------------ D


class TestRulesLayer:
    @pytest.mark.parametrize("sentence", ["不要删除 /Inbox 里的视频", "别删除视频", "不用整理了",
                                          "请不要下载这个"])
    def test_a_negated_instruction_does_nothing(self, sentence):
        result = parse(sentence)
        assert isinstance(result, Clarification) and result.question == "nl.ask.negated"

    def test_the_reply_is_a_plain_okay(self):
        from pikpak_wms.ops.nl import clarification_text

        assert clarification_text(parse("不要删除 /Inbox 里的视频")) == (
            "All right, I will not do anything.")

    def test_an_instruction_is_not_mistaken_for_a_negation(self):
        assert parse("删除 /Inbox 里的视频").intent == "trash"

    def test_the_end_of_the_day_is_a_natural_boundary(self):
        expected = {"intent": "list", "filters": {"created_after": "2026-10-01T00:00:00+08:00"}}
        got = {"intent": "list", "filters": {"created_after": "2026-10-01T00:00:00+08:00",
                                              "created_before": "2026-10-01T23:59:59+08:00"}}
        assert nl_eval.equivalent(expected, got, TODAY, SH)
        late = {"intent": "list", "filters": {"created_after": "2026-10-01T00:00:00+08:00",
                                               "created_before": "2026-10-01T18:00:00+08:00"}}
        assert not nl_eval.equivalent(expected, late, TODAY, SH)

    def test_a_week_starts_on_monday_in_the_prompt(self):
        from pikpak_wms.nl.translator import SYSTEM_PROMPT

        assert "a week starts on Monday" in SYSTEM_PROMPT

    def test_a_move_with_its_time_the_wrong_way_is_dangerous(self):
        case = {"text": "x", "expect": {"intent": "move", "action_args": {"dest": "/A"},
                                        "filters": {"created_before": "30d"}}}
        wrong = Query.model_validate({"intent": "move", "action_args": {"dest": "/A"},
                                      "filters": {"created_after": "30d"}})
        assert nl_eval.is_dangerous(case, wrong, TODAY, SH)
        listing = Query.model_validate({"intent": "list", "filters": {"created_after": "30d"}})
        assert not nl_eval.is_dangerous(case, listing, TODAY, SH)


# ------------------------------------------------------------------ K


class TestAFullFileName:
    NAME = "【示例影像】31号模特_2026.9.30_样片合集.mp4"

    def test_the_name_alone_is_taken_by_the_rules_with_the_original_sentence(self):
        result = parse(f"只下载一个示例影像的视频，{self.NAME}")
        assert result.intent == "download"
        assert result.filters.name_equals == self.NAME  # exactly as typed, brackets and all
        assert result.filters.limit == 1
        assert result.filters.created_after is None and result.filters.min_size is None

    def test_a_path_is_not_a_file_name(self):
        assert parse("下载 /Inbox/a.mp4").filters.name_equals is None

    def test_other_files_are_not_matched_and_the_wording_around_it_is_free(self):
        assert parse(f"把{self.NAME}删除").intent == "trash"
        assert parse(f"删除除了{self.NAME}以外的视频") is None  # turned around: not ours

    @pytest.mark.parametrize(("sentence", "n"), [
        ("只下载一个视频", 1), ("只下载两个视频", 2), ("下载前3个视频", 3),
        ("下载最新的2个视频", 2), ("下载最新的视频", 1),
    ])
    def test_counts(self, sentence, n):
        assert parse(sentence).filters.limit == n

    def test_a_span_of_months_is_not_a_count(self):
        result = parse("下载最近3个月的视频")
        assert result.filters.limit is None and result.filters.created_after == "90d"

    def test_a_count_cannot_be_scheduled(self):
        query = Query.model_validate({"intent": "download", "filters": {"limit": 1},
                                      "schedule": {"cron": "0 3 * * *"}})
        assert query.needs_clarification == "nl.ask.limit_schedule"

    async def test_the_plan_takes_exactly_that_file_and_only_the_newest_n(self, tmp_path):
        drive = FakeDrive()
        drive.add("/V/a.mp4", size=5, created="2026-09-01T00:00:00+08:00")
        drive.add("/V/b.mp4", size=5, created="2026-09-02T00:00:00+08:00")
        drive.add("/V/c.mp4", size=5, created="2026-09-03T00:00:00+08:00")
        drive.add("/V/other.mkv", size=5, created="2026-09-04T00:00:00+08:00")
        async with Store(tmp_path / "w.sqlite3") as store:
            client = WmsClient(provider_for(drive), limiter=TokenBucket(1e9, 10**6),
                               sleep=_no_sleep)
            config = Config()
            config.outbound.local_dir = tmp_path / "out"
            ctx = Context(config=config, client=client, store=store)
            await stocktake(client, store, full=True)

            exact = Query.model_validate(
                {"intent": "download", "filters": {"name_equals": "b.mp4"}})
            proposal = await nl_ops.make_proposal(ctx, exact, now=TODAY)
            assert [a.before["path"] for a in proposal.plan.actions] == ["/V/b.mp4"]
            assert "File name is exactly “b.mp4”" in "\n".join(nl_ops.proposal_lines(proposal))

            newest = Query.model_validate({"intent": "download", "filters": {
                "kinds": ["video"], "limit": 2}})
            proposal = await nl_ops.make_proposal(ctx, newest, now=TODAY)
            assert sorted(a.before["path"] for a in proposal.plan.actions) == [
                "/V/c.mp4", "/V/other.mkv"]
            assert proposal.count == 2
            await plans.discard(ctx, proposal.plan_id)


# ------------------------------------------------------------------ I


class TestWhoUnderstood:
    async def test_names_per_host_and_the_display(self, clock):
        hosts = parse_hosts("http://10.0.0.1:11434/v1,http://192.168.0.50:11434/v1",
                            "qwen3.6-35b-a3b", "Mac,WinPC")
        assert [h.display for h in hosts] == ["Mac", "WinPC"]
        assert parse_hosts("http://192.168.0.50:11434/v1", "m")[0].display == "192.168.0.50"

    async def test_a_model_is_called_a_local_model_not_openai(self, clock):
        lan = Lan(MAC)
        chain = Chain([RulesTranslator(), model(lan, clock, names="Mac",
                                                 models="qwen3.6-35b-a3b")])
        await chain.translate("去重", TODAY, SH)  # rules read this one
        assert chain.last_label == "rules"
        lan.answer = wire(filters={**wire()["filters"], "kinds": ["video"]},
                          intent="list")
        await chain.translate("来点视频", TODAY, SH)
        assert chain.last_label == "local model qwen3.6-35b-a3b (Mac)"

    async def test_in_chinese(self, clock):
        set_language("zh")
        lan = Lan(MAC, answer=wire(intent="list"))
        chain = Chain([model(lan, clock, names="Mac", models="qwen3.6-35b-a3b")])
        await chain.translate("来点东西", TODAY, SH)
        assert chain.last_label == "本地模型 qwen3.6-35b-a3b（Mac）"

    async def test_doctor_style_rows_carry_the_name(self, clock, monkeypatch):
        monkeypatch.setenv("NL_OPENAI_BASE_URL", MAC)
        monkeypatch.setenv("NL_OPENAI_MODEL", "m")
        monkeypatch.setenv("NL_OPENAI_NAMES", "Mac")
        (host,) = await nl_ops.model_hosts(probe=False)
        assert host.name == "Mac"


# ------------------------------------------------------------------ J


class TestNewModels:
    async def test_thinking_is_off_by_default(self, clock):
        lan, bodies = Lan(MAC), []
        original = lan.post

        async def post(url, body, headers):
            bodies.append(body)
            return await original(url, body, headers)

        lan.post = post
        await model(lan, clock).translate("去重", NOW, UTC)
        assert bodies[0]["reasoning_effort"] == "none"
        await model(lan, clock, think="on").translate("去重", NOW, UTC)
        assert "reasoning_effort" not in bodies[1]

    async def test_a_server_that_refuses_reasoning_effort_is_asked_without_it(self, clock):
        lan, bodies = Lan(MAC), []

        async def post(url, body, headers):
            bodies.append(body)
            if "reasoning_effort" in body:
                return 400, {"error": {"message": "unknown field reasoning_effort"}}
            return ok(wire())

        tr = OpenAITranslator(base_url=MAC, model="m", api_key="", post=post, get=lan.get,
                              board=HostBoard(clock=clock))
        assert (await tr.translate("去重", NOW, UTC)).intent == "dedupe"
        await tr.translate("去重", NOW, UTC)
        # First sentence: the schema form and the json_object form are both refused (the
        # server dislikes reasoning_effort, not the format); without it, it answers. The
        # second sentence never sends it.
        assert ["reasoning_effort" in body for body in bodies] == [True, True, False, False]

    def test_thinking_text_is_removed_before_the_json(self):
        assert strip_thinking('<think>let me see\n{"a": 1}</think>{"b": 2}') == '{"b": 2}'
        assert strip_thinking('<think>never closed {"b": 2}') == ""
        assert strip_thinking('{"b": 2}') == '{"b": 2}'

    async def test_a_think_block_and_reasoning_content_do_not_reach_the_parser(self, clock):
        lan = Lan(MAC)

        async def post(url, body, headers):
            return 200, {"choices": [{"message": {
                "content": "<think>hmm {not json}</think>" + json.dumps(wire()),
                "reasoning_content": "some reasoning {"}, "finish_reason": "stop"}]}

        tr = OpenAITranslator(base_url=MAC, model="m", api_key="", post=post, get=lan.get,
                              board=HostBoard(clock=clock))
        assert (await tr.translate("去重", NOW, UTC)).intent == "dedupe"

    async def test_the_retry_model_looks_again_when_the_first_is_cut_off(self, clock):
        lan, asked = Lan(MAC), []

        async def post(url, body, headers):
            asked.append(body["model"])
            if body["model"] == "fast":
                return 200, {"choices": [{"message": {"content": "{"},
                                          "finish_reason": "length"}]}
            return ok(wire())

        tr = OpenAITranslator(base_url=MAC, model="fast", api_key="", post=post, get=lan.get,
                              board=HostBoard(clock=clock), names="Mac", retry_model="strong")
        chain = Chain([tr])
        result = await chain.translate("来点东西", TODAY, SH)
        assert result.intent == "dedupe" and asked == ["fast", "strong"]
        assert chain.last_label == "strong (Mac), on a second look"

    async def test_the_retry_model_is_also_asked_after_a_question_or_a_bad_schema(self, clock):
        lan, asked = Lan(MAC), []

        async def post(url, body, headers):
            asked.append(body["model"])
            if body["model"] == "fast":
                return ok(wire(needs_clarification="Which one?"))
            return ok(wire(intent="list"))

        tr = OpenAITranslator(base_url=MAC, model="fast", api_key="", post=post, get=lan.get,
                              board=HostBoard(clock=clock), retry_model="strong")
        assert (await tr.translate("来点东西", NOW, UTC)).intent == "list"
        asked.clear()

        async def broken(url, body, headers):
            asked.append(body["model"])
            if body["model"] == "fast":
                return 200, {"choices": [{"message": {"content": '{"intent": "nope"}'},
                                          "finish_reason": "stop"}]}
            return ok(wire())

        tr = OpenAITranslator(base_url=MAC, model="fast", api_key="", post=broken, get=lan.get,
                              board=HostBoard(clock=clock), retry_model="strong")
        assert (await tr.translate("来点东西", NOW, UTC)).intent == "dedupe"
        assert asked == ["fast", "strong"]

    async def test_without_a_retry_model_nothing_changes(self, clock):
        lan, asked = Lan(MAC), []
        original = lan.post

        async def post(url, body, headers):
            asked.append(body["model"])
            return await original(url, body, headers)

        lan.post = post
        await model(lan, clock).translate("去重", NOW, UTC)
        assert asked == ["m"]

    async def test_a_second_look_that_fails_keeps_the_first_outcome(self, clock):
        lan = Lan(MAC)

        async def post(url, body, headers):
            if body["model"] == "fast":
                return 200, {"choices": [{"message": {"content": "{"},
                                          "finish_reason": "length"}]}
            raise TimeoutError("too slow")

        tr = OpenAITranslator(base_url=MAC, model="fast", api_key="", post=post, get=lan.get,
                              board=HostBoard(clock=clock), retry_model="strong")
        with pytest.raises(CutOff):
            await tr.translate("来点东西", NOW, UTC)


_ = asyncio
