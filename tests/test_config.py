"""Configuration loading, precedence and validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tgmd.config import ConfigError, detect_platform_base_url, load_config

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

    def test_http_without_a_public_url_is_a_warning_not_a_crash(self, tmp_path):
        # It used to raise, and the container restarted forever. No public
        # address yet is a normal state for a NAS, so only the one capability
        # that needs it is switched off.
        config = load_config(
            write_config(tmp_path, MINIMAL_YAML + "\nhttp:\n  enabled: true\n")
        )
        warnings = config.validate()
        assert any("Telegram-to-PikPak transfers are off" in w for w in warnings)
        assert not config.http.usable

    def test_the_http_problem_is_reported_once(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PIKPAK_USERNAME", "a@b.c")
        monkeypatch.setenv("PIKPAK_PASSWORD", "secret")
        config = load_config(
            write_config(tmp_path, MINIMAL_YAML + "\nhttp:\n  enabled: true\n")
        )
        about_http = [w for w in config.validate() if "Telegram-to-PikPak" in w]
        assert len(about_http) == 1

    def test_runtime_state_is_not_reported_here(self, tmp_path):
        # Whether there is an admin or a reading account is decided by /claim
        # and /setup telegram, which store it in the database. validate() runs
        # before that is read, so a warning from it would be wrong every time
        # after either was done.
        body = MINIMAL_YAML.replace("admin_user_ids: [42]", "admin_user_ids: []")
        config = load_config(write_config(tmp_path, body))
        assert not config.telegram.user_session
        warnings = " ".join(config.validate())
        assert "admin" not in warnings
        assert "user session" not in warnings

    def test_the_retired_login_link_ttl_still_parses(self, tmp_path, monkeypatch):
        # The link it timed is gone; an old compose that sets it must still start.
        monkeypatch.setenv("PIKPAK_LOGIN_LINK_TTL", "300")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        config.validate()
        assert config.pikpak.login_link_ttl == 300

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


class TestPlatformDetection:
    """One-click deploys rely on reading the host's own environment."""

    def test_nothing_detected_by_default(self):
        assert detect_platform_base_url({}) is None

    def test_render_exports_a_full_url(self):
        assert (
            detect_platform_base_url({"RENDER_EXTERNAL_URL": "https://a.onrender.com"})
            == "https://a.onrender.com"
        )

    def test_a_trailing_slash_is_trimmed(self):
        assert (
            detect_platform_base_url({"RENDER_EXTERNAL_URL": "https://a.onrender.com/"})
            == "https://a.onrender.com"
        )

    @pytest.mark.parametrize(
        "name", ["KOYEB_PUBLIC_DOMAIN", "RAILWAY_PUBLIC_DOMAIN", "SPACE_HOST"]
    )
    def test_bare_domains_get_an_https_scheme(self, name):
        assert detect_platform_base_url({name: "app.example.com"}) == (
            "https://app.example.com"
        )

    def test_a_domain_that_already_has_a_scheme_is_not_doubled(self):
        assert (
            detect_platform_base_url({"KOYEB_PUBLIC_DOMAIN": "https://app.example.com"})
            == "https://app.example.com"
        )

    def test_fly_builds_its_conventional_hostname(self):
        assert detect_platform_base_url({"FLY_APP_NAME": "mybot"}) == (
            "https://mybot.fly.dev"
        )

    def test_render_wins_over_the_others(self):
        assert detect_platform_base_url(
            {
                "RENDER_EXTERNAL_URL": "https://render.example",
                "KOYEB_PUBLIC_DOMAIN": "koyeb.example",
                "FLY_APP_NAME": "fly",
            }
        ) == "https://render.example"

    def test_blank_values_are_ignored(self):
        assert detect_platform_base_url({"RENDER_EXTERNAL_URL": "  "}) is None


class TestPlatformConfiguration:
    def test_a_platform_url_becomes_the_public_base_url(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://bot.onrender.com")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.http.base_url == "https://bot.onrender.com"

    def test_a_platform_url_enables_the_http_server(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://bot.onrender.com")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.http.enabled
        assert config.http.usable

    def test_an_explicit_public_url_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://bot.onrender.com")
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://media.example.com")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.http.base_url == "https://media.example.com"

    def test_an_explicit_off_switch_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://bot.onrender.com")
        monkeypatch.setenv("HTTP_ENABLED", "false")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert not config.http.enabled

    def test_a_yaml_off_switch_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://bot.onrender.com")
        config = load_config(
            write_config(tmp_path, MINIMAL_YAML + "\nhttp:\n  enabled: false\n")
        )
        assert not config.http.enabled

    def test_the_platform_port_is_used(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PORT", "10000")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.http.port == 10000

    def test_an_explicit_port_wins_over_the_platform(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PORT", "10000")
        monkeypatch.setenv("HTTP_PORT", "9999")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.http.port == 9999

    def test_no_platform_means_the_default_port(self, tmp_path):
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.http.port == 8080
        assert not config.http.enabled


class TestMediaDirectory:
    def test_it_defaults_to_the_download_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "dl"))
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.download.media_root == tmp_path / "dl"

    def test_it_can_point_at_a_nas_share(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEDIA_DIR", "/media/nas")
        monkeypatch.setenv("LOCAL_URL_PREFIX", "smb://10.10.10.2/media/")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.download.media_root == Path("/media/nas")
        assert config.download.local_url_prefix == "smb://10.10.10.2/media/"

    def test_the_default_layout_keeps_the_original_name(self, tmp_path):
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.download.media_template == "{chat}/{name}"

    def test_an_unknown_field_in_the_media_template_is_fatal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEDIA_TEMPLATE", "{chat}/{nope}")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        with pytest.raises(ConfigError, match="media_template"):
            config.validate()

    def test_auto_is_a_valid_default_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEFAULT_MODE", "auto")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        config.validate()
        assert config.delivery.default_mode == "auto"


class TestDownloadTuning:
    def test_four_connections_by_default(self, tmp_path):
        assert load_config(write_config(tmp_path, MINIMAL_YAML)).download.connections == 4

    def test_more_than_eight_is_capped_with_a_warning(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOWNLOAD_CONNECTIONS", "32")
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert any("using 8" in w for w in config.validate())

    def test_zero_connections_is_fatal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOWNLOAD_CONNECTIONS", "0")
        with pytest.raises(ConfigError, match="connections"):
            load_config(write_config(tmp_path, MINIMAL_YAML)).validate()

    def test_direct_media_is_off_by_default(self, tmp_path):
        config = load_config(write_config(tmp_path, MINIMAL_YAML))
        assert config.telegram.direct_media == "off"

    def test_direct_media_rejects_a_typo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TG_DIRECT_MEDIA", "yes")
        with pytest.raises(ConfigError, match="TG_DIRECT_MEDIA"):
            load_config(write_config(tmp_path, MINIMAL_YAML)).validate()
