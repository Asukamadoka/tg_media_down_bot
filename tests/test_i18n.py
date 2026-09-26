"""The message catalogue: fallbacks, formatting, and the rules it must keep."""

from __future__ import annotations

import pytest

from tgmd import botconfig, i18n


@pytest.fixture(autouse=True)
def restore_language():
    """Never let one test's language leak into the next."""
    previous = i18n.language()
    yield
    i18n.set_language(previous)


class TestNormalize:
    @pytest.mark.parametrize(
        "value",
        ["zh", "ZH", "zh-CN", "zh_CN", "zh-Hans", "chinese", "cn", "中文"],
    )
    def test_chinese_spellings(self, value):
        assert i18n.normalize(value) == "zh"

    @pytest.mark.parametrize("value", ["en", "EN", "en_US.UTF-8", "english"])
    def test_english_spellings(self, value):
        assert i18n.normalize(value) == "en"

    @pytest.mark.parametrize("value", [None, "", "   ", "klingon", "fr"])
    def test_anything_else_falls_back(self, value):
        """A typo in an environment variable must not stop the bot starting."""
        assert i18n.normalize(value) == i18n.DEFAULT_LANGUAGE


class TestCatalogue:
    def test_every_language_has_the_same_keys(self):
        english = set(i18n.CATALOG["en"])
        for language in i18n.LANGUAGES:
            assert set(i18n.CATALOG[language]) == english, language

    def test_no_entry_is_empty(self):
        for language, table in i18n.CATALOG.items():
            for key, value in table.items():
                assert value.strip(), f"{language}:{key}"

    def test_placeholders_match_across_languages(self):
        """A translation that drops a placeholder would silently lose data."""
        import string

        def fields(text):
            return {
                name
                for _, name, _, _ in string.Formatter().parse(text)
                if name
            }

        for key, english in i18n.CATALOG["en"].items():
            expected = fields(english)
            for language in i18n.LANGUAGES:
                assert fields(i18n.CATALOG[language][key]) == expected, (
                    f"{language}:{key}"
                )


class TestLookup:
    def test_it_translates(self):
        i18n.set_language("zh")
        assert i18n.t("status.empty") == i18n.CATALOG["zh"]["status.empty"]

    def test_it_formats(self):
        i18n.set_language("en")
        assert i18n.t("mode.set", choice="local") == "Mode set to <b>local</b>."

    def test_an_explicit_language_wins(self):
        i18n.set_language("zh")
        assert i18n.t("status.empty", lang="en") == "Nothing in your queue."

    def test_a_missing_key_returns_the_key(self):
        assert i18n.t("no.such.key") == "no.such.key"

    def test_a_missing_translation_falls_back_to_english(self, monkeypatch):
        monkeypatch.setitem(i18n.CATALOG, "zh", {})
        i18n.set_language("zh")
        assert i18n.t("status.empty") == "Nothing in your queue."

    def test_bad_formatting_returns_the_unformatted_text(self):
        """A wrong kwarg must not raise in the middle of answering someone."""
        i18n.set_language("en")
        assert "{choice}" in i18n.t("mode.set", wrong="x")


class TestStoredValuesAreNotTranslated:
    """Job states and modes are SQLite values; only their display changes."""

    def test_display_state_translates_known_states(self):
        assert i18n.display_state("done", lang="zh") != "done"

    def test_display_state_passes_unknown_values_through(self):
        assert i18n.display_state("something-new", lang="zh") == "something-new"

    def test_display_mode_passes_unknown_values_through(self):
        assert i18n.display_mode("something-new", lang="zh") == "something-new"


class TestCommandNamesStayEnglish:
    def test_names_are_untranslated_and_menu_matches(self):
        for language in i18n.LANGUAGES:
            names = [name for name, _ in botconfig.commands(language)]
            assert names == list(botconfig.COMMAND_NAMES)

    def test_every_language_has_a_description_for_every_command(self):
        for language in i18n.LANGUAGES:
            for name, description in botconfig.commands(language):
                assert description and description != f"menu.{name}"

    def test_translated_help_keeps_the_english_arguments(self):
        """`/mode local` is parsed literally, so help must not translate it."""
        from tgmd.config import MODES

        for language in i18n.LANGUAGES:
            body = i18n.t("help.body", lang=language)
            for mode in MODES:
                assert mode in body, f"{language} lost /mode {mode}"
            current = i18n.t("mode.current", lang=language, current="x")
            for mode in MODES:
                assert f"/mode {mode}" in current, f"{language} lost /mode {mode}"


class TestEnvironment:
    def test_it_reads_tgmd_lang(self, monkeypatch):
        monkeypatch.setenv("TGMD_LANG", "zh")
        assert i18n.language_from_environment() == "zh"

    def test_posix_lang_is_ignored(self, monkeypatch):
        """Images set LANG=C.UTF-8 for unrelated reasons."""
        monkeypatch.delenv("TGMD_LANG", raising=False)
        monkeypatch.delenv("BOT_LANG", raising=False)
        monkeypatch.setenv("LANG", "zh_CN.UTF-8")
        assert i18n.language_from_environment() is None
