"""Formatting and filename helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tgmd.utils import (
    build_relative_path,
    human_duration,
    human_rate,
    human_size,
    parse_bool,
    parse_id_list,
    progress_bar,
    sanitize_component,
    split_extension,
    truncate,
    unique_path,
)


class TestHumanSize:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "0 B"),
            (512, "512 B"),
            (1024, "1.0 KiB"),
            (1536, "1.5 KiB"),
            (1024**2, "1.0 MiB"),
            (int(2.5 * 1024**3), "2.5 GiB"),
        ],
    )
    def test_formats(self, value, expected):
        assert human_size(value) == expected

    def test_unknown(self):
        assert human_size(None) == "unknown size"


class TestHumanDuration:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, "0s"), (45, "45s"), (67, "1m07s"), (3700, "1h01m"), (None, "?")],
    )
    def test_formats(self, value, expected):
        assert human_duration(value) == expected


class TestMisc:
    def test_rate_of_zero_is_a_dash(self):
        assert human_rate(0) == "—"

    def test_rate(self):
        assert human_rate(1024) == "1.0 KiB/s"

    def test_progress_bar_endpoints(self):
        assert progress_bar(0, width=4) == "░░░░"
        assert progress_bar(1, width=4) == "████"
        assert len(progress_bar(0.5, width=10)) == 10

    def test_progress_bar_clamps(self):
        assert progress_bar(-1, width=3) == "░░░"
        assert progress_bar(9, width=3) == "███"

    def test_truncate(self):
        assert truncate("abcdef", 10) == "abcdef"
        assert truncate("abcdef", 4).endswith("…")
        assert len(truncate("abcdef", 4)) <= 4


class TestSanitizeComponent:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("plain.mp4", "plain.mp4"),
            ("a/b.mp4", "a_b.mp4"),
            ("a\\b.mp4", "a_b.mp4"),
            ('quote".mp4', "quote_.mp4"),
            ("colon:name.mp4", "colon_name.mp4"),
        ],
    )
    def test_illegal_characters(self, raw, expected):
        assert sanitize_component(raw) == expected

    def test_whitespace_is_collapsed(self):
        assert sanitize_component("a   b.mp4") == "a b.mp4"

    def test_empty_falls_back(self):
        assert sanitize_component("") == "unnamed"
        assert sanitize_component("  ..  ") == "unnamed"
        assert sanitize_component("", fallback="x.bin") == "x.bin"

    def test_windows_reserved_names_are_prefixed(self):
        assert sanitize_component("CON.txt").startswith("_")
        assert sanitize_component("NUL") == "_NUL"

    def test_long_names_keep_their_extension(self):
        name = "a" * 300 + ".mp4"
        result = sanitize_component(name)
        assert len(result) <= 120
        assert result.endswith(".mp4")

    def test_control_characters_are_removed(self):
        assert sanitize_component("a\x00b.mp4") == "a_b.mp4"


class TestSplitExtension:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("video.mp4", ("video", ".mp4")),
            ("archive.tar.gz", ("archive.tar", ".gz")),
            ("noext", ("noext", "")),
        ],
    )
    def test_split(self, raw, expected):
        assert split_extension(raw) == expected

    def test_long_suffix_is_not_an_extension(self):
        assert split_extension("name.thisisnotanextension") == (
            "name.thisisnotanextension",
            "",
        )


class TestBuildRelativePath:
    def test_default_template(self):
        path = build_relative_path(
            "{chat}/{message_id}_{name}",
            chat="My Channel",
            chat_id=-1001,
            message_id=42,
            name="clip.mp4",
        )
        assert path == Path("My Channel/42_clip.mp4")

    def test_subdirectories_from_template(self):
        path = build_relative_path(
            "{date}/{chat}/{stem}{ext}",
            chat="chan",
            chat_id=1,
            message_id=7,
            name="a.mkv",
            when=datetime(2026, 9, 12, tzinfo=UTC),
        )
        assert path == Path("2026-09-12/chan/a.mkv")

    def test_extension_is_restored_when_template_drops_it(self):
        path = build_relative_path(
            "{chat}/{message_id}_{stem}",
            chat="chan",
            chat_id=1,
            message_id=7,
            name="movie.mp4",
        )
        assert path.name == "7_movie.mp4"

    def test_traversal_in_values_cannot_escape(self):
        path = build_relative_path(
            "{chat}/{name}",
            chat="../../etc",
            chat_id=1,
            message_id=1,
            name="../passwd",
        )
        assert not path.is_absolute()
        assert ".." not in path.parts

    def test_slashes_in_a_chat_title_do_not_create_directories(self):
        path = build_relative_path(
            "{chat}/{name}",
            chat="a/b/c",
            chat_id=1,
            message_id=1,
            name="x.mp4",
        )
        assert path == Path("a_b_c/x.mp4")

    def test_unknown_field_falls_back_to_a_safe_layout(self):
        path = build_relative_path(
            "{nope}/{name}",
            chat="chan",
            chat_id=1,
            message_id=3,
            name="x.mp4",
        )
        assert path == Path("chan/3_x.mp4")

    def test_topic_id_defaults_to_zero(self):
        path = build_relative_path(
            "{topic_id}/{name}", chat="c", chat_id=1, message_id=1, name="x.bin"
        )
        assert path == Path("0/x.bin")


class TestUniquePath:
    def test_returns_original_when_free(self, tmp_path):
        target = tmp_path / "a.mp4"
        assert unique_path(target) == target

    def test_appends_a_counter(self, tmp_path):
        target = tmp_path / "a.mp4"
        target.touch()
        assert unique_path(target).name == "a (1).mp4"

    def test_counts_up_past_existing_variants(self, tmp_path):
        (tmp_path / "a.mp4").touch()
        (tmp_path / "a (1).mp4").touch()
        assert unique_path(tmp_path / "a.mp4").name == "a (2).mp4"


class TestParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1, 2;3", [1, 2, 3]),
            ("1 2\n3", [1, 2, 3]),
            ([1, "2", 2], [1, 2]),
            ("", []),
            (None, []),
            ("1,junk,2", [1, 2]),
            ("-100123", [-100123]),
        ],
    )
    def test_parse_id_list(self, raw, expected):
        assert parse_id_list(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("true", True),
            ("YES", True),
            ("1", True),
            ("on", True),
            ("false", False),
            ("0", False),
            ("nonsense", False),
            (True, True),
        ],
    )
    def test_parse_bool(self, raw, expected):
        assert parse_bool(raw) is expected

    def test_parse_bool_default_for_empty(self):
        assert parse_bool(None, True) is True
        assert parse_bool("", True) is True
