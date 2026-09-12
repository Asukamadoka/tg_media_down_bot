"""Configuration loading, precedence and validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tgmd.config import ConfigError, load_config

MINIMAL_YAML = """
telegram:
  api_id: 111
  api_hash: hash-from-file
  bot_token: token-from-file
access:
  admin_user_ids: [42]
"""


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class TestLoading:
    def test_values_come_from_the_file(self, tmp_path):
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.telegram.api_id == 111
        assert config.telegram.api_hash == "hash-from-file"
        assert config.access.admin_user_ids == [42]

    def test_environment_overrides_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TG_API_HASH", "hash-from-env")
        monkeypatch.setenv("TG_API_ID", "999")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.telegram.api_id == 999
        assert config.telegram.api_hash == "hash-from-env"

    def test_missing_file_is_an_error_when_named(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "absent.yaml")

    def test_no_file_at_all_still_loads_from_env(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TG_API_ID", "7")
        monkeypatch.setenv("TG_API_HASH", "h")
        monkeypatch.setenv("TG_BOT_TOKEN", "t")
        config = load_config()
        assert config.telegram.api_id == 7

    def test_non_mapping_file_is_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("- just\n- a list\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="mapping"):
            load_config(path)

    def test_non_numeric_env_int_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TG_API_ID", "not-a-number")
        with pytest.raises(ConfigError, match="must be an integer"):
            load_config(write_config(tmp_path, MINIMAL_YAML))

    def test_admin_ids_parse_from_a_comma_list(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ADMIN_USER_IDS", "1, 2 ,3")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.access.admin_user_ids == [1, 2, 3]

    def test_negative_cache_chat_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CACHE_CHAT_ID", "-1001234567890")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.delivery.cache_chat_id == -1001234567890


class TestPikPakActivation:
    def test_credentials_turn_pikpak_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PIKPAK_USERNAME", "a@b.c")
        monkeypatch.setenv("PIKPAK_PASSWORD", "secret")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.pikpak.enabled
        assert config.pikpak.configured

    def test_enabled_without_credentials_is_not_configured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PIKPAK_ENABLED", "true")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.pikpak.enabled
        assert not config.pikpak.configured


class TestValidation:
    def test_missing_credentials_are_fatal(self, tmp_path):
        config = load_config(write_config(tmp_path, "telegram:\n  api_id: 1\n"))
        with pytest.raises(ConfigError, match="missing required settings"):
            config.validate()

    def test_unknown_mode_is_fatal(self, tmp_path):
        config = load_config(
            write_config(tmp_path, MINIMAL_YAML + "\ndelivery:\n  default_mode: cloud\n")
        )
        with pytest.raises(ConfigError, match="default_mode"):
            config.validate()

    def test_unknown_template_field_is_fatal(self, tmp_path):
        config = load_config(
            write_config(
                tmp_path,
                MINIMAL_YAML + '\ndownload:\n  filename_template: "{bogus}/{name}"\n',
            )
        )
        with pytest.raises(ConfigError, match="unknown fields"):
            config.validate()

    def test_valid_template_fields_pass(self, tmp_path):
        config = load_config(
            write_config(
                tmp_path,
                MINIMAL_YAML
                + '\ndownload:\n  filename_template: "{date}/{chat}/{stem}{ext}"\n',
            )
        )
        config.validate()

    def test_zero_workers_is_fatal(self, tmp_path):
        config = load_config(
            write_config(tmp_path, MINIMAL_YAML + "\ndownload:\n  concurrent: 0\n")
        )
        with pytest.raises(ConfigError, match="at least 1"):
            config.validate()

    def test_http_without_a_public_url_is_fatal(self, tmp_path):
        config = load_config(
            write_config(tmp_path, MINIMAL_YAML + "\nhttp:\n  enabled: true\n")
        )
        with pytest.raises(ConfigError, match="public_base_url"):
            config.validate()

    def test_no_allowed_users_is_a_warning(self, tmp_path):
        body = MINIMAL_YAML.replace("admin_user_ids: [42]", "admin_user_ids: []")
        config = load_config(write_config(tmp_path, body))
        warnings = config.validate()
        assert any("refuse every request" in warning for warning in warnings)

    def test_open_access_is_a_warning(self, tmp_path):
        config = load_config(
            write_config(tmp_path, MINIMAL_YAML + "\naccess:\n  allow_all_users: true\n")
        )
        assert any("allow_all_users" in warning for warning in config.validate())

    def test_pikpak_without_http_server_is_a_warning(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PIKPAK_USERNAME", "a@b.c")
        monkeypatch.setenv("PIKPAK_PASSWORD", "secret")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert any("HTTP file server" in warning for warning in config.validate())


class TestDerivedValues:
    def test_upload_limit_in_bytes(self, tmp_path):
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.delivery.max_upload_bytes == 2000 * 1024 * 1024

    def test_access_checks(self, tmp_path):
        body = MINIMAL_YAML + "\naccess:\n  admin_user_ids: [42]\n  allowed_user_ids: [7]\n"
        config = load_config(write_config(tmp_path, body))
        assert config.access.is_admin(42)
        assert not config.access.is_admin(7)
        assert config.access.is_allowed(7)
        assert not config.access.is_allowed(99)

    def test_allow_all_opens_access(self, tmp_path):
        config = load_config(
            write_config(tmp_path, MINIMAL_YAML + "\naccess:\n  allow_all_users: true\n")
        )
        assert config.access.is_allowed(99999)

    def test_http_base_url_loses_its_trailing_slash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HTTP_ENABLED", "true")
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com/")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.http.base_url == "https://example.com"
        assert config.http.usable

    def test_directories_are_created(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        config.ensure_directories()
        assert (tmp_path / "downloads").is_dir()
        assert (tmp_path / "data").is_dir()
        assert (tmp_path / "sessions").is_dir()
