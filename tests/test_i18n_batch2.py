"""Stage 4, i18n batch 2: errors, the verify report and the login Mini App.

Errors people read carry a catalogue key: ``str(exc)`` stays English for the
log and the database, and :func:`tgmd.i18n.describe` gives the reader's
language. The verify report keeps stable English check ids while showing
translated labels, aligned by display width. The Mini App page follows the
bot's language, with the script's strings handed over as JSON.
"""

from __future__ import annotations

import ast
import importlib
import json
import pkgutil
import re
from pathlib import Path

import pytest

import tgmd
from tgmd import i18n
from tgmd.bootstrap import ClaimError
from tgmd.config import HttpConfig, PikPakConfig
from tgmd.delivery import DeliveryError, TooLargeToUpload
from tgmd.downloader import DownloadError
from tgmd.i18n import CATALOG, Explained, describe
from tgmd.identity import BotTokenError, Check, Report, parse_bot_token
from tgmd.links import LinkError, extract_links, parse_message_link
from tgmd.pikpak import PikPakError
from tgmd.portal import PikPakLoginPortal, render_miniapp
from tgmd.resolver import ResolveError
from tgmd.tasks import QueueFull
from tgmd.utils import display_width

PACKAGE = Path(tgmd.__file__).parent


@pytest.fixture
def chinese():
    previous = i18n.language()
    i18n.set_language("zh")
    yield
    i18n.set_language(previous)


def explained_classes() -> dict[str, type]:
    for module in pkgutil.iter_modules([str(PACKAGE)]):
        importlib.import_module(f"tgmd.{module.name}")

    def walk(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from walk(sub)

    return {cls.__name__: cls for cls in walk(Explained) if cls.__module__.startswith("tgmd.")}


def calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            yield name, node


def sources():
    for path in sorted(PACKAGE.glob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


class TestEveryUserFacingErrorHasAKey:
    def test_the_expected_classes_are_explained(self):
        names = set(explained_classes())
        assert {"ResolveError", "PikPakError", "LinkError", "DeliveryError", "TooLargeToUpload",
                "DownloadError", "BotTokenError", "ClaimError", "QueueFull",
                "SessionError"} <= names

    def test_every_raise_uses_a_catalogue_key_in_both_languages(self):
        classes = set(explained_classes())
        checked = 0
        for path, tree in sources():
            for name, node in calls(tree):
                if name not in classes:
                    continue
                keys = [kw for kw in node.keywords if kw.arg == "key"]
                assert keys, f"{path.name}:{node.lineno} raises {name} without a key"
                assert not node.args, f"{path.name}:{node.lineno} also passes a message"
                key = keys[0].value
                assert isinstance(key, ast.Constant), f"{path.name}:{node.lineno}"
                for lang in i18n.LANGUAGES:
                    assert key.value in CATALOG[lang], (lang, key.value)
                checked += 1
        # 68 raise sites when this was written: the ~76 messages CC_BRIEF §6
        # counted, less the ones stage 1 removed and duplicates sharing a key.
        assert checked >= 68

    def test_every_literal_catalogue_key_exists(self):
        missing = []
        for path, tree in sources():
            for name, node in calls(tree):
                if name != "t" or not node.args:
                    continue
                first = node.args[0]
                literal = isinstance(first, ast.Constant) and isinstance(first.value, str)
                if literal and first.value not in CATALOG["en"]:
                    missing.append(f"{path.name}:{node.lineno} {first.value}")
        assert missing == []


class TestLogsStayEnglishReadersGetTheirLanguage:
    @pytest.mark.parametrize(
        ("error", "english", "zh"),
        [
            (ResolveError(key="err.resolve.no_username", name="nosuch"),
             "no chat called @nosuch exists", "不存在名为 @nosuch 的聊天"),
            (PikPakError(key="err.pikpak.share_status", status="EXPIRED"),
             "the share link is not usable (status EXPIRED); it may be expired or need a password",
             "分享链接不可用（状态 EXPIRED），可能已过期或需要提取码"),
            (DownloadError(key="err.download.disk", needed=2048.0, free=100.4),
             "not enough free disk space: 2048 MiB needed, 100 MiB free",
             "磁盘空间不足：需要 2048 MiB，剩余 100 MiB"),
            (TooLargeToUpload(key="err.delivery.too_large", size="3.0 GiB", limit="2.0 GiB"),
             "3.0 GiB is over the 2.0 GiB a bot can upload",
             "3.0 GiB 超过了机器人可上传的 2.0 GiB"),
            (ClaimError(key="err.claim.wrong"), "that claim code is wrong", "认领码不对"),
            (QueueFull(key="job.queue_full", limit=3),
             i18n.t("job.queue_full", lang="en", limit=3),
             i18n.t("job.queue_full", lang="zh", limit=3)),
        ],
    )
    def test_str_is_english_and_describe_translates(self, chinese, error, english, zh):
        assert str(error) == english
        assert describe(error, "en") == english
        assert describe(error) == zh != english

    def test_the_chinese_text(self, chinese):
        error = ResolveError(key="err.resolve.no_username", name="nosuch")
        assert describe(error) == "不存在名为 @nosuch 的聊天"
        disk = DownloadError(key="err.download.disk", needed=2048.0, free=100.4)
        assert describe(disk) == "磁盘空间不足：需要 2048 MiB，剩余 100 MiB"

    def test_a_wrapped_error_is_translated_inside_the_wrapper(self, chinese):
        inner = PikPakError(key="err.pikpak.share_empty")
        outer = DeliveryError(key="err.passthrough", error=inner)
        assert str(outer) == "the share link contains no files"
        assert describe(outer) == "分享链接里没有文件"
        attempts = DownloadError(key="err.download.attempts", attempts=3, error=inner)
        assert str(attempts) == "download failed after 3 attempts: the share link contains no files"
        assert describe(attempts) == "重试 3 次后下载仍然失败：分享链接里没有文件"

    def test_a_foreign_error_inside_is_passed_through(self, chinese):
        error = PikPakError(key="err.pikpak.login_failed", error=RuntimeError("invalid_grant"))
        assert describe(error) == "PikPak 登录失败：invalid_grant"

    def test_plain_exceptions_are_shown_as_they_are(self):
        assert describe(ValueError("boom")) == "boom"

    def test_link_errors_reach_the_reply_translated(self, chinese):
        with pytest.raises(LinkError) as caught:
            parse_message_link("https://t.me/c/123")
        assert str(caught.value) == "a t.me/c link needs both a chat id and a message id"
        bundle = extract_links("https://t.me/c/123")
        assert bundle.errors and "t.me/c 链接需要同时带有聊天 ID 和消息 ID" in bundle.errors[0]

    def test_bot_token_errors(self, chinese):
        with pytest.raises(BotTokenError) as caught:
            parse_bot_token("")
        assert str(caught.value) == "the bot token is empty; get one from @BotFather"
        assert describe(caught.value) == "机器人令牌为空，请向 @BotFather 申请一个"
        with pytest.raises(BotTokenError) as caught:
            parse_bot_token("abc:" + "x" * 35)
        assert describe(caught.value) == "冒号前面应该是数字形式的机器人 ID，实际是 'abc'"


class TestVerifyReport:
    def report(self) -> Report:
        report = Report()
        report.add(Check.ok("bot token", "fine"))
        report.add(Check.warn("public reachability", "slow"))
        report.add(Check.fail("pikpak account", "no"))
        return report

    def test_ids_stay_english_labels_translate(self, chinese):
        report = self.report()
        assert report.checks[0].name == "bot token"
        text = report.render_text()
        assert "机器人令牌" in text and "公网可达" in text and "PikPak 账号" in text
        assert text.endswith("1 项检查失败——按当前配置机器人无法工作")
        assert "<b>机器人令牌</b>" in report.render_html()

    @pytest.mark.parametrize("lang", ["en", "zh"])
    def test_details_line_up_by_display_width(self, lang):
        previous = i18n.language()
        i18n.set_language(lang)
        try:
            lines = self.report().render_text().splitlines()[:3]
        finally:
            i18n.set_language(previous)
        columns = {display_width(line[: line.rindex("  ") + 2]) for line in lines}
        assert len(columns) == 1, lines

    def test_display_width(self):
        assert display_width("abc") == 3
        assert display_width("机器人") == 6
        assert display_width("é") == 1  # a combining accent takes no column

    def test_the_cli_speaks_the_configured_language(self, monkeypatch, capsys, tmp_path):
        from tgmd import verify

        monkeypatch.setenv("TGMD_LANG", "zh")
        previous = i18n.language()
        try:
            status = verify.main([str(tmp_path / "missing.yaml")])
        finally:
            i18n.set_language(previous)
        assert status == 2
        assert capsys.readouterr().err.startswith("配置错误：")


class TestMiniApp:
    def test_english_by_default(self):
        page = render_miniapp()
        assert '<html lang="en">' in page
        assert "Connect your PikPak account" in page

    def test_chinese_page(self, chinese):
        page = render_miniapp()
        assert '<html lang="zh">' in page
        assert "<h1>连接你的 PikPak 账号</h1>" in page
        assert "不是由 PikPak 运营的" in page
        assert "<code>/pikpak logout</code>" in page  # the command itself is not translated
        strings = json.loads(re.search(
            r'<script type="application/json" id="strings">(.*?)</script>', page, re.S
        ).group(1))
        assert strings["connecting"] == "连接中…"
        assert strings["unreachable"] == "连不上机器人：{error}"

    def test_no_visible_english_is_left_in_the_chinese_page(self, chinese):
        page = render_miniapp()
        body = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", page, flags=re.S)
        words = set(re.findall(r"[A-Za-z]{4,}", body))
        assert words <= {"PikPak", "Telegram", "pikpak", "logout"}, words

    def test_a_translation_cannot_close_the_script(self, chinese, monkeypatch):
        monkeypatch.setitem(CATALOG["zh"], "portal.js.failed", "</script><script>alert(1)")
        page = render_miniapp()
        assert "</script><script>alert(1)" not in page
        assert "<\\/script><script>alert(1)" in page

    def test_the_markup_escapes_translations(self, chinese, monkeypatch):
        monkeypatch.setitem(CATALOG["zh"], "portal.heading", "<b>x</b>")
        assert "<h1>&lt;b&gt;x&lt;/b&gt;</h1>" in render_miniapp()

    def test_unavailable_reasons_are_translated(self, chinese):
        http = HttpConfig(enabled=True, public_base_url="http://nas.local:8080")
        portal = PikPakLoginPortal(object(), PikPakConfig(allow_user_login=False), http,
                                   bot_token="1:x", is_allowed=lambda _u: True)
        assert "运营者关闭了按用户登录 PikPak" in (portal.unavailable_reason() or "")
