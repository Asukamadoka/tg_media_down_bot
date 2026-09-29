"""WMS M8 (docs/wms/M8-lan-model-hosts.md) §A and §B.

§A: model hosts on the LAN that are not always on: several in order, a
quick check of who is up, a remembered verdict, and an immediate answer when
none is. §B: what small models get wrong, forgiven before the schema check.
No test reaches a real host; conftest makes an unstubbed probe fail.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from pikpak_wms.i18n import set_language
from pikpak_wms.nl import hosts as hosts_module
from pikpak_wms.nl.hosts import HostBoard, HostsConfigError, parse_hosts
from pikpak_wms.nl.query import normalize_wire, quartz_to_cron
from pikpak_wms.nl.rules_parser import RulesTranslator
from pikpak_wms.nl.translator import (
    SYSTEM_PROMPT,
    Chain,
    OpenAITranslator,
    TranslationError,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
MAC = "http://10.10.10.1:11434/v1"
PC = "http://192.168.0.50:11434/v1"


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language(None)


def wire(**overrides):
    answer = {"intent": "dedupe", "scope": {"path": "/", "recursive": True},
              "filters": {"created_after": None, "created_before": None, "min_size": None,
                          "max_size": None, "kinds": [], "extensions": [],
                          "name_contains": [], "name_regex": None},
              "action_args": {"dest": None, "template": None, "part": None},
              "schedule": None, "needs_clarification": None}
    answer.update(overrides)
    return answer


def ok(answer):
    return 200, {"choices": [{"message": {"content": json.dumps(answer)},
                              "finish_reason": "stop"}]}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Lan:
    """Which hosts answer, and what was asked of each."""

    def __init__(self, *up, answer=None):
        self.up = set(up)
        self.probes: list[str] = []
        self.posts: list[str] = []
        self.answer = answer or wire()
        self.drop_mid_way: set[str] = set()

    async def get(self, url, headers, timeout):
        self.probes.append(url)
        assert timeout == 1.5
        if not any(url.startswith(host) for host in self.up):
            raise ConnectionError("no route to host")
        return 200

    async def post(self, url, body, headers):
        self.posts.append(url)
        host = url.removesuffix("/chat/completions")
        if host in self.drop_mid_way:
            raise ConnectionError("connection reset")
        return ok(self.answer)


@pytest.fixture
def clock():
    return Clock()


def translator(lan, clock, *, urls=f"{MAC},{PC}", models="qwen2.5:7b", board=None):
    return OpenAITranslator(base_url=urls, model=models, api_key="", post=lan.post,
                            get=lan.get, board=board or HostBoard(clock=clock))


class TestHosts:
    def test_one_model_for_all_or_one_each(self):
        assert [h.model for h in parse_hosts(f"{MAC},{PC}", "m")] == ["m", "m"]
        assert [h.model for h in parse_hosts(f"{MAC}, {PC}/", "a,b")] == ["a", "b"]
        assert parse_hosts(f"{MAC}, {PC}/", "a,b")[1].base_url == PC
        with pytest.raises(HostsConfigError):
            parse_hosts(f"{MAC},{PC}", "a,b,c")

    async def test_the_first_host_that_is_up_gets_the_sentence(self, clock):
        lan = Lan(PC)  # the Mac is off
        result = await translator(lan, clock).translate("去重", NOW, UTC)
        assert result.intent == "dedupe"
        assert lan.probes == [f"{MAC}/models", f"{PC}/models"]
        assert lan.posts == [f"{PC}/chat/completions"]

    async def test_in_order_when_both_are_up(self, clock):
        lan = Lan(MAC, PC)
        await translator(lan, clock).translate("去重", NOW, UTC)
        assert lan.posts == [f"{MAC}/chat/completions"]

    async def test_an_offline_host_is_not_asked_again_for_a_minute(self, clock):
        lan = Lan(PC)
        board = HostBoard(clock=clock)
        await translator(lan, clock, board=board).translate("去重", NOW, UTC)
        await translator(lan, clock, board=board).translate("去重", NOW, UTC)
        # The Mac once, the PC once: both verdicts stood for the second sentence.
        assert lan.probes == [f"{MAC}/models", f"{PC}/models"]
        clock.now += 61
        lan.up.add(MAC)
        await translator(lan, clock, board=board).translate("去重", NOW, UTC)
        assert lan.posts[-1] == f"{MAC}/chat/completions"

    async def test_all_offline_answers_at_once(self, clock):
        lan = Lan()
        with pytest.raises(TranslationError) as caught:
            await translator(lan, clock).translate("去重", NOW, UTC)
        assert caught.value.key == "nl.error.offline"
        assert lan.posts == []  # nobody waited on a generation
        set_language("zh")
        assert caught.value.display().startswith("模型主机离线")

    async def test_the_rules_still_answer_when_every_host_is_off(self, clock):
        chain = Chain([RulesTranslator(), translator(Lan(), clock)])
        assert (await chain.translate("去重", NOW, UTC)).intent == "dedupe"
        with pytest.raises(TranslationError, match="offline"):
            await chain.translate("帮我想想这周该看点什么", NOW, UTC)

    async def test_a_host_that_drops_mid_way_counts_as_offline(self, clock):
        lan = Lan(MAC, PC)
        lan.drop_mid_way = {MAC}
        board = HostBoard(clock=clock)
        result = await translator(lan, clock, board=board).translate("去重", NOW, UTC)
        assert result.intent == "dedupe"
        assert lan.posts == [f"{MAC}/chat/completions", f"{PC}/chat/completions"]
        assert board.states[MAC].online is False
        assert board.states[PC].latency_ms is not None

    async def test_timeouts(self, monkeypatch):
        monkeypatch.setenv("NL_OPENAI_TIMEOUT", "30")
        assert OpenAITranslator(base_url=MAC, model="m").timeout == 30
        monkeypatch.setenv("NL_OPENAI_TIMEOUT", "soon")
        assert OpenAITranslator(base_url=MAC, model="m").timeout == 60
        assert hosts_module.PROBE_TIMEOUT == 1.5

    async def test_a_model_count_that_fits_nothing_is_an_error(self, clock):
        with pytest.raises(TranslationError, match="3 models for 2 hosts"):
            await translator(Lan(MAC), clock, models="a,b,c").translate("去重", NOW, UTC)

    async def test_the_shared_board_outlives_a_translator(self):
        # The bot builds a translator per sentence; the verdicts must stay.
        lan = Lan(PC)
        first = OpenAITranslator(base_url=f"{MAC},{PC}", model="m", post=lan.post, get=lan.get)
        await first.translate("去重", NOW, UTC)
        again = OpenAITranslator(base_url=f"{MAC},{PC}", model="m", post=lan.post, get=lan.get)
        await again.translate("去重", NOW, UTC)
        assert len(lan.probes) == 2


class TestReport:
    async def test_model_hosts_for_verify_and_doctor(self, monkeypatch):
        from pikpak_wms.ops import nl as nl_ops

        monkeypatch.setenv("NL_OPENAI_BASE_URL", f"{MAC},{PC}")
        monkeypatch.setenv("NL_OPENAI_MODEL", "qwen2.5:7b")
        lan = Lan(PC)
        monkeypatch.setattr(hosts_module, "_http_get", lan.get)
        found = await nl_ops.model_hosts()
        assert [(h.url, h.model, h.online) for h in found] == [
            (MAC, "qwen2.5:7b", False), (PC, "qwen2.5:7b", True)]

    async def test_nothing_when_no_host_is_set(self):
        from pikpak_wms.ops import nl as nl_ops

        assert await nl_ops.model_hosts() == []

    def test_doctor_prints_a_row_per_host(self, monkeypatch, tmp_path):
        from typer.testing import CliRunner

        from pikpak_wms.cli import main as cli

        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("WMS_LANG", "en")
        monkeypatch.setenv("NL_OPENAI_BASE_URL", MAC)
        monkeypatch.setenv("NL_OPENAI_MODEL", "qwen2.5:7b")
        result = CliRunner().invoke(cli.app, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "10.10.10.1" in result.output and "offline" in result.output


class TestNormalizing:
    """§B: the two real errors from the 3B eval, and the prompt that heads them off."""

    @pytest.mark.parametrize(("given", "expected"), [
        ("0 0 * * ? *", "0 * * * *"),        # seen: Quartz with a year
        ("0 0 3 * * ?", "0 3 * * *"),        # Quartz without a year
        ("0 30 4 ? * MON 2026", "30 4 * * MON"),
        ("0 3 * * *", "0 3 * * *"),          # already cron: untouched
    ])
    def test_quartz(self, given, expected):
        assert quartz_to_cron(given) == expected

    def test_null_strings(self):
        cleaned = normalize_wire(wire(schedule="null", needs_clarification="none",
                                      action_args={"dest": "", "template": None,
                                                   "part": "NULL"}))
        assert cleaned["schedule"] is None and cleaned["needs_clarification"] is None
        assert cleaned["action_args"] == {"dest": None, "template": None, "part": None}

    def test_a_null_cron_means_no_schedule(self):
        assert normalize_wire(wire(schedule={"cron": "null"}))["schedule"] is None

    async def test_the_seen_answers_now_pass_and_are_counted(self, clock):
        answers = [
            wire(intent="move", action_args={"dest": "/Media", "template": None,
                                             "part": None}, schedule={"cron": "0 0 * * ? *"}),
            wire(schedule="null", needs_clarification="null"),
            wire(schedule={"cron": "every day"}),   # still wrong after normalizing
            wire(),
        ]
        lan = Lan(MAC)
        tr = translator(lan, clock, urls=MAC)
        results = []
        for answer in answers:
            lan.answer = answer
            try:
                results.append(await tr.translate("x", NOW, UTC))
            except TranslationError:
                results.append(None)
        assert results[0].schedule.cron == "0 * * * *"
        assert results[1].intent == "dedupe" and results[1].schedule is None
        assert results[2] is None
        assert (tr.stats.answers, tr.stats.invalid_before, tr.stats.invalid_after) == (4, 3, 1)

    def test_the_prompt_shows_right_and_wrong(self):
        assert SYSTEM_PROMPT.count('"cron": "0 3 * * *"') >= 1
        assert "Wrong answers" in SYSTEM_PROMPT and "Quartz" in SYSTEM_PROMPT
        assert "When you are unsure, fill needs_clarification" in SYSTEM_PROMPT


class TestEval:
    async def test_it_reports_both_counts(self, clock, tmp_path):
        from pikpak_wms.nl.eval import evaluate

        cases = tmp_path / "cases.yaml"
        cases.write_text(
            "timezone: UTC\nnow: '2026-09-27T12:00:00+00:00'\ncases:\n"
            "  - text: 去重\n    expect: {intent: dedupe}\n", encoding="utf-8")
        lan = Lan(MAC, answer=wire(schedule="null"))
        report = await evaluate(Chain([translator(lan, clock, urls=MAC)]), cases)
        summary = report.summary()
        assert summary["right"] == 1
        assert summary["schema_invalid_as_answered"] == 1
        assert summary["schema_invalid_after_normalizing"] == 0

    def test_base_url_picks_one_host_and_its_model(self, monkeypatch):
        from pikpak_wms.nl.eval import model_for

        monkeypatch.setenv("NL_OPENAI_BASE_URL", f"{MAC},{PC}")
        monkeypatch.setenv("NL_OPENAI_MODEL", "big,small")
        assert model_for(PC) == "small"
        assert model_for("http://other:1/v1") == "big"

    def test_base_url_only_with_openai(self):
        from pikpak_wms.nl.eval import main

        with pytest.raises(SystemExit):
            main(["--backend", "rules", "--base-url", MAC])
