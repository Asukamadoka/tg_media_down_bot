"""WMS M6: natural-language commands, from a sentence to the audit trail.

Model backends are always fakes here (no network); ``python -m
pikpak_wms.nl.eval --backend claude|ollama`` is how they are measured for real.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import yaml
from typer.testing import CliRunner
from wms_fakes import FakeDrive, provider_for

from pikpak_wms.cli import main as cli
from pikpak_wms.config import Config
from pikpak_wms.core.client import WmsClient
from pikpak_wms.core.models import render_note
from pikpak_wms.core.ratelimit import TokenBucket
from pikpak_wms.i18n import set_language
from pikpak_wms.nl import eval as nl_eval
from pikpak_wms.nl.query import Clarification, Query, from_wire, wire_schema
from pikpak_wms.nl.rules_parser import RulesTranslator
from pikpak_wms.nl.translator import (
    Chain,
    ClaudeTranslator,
    OllamaTranslator,
    TranslationError,
    build,
    from_environment,
)
from pikpak_wms.ops import nl, outbound, plans, rulesfile
from pikpak_wms.ops.context import Context
from pikpak_wms.rules.schema import load_rules, parse_rules
from pikpak_wms.store.db import Store

ROOT = Path(__file__).resolve().parent.parent
SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=SHANGHAI)
GB = 1024**3
USERS_SENTENCE = "下载今天转存到网盘的所有大于1GB的视频"


@pytest.fixture(autouse=True)
def chinese():
    set_language("zh")
    yield
    set_language(None)


# ------------------------------------------------------------ the eval set


class TestEvalSet:
    def test_at_least_sixty_chinese_cases_including_the_users_sentence(self):
        data = yaml.safe_load((ROOT / "tests/nl/cases.yaml").read_text(encoding="utf-8"))
        texts = [case["text"] for case in data["cases"]]
        assert len(texts) >= 60
        assert USERS_SENTENCE in texts
        assert sum(1 for case in data["cases"] if case.get("clarify")) >= 8
        for case in data["cases"]:
            if case.get("expect"):
                Query.model_validate(case["expect"])  # every expectation is a valid Query

    async def test_rules_covers_seventy_percent_with_zero_wrong(self):
        report = await nl_eval.evaluate(RulesTranslator(), ROOT / "tests/nl/cases.yaml")
        summary = report.summary()
        assert report.wrong == [], report.wrong
        assert summary["coverage"] >= 0.70, summary

    def test_the_eval_command(self, capsys):
        assert nl_eval.main(["--backend", "rules", "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["backend"] == "rules" and out["wrong"] == 0


# ---------------------------------------------------------------- world


class World:
    def __init__(self, drive, ctx):
        self.drive, self.ctx = drive, ctx

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
    config.outbound.local_dir = tmp_path / "media"
    async with Store(tmp_path / "wms.sqlite3") as store:
        client = WmsClient(provider_for(drive), limiter=TokenBucket(1e9, 1_000_000))
        yield World(drive, Context(config=config, client=client, store=store))


def arrivals(drive: FakeDrive) -> None:
    """Today in Shanghai started at 16:00 UTC yesterday."""
    drive.add("/TelegramMedia/big.today.mkv", size=3 * GB, created="2026-09-24T02:00:00+00:00")
    drive.add("/Inbox/huge.today.mp4", size=2 * GB, created="2026-09-23T17:00:00+00:00")
    drive.add("/Inbox/small.today.mkv", size=GB // 2, created="2026-09-24T01:00:00+00:00")
    drive.add("/Inbox/big.yesterday.mkv", size=4 * GB, created="2026-09-23T15:00:00+00:00")
    drive.add("/Inbox/photo.today.jpg", size=2 * GB, created="2026-09-24T01:00:00+00:00")


# ----------------------------------------------------------- end to end


class TestTheUsersSentence:
    async def test_sentence_to_plan_to_nas_to_audit(self, world, tmp_path):
        w = world
        arrivals(w.drive)
        query = await nl.understand(w.ctx, USERS_SENTENCE, now=NOW)
        assert isinstance(query, Query) and query.intent == "download"

        proposal = await nl.make_proposal(w.ctx, query, now=NOW)
        lines = "\n".join(nl.proposal_lines(proposal))
        # The plan says how the sentence was understood ...
        assert "理解为：下载到 NAS" in lines
        assert "进网盘时间晚于 2026-09-24 00:00（Asia/Shanghai）" in lines
        assert "按文件进网盘的时间（created_time）判断" in lines
        assert "大小不小于 1.0 GiB" in lines and "类型：视频" in lines
        assert f"下载到 NAS 的 {tmp_path / 'media' / 'PikPak'}" in lines
        # ... and what it matches: two videos today above 1GB, 5 GiB together.
        assert "命中 2 个文件，共 5.0 GiB" in lines
        assert "big.today.mkv" in lines and "huge.today.mp4" in lines
        assert "big.yesterday" not in lines and "small.today" not in lines
        assert proposal.plan_id is not None

        fetched = []

        async def fetch(url, target):
            # The files are gigabytes on paper: write a byte, report the size.
            fetched.append(url)
            target.write_bytes(b"x")
            return int(w.drive.items[url.rsplit("/", 1)[1]]["size"])

        # Confirmed: default downloader is "none", but 下载 means the NAS.
        report = await plans.apply(w.ctx, proposal.plan_id,
                                   deliver=outbound.make_deliver(w.ctx, fetch=fetch))
        assert (report.applied, report.failed) == (2, [])
        media = tmp_path / "media" / "PikPak"
        assert sorted(p.name for p in media.iterdir()) == ["big.today.mkv", "huge.today.mp4"]
        audit = await w.ctx.store.audit_entries(plan_id=proposal.plan_id)
        assert [entry["action"] for entry in audit] == ["outbound", "outbound"]
        assert all(entry["after"]["downloader"] == "local" for entry in audit)
        assert len(fetched) == 2

    async def test_the_explanation_is_stored_as_keys_and_follows_the_reader(self, world):
        w = world
        arrivals(w.drive)
        proposal = await nl.make_proposal(
            w.ctx, await nl.understand(w.ctx, USERS_SENTENCE, now=NOW), now=NOW)
        stored = (await plans.get(w.ctx, proposal.plan_id))["plan"]
        assert all("key" in note for note in stored.notes)
        set_language("en")
        english = [render_note(note) for note in stored.notes]
        assert "Understood as: download to the NAS" in english
        assert "Type: video (by extension or MIME type)" in english


class TestOtherIntents:
    async def test_list_changes_nothing(self, world):
        w = world
        arrivals(w.drive)
        query = await nl.understand(w.ctx, "今天离线下载的视频有哪些", now=NOW)
        proposal = await nl.make_proposal(w.ctx, query, now=NOW)
        assert proposal.kind == "listing" and proposal.plan_id is None
        assert [n.name for n in proposal.matches][:1] == ["big.today.mkv"]  # newest first
        assert proposal.count == 3

    async def test_classify_leaves_what_is_already_filed(self, world):
        w = world
        w.drive.add("/Inbox/a.mkv", size=1)
        w.drive.add("/Media/视频/b.mkv", size=1)
        query = await nl.understand(w.ctx, "把/Inbox里的文件按类型分类", now=NOW)
        proposal = await nl.make_proposal(w.ctx, query, now=NOW)
        await plans.apply(w.ctx, proposal.plan_id)
        assert w.exists("/Media/视频/a.mkv")
        query = await nl.understand(w.ctx, "按类型整理今天转存的文件", now=NOW)
        assert (await nl.make_proposal(w.ctx, query, now=NOW)).plan_id is None

    async def test_archive_by_arrival_month(self, world):
        w = world
        w.drive.add("/Media/old.mkv", size=1, created="2026-05-31T17:00:00+00:00")
        query = await nl.understand(w.ctx, "把三个月前的视频归档", now=NOW)
        proposal = await nl.make_proposal(w.ctx, query, now=NOW)
        await plans.apply(w.ctx, proposal.plan_id)
        assert w.exists("/Archive/2026-06/old.mkv")

    async def test_trash_is_the_recycle_bin_and_undoable(self, world):
        w = world
        w.drive.add("/Temp/old.bin", size=1, created="2026-07-01T00:00:00+00:00")
        query = await nl.understand(w.ctx, "删除/Temp里超过30天的文件", now=NOW)
        proposal = await nl.make_proposal(w.ctx, query, now=NOW)
        lines = "\n".join(nl.proposal_lines(proposal))
        assert "只放进回收站" in lines
        await plans.apply(w.ctx, proposal.plan_id)
        assert "delete_forever" not in w.drive.calls
        entry = (await w.ctx.store.audit_entries())[0]
        await plans.undo(w.ctx, entry["id"], apply_now=True)
        assert w.exists("/Temp/old.bin")

    async def test_a_question_rather_than_a_guess(self, world):
        for sentence in ("移动视频", "彻底删除/Temp", "把网盘里的大文件都删了"):
            result = await nl.understand(world.ctx, sentence, now=NOW)
            assert isinstance(result, Clarification)
            assert nl.clarification_text(result).endswith(("？", "。"))


class TestScheduledCommands:
    async def test_a_schedule_becomes_a_rule_in_the_rules_file(self, world, tmp_path):
        w = world
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text(
            "# 我的规则\nversion: 1\nrules:\n  - name: 已有\n    actions: [trash]\n"
            "    enabled: false\n",
            encoding="utf-8",
        )
        w.ctx.config.rules_file = rules_file
        query = await nl.understand(w.ctx, "每天凌晨把新文件按类型归档", now=NOW)
        proposal = await nl.make_proposal(w.ctx, query, now=NOW)
        assert proposal.kind == "rule" and proposal.plan_id is None
        lines = "\n".join(nl.proposal_lines(proposal))
        assert "按 0 3 * * *（Asia/Shanghai）定时运行" in lines

        nl.add_rules(w.ctx, proposal.rules, sentence="每天凌晨把新文件按类型归档")
        text = rules_file.read_text(encoding="utf-8")
        assert text.startswith("# 我的规则")  # the person's file is kept as it was
        loaded = load_rules(rules_file)
        added = [r for r in loaded.rules if r.name.startswith("nl-")]
        assert len(added) == 6 and all(r.schedule.cron == "0 3 * * *" for r in added)
        assert all(not r.schedule.apply for r in added)  # plans to confirm, not auto

    def test_a_broken_append_puts_the_file_back(self, tmp_path):
        rules_file = tmp_path / "rules.yaml"
        original = "version: 1\nrules:\n  - name: a\n    actions: [trash]\nextra: 1\n"
        rules_file.write_text(original)
        rule = parse_rules({"rules": [{"name": "a", "actions": ["trash"]}]}).rules[0]
        with pytest.raises(Exception, match="rules"):
            rulesfile.append_rules(rules_file, [rule])  # duplicate name → invalid
        assert rules_file.read_text() == original

    async def test_the_scheduler_runs_rule_schedules(self, world, tmp_path):
        from pikpak_wms.scheduler.runner import WmsScheduler

        w = world
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text("rules:\n  - name: nightly\n    schedule: {cron: '0 3 * * *'}\n"
                              "    scope: /Temp\n    actions: [trash]\n")
        w.ctx.config.rules_file = rules_file
        w.drive.add("/Temp/x.bin", size=1)
        scheduler = WmsScheduler(w.ctx)
        scheduler.start()
        try:
            assert ("rule:nightly", "Asia/Shanghai") in scheduler.scheduled()
            result = await scheduler.run_rule("nightly")
            assert result.plan_id and w.exists("/Temp/x.bin")  # planned, not applied
        finally:
            scheduler.shutdown()


# ---------------------------------------------------------------- backends


def answer(**query) -> dict:
    base = {"intent": "list", "scope": {"path": "/", "recursive": True},
            "filters": {"created_after": None, "created_before": None, "min_size": None,
                        "max_size": None, "kinds": [], "extensions": [], "name_contains": [],
                        "name_regex": None},
            "action_args": {"dest": None, "template": None}, "schedule": None,
            "needs_clarification": None}
    for key, value in query.items():
        if isinstance(value, dict):
            base[key].update(value)
        else:
            base[key] = value
    return base


class FakeAnthropic:
    def __init__(self, text: str, stop_reason: str = "end_turn"):
        self.calls: list[dict] = []
        self._text, self._stop = text, stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason=self._stop,
                               content=[SimpleNamespace(type="text", text=self._text)])


class TestModelBackends:
    async def test_claude_sends_only_the_sentence_schema_and_date(self):
        fake = FakeAnthropic(json.dumps(answer(intent="download",
                                               filters={"name_contains": ["速度与激情8"]})))
        translator = ClaudeTranslator(client=fake)
        result = await translator.translate("把速度与激情8下载下来", NOW, SHANGHAI)
        assert result.intent == "download" and result.filters.name_contains == ["速度与激情8"]
        (call,) = fake.calls
        assert call["model"] == "claude-opus-5"
        assert call["output_config"]["format"] == {"type": "json_schema", "schema": wire_schema()}
        assert call["fallbacks"] == "default"
        assert call["betas"] == ["server-side-fallback-2026-07-01"]
        (message,) = call["messages"]
        assert message == {"role": "user", "content":
                           "Now: 2026-09-24T12:00:00+08:00 (time zone Asia/Shanghai).\n"
                           "Instruction: 把速度与激情8下载下来"}

    async def test_claude_refusal_and_garbage(self):
        refused = ClaudeTranslator(client=FakeAnthropic("", stop_reason="refusal"))
        assert isinstance(await refused.translate("x", NOW, SHANGHAI), Clarification)
        garbage = ClaudeTranslator(client=FakeAnthropic("{not json"))
        with pytest.raises(TranslationError):
            await garbage.translate("x", NOW, SHANGHAI)

    async def test_missing_credentials_are_a_translation_error(self):
        class NoKey:
            beta = SimpleNamespace(messages=SimpleNamespace(create=None))

            async def create(self, **_kwargs):
                raise TypeError("Could not resolve authentication method")

        client = NoKey()
        client.beta.messages.create = client.create
        with pytest.raises(TranslationError, match="authentication"):
            await ClaudeTranslator(client=client).translate("x", NOW, SHANGHAI)

    async def test_claude_model_questions_come_back_as_questions(self):
        fake = FakeAnthropic(json.dumps(answer(intent="move", needs_clarification="移到哪里？")))
        result = await ClaudeTranslator(client=fake).translate("移动视频", NOW, SHANGHAI)
        assert isinstance(result, Clarification) and result.question == "移到哪里？"

    async def test_other_models_get_no_fallback_parameters(self):
        fake = FakeAnthropic(json.dumps(answer()))
        await ClaudeTranslator(client=fake, model="claude-haiku-4-5", effort="").translate(
            "列出视频", NOW, SHANGHAI)
        assert "fallbacks" not in fake.calls[0] and "effort" not in fake.calls[0]["output_config"]

    async def test_ollama_uses_the_schema_as_format(self):
        sent = []

        async def post(url, body):
            sent.append((url, body))
            return {"message": {"content": json.dumps(answer(intent="list",
                                                             filters={"kinds": ["audio"]}))}}

        translator = OllamaTranslator(url="http://ollama:11434/", post=post)
        result = await translator.translate("找一下周杰伦的歌", NOW, SHANGHAI)
        assert result.filters.kinds == ["audio"]
        url, body = sent[0]
        assert url == "http://ollama:11434/api/chat"
        assert body["format"] == wire_schema() and body["stream"] is False
        assert body["model"] == "qwen2.5:3b"

    def test_the_wire_schema_only_uses_what_structured_outputs_accept(self):
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node.get("additionalProperties") is False
                    assert set(node["required"]) == set(node["properties"])
                for key in ("minimum", "maximum", "minLength", "maxLength", "pattern"):
                    assert key not in node
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(wire_schema())
        assert from_wire(answer()).intent == "list"


class TestChain:
    async def test_rules_first_then_the_model(self):
        class Model:
            name = "model"
            called = 0

            async def translate(self, text, now, tz):
                Model.called += 1
                return Query(intent="list")

        chain = Chain([RulesTranslator(), Model()])
        await chain.translate(USERS_SENTENCE, NOW, SHANGHAI)
        assert Model.called == 0 and chain.last_used == "rules"
        await chain.translate("找一下周杰伦的歌", NOW, SHANGHAI)
        assert Model.called == 1 and chain.last_used == "model"

    async def test_a_dead_model_does_not_hide_the_rules_answer_but_is_reported(self):
        class Dead:
            name = "dead"

            async def translate(self, text, now, tz):
                raise TranslationError("down")

        chain = Chain([RulesTranslator(), Dead()])
        assert isinstance(await chain.translate(USERS_SENTENCE, NOW, SHANGHAI), Query)
        with pytest.raises(TranslationError):
            await chain.translate("找一下周杰伦的歌", NOW, SHANGHAI)

    def test_settings(self, monkeypatch):
        assert from_environment().name == "rules"
        monkeypatch.setenv("NL_BACKEND", "claude")
        monkeypatch.setenv("NL_FALLBACK", "ollama")
        assert from_environment().name == "rules+claude+ollama"
        assert build("claude", "none", rules_first=False).name == "claude"
        monkeypatch.setenv("NL_BACKEND", "gpt")
        with pytest.raises(Exception, match="NL_BACKEND"):
            from_environment()


# --------------------------------------------------------------------- CLI


def test_wms_do(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config = tmp_path / "wms.yaml"
    config.write_text("ratelimit: {requests_per_second: 100000, burst: 100000}\n")
    monkeypatch.setenv("WMS_CONFIG", str(config))
    drive = FakeDrive()
    drive.add("/Temp/old.bin", size=1, created="2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(cli.state, "provider_factory", lambda config: provider_for(drive))
    runner = CliRunner()
    set_language(None)
    monkeypatch.setenv("WMS_LANG", "zh")
    result = runner.invoke(cli.app, ["do", "删除/Temp里超过30天的文件"])
    assert result.exit_code == 0, result.output
    assert "理解为：放进回收站" in result.output and "wms apply 1" in result.output
    result = runner.invoke(cli.app, ["do", "移动视频"])
    assert result.exit_code == 1 and "移到哪里" in result.output
    result = runner.invoke(cli.app, ["do", "删除/Temp里超过30天的文件", "--apply"])
    assert "执行 1" in result.output
