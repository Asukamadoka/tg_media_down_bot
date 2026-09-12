"""Configuration: YAML file as the base, environment variables on top."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .utils import ALLOWED_TEMPLATE_FIELDS, parse_bool, parse_id_list, template_fields

MODES = ("telegram", "local", "pikpak")

DEFAULT_CONFIG_PATHS = ("config.yaml", "config.yml")


class ConfigError(RuntimeError):
    """Configuration is missing or inconsistent."""


@dataclass
class TelegramConfig:
    api_id: int = 0
    api_hash: str = ""
    bot_token: str = ""
    user_session: str = ""
    session_dir: Path = Path("sessions")

    @property
    def user_session_file(self) -> Path:
        """Where the user session is stored when no session string is given."""
        return self.session_dir / "user.session"

    @property
    def bot_session_file(self) -> Path:
        return self.session_dir / "bot.session"


@dataclass
class AccessConfig:
    admin_user_ids: list[int] = field(default_factory=list)
    allowed_user_ids: list[int] = field(default_factory=list)
    allow_all_users: bool = False

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_user_ids

    def is_allowed(self, user_id: int) -> bool:
        if self.allow_all_users:
            return True
        return user_id in self.admin_user_ids or user_id in self.allowed_user_ids


@dataclass
class DownloadConfig:
    dir: Path = Path("downloads")
    data_dir: Path = Path("data")
    filename_template: str = "{chat}/{message_id}_{name}"
    concurrent: int = 2
    max_queue_per_user: int = 20
    max_batch: int = 50
    progress_interval: float = 5.0
    delete_after_delivery: bool = True
    auto_join_invites: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tgmd.sqlite3"


@dataclass
class DeliveryConfig:
    default_mode: str = "telegram"
    max_upload_size_mb: int = 2000
    cache_chat_id: int | None = None

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024


@dataclass
class PikPakConfig:
    enabled: bool = False
    username: str = ""
    password: str = ""
    folder: str = "/TelegramMedia"
    task_timeout: int = 600
    allow_user_login: bool = True
    """Whether users may connect their own account with /pikpak login."""

    login_link_ttl: int = 900
    """How long a login link stays valid, in seconds."""

    @property
    def configured(self) -> bool:
        """True when a shared account is available to every user."""
        return self.enabled and bool(self.username and self.password)


@dataclass
class HttpConfig:
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    public_base_url: str = ""
    url_ttl: int = 3600

    @property
    def usable(self) -> bool:
        """True when PikPak can actually reach files we serve."""
        return self.enabled and bool(self.public_base_url)

    @property
    def base_url(self) -> str:
        return self.public_base_url.rstrip("/")


@dataclass
class Config:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    access: AccessConfig = field(default_factory=AccessConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    pikpak: PikPakConfig = field(default_factory=PikPakConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    log_level: str = "INFO"

    def ensure_directories(self) -> None:
        """Create every directory the bot writes to."""
        for directory in (
            self.telegram.session_dir,
            self.download.dir,
            self.download.data_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def validate(self) -> list[str]:
        """Return a list of warnings, raising :class:`ConfigError` on anything fatal."""
        missing = [
            name
            for name, value in (
                ("telegram.api_id (TG_API_ID)", self.telegram.api_id),
                ("telegram.api_hash (TG_API_HASH)", self.telegram.api_hash),
                ("telegram.bot_token (TG_BOT_TOKEN)", self.telegram.bot_token),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "missing required settings: " + ", ".join(missing)
            )

        if self.delivery.default_mode not in MODES:
            raise ConfigError(
                f"delivery.default_mode must be one of {', '.join(MODES)}, "
                f"got {self.delivery.default_mode!r}"
            )

        if self.download.concurrent < 1:
            raise ConfigError("download.concurrent must be at least 1")
        if self.download.max_batch < 1:
            raise ConfigError("download.max_batch must be at least 1")
        if self.download.progress_interval < 1:
            raise ConfigError("download.progress_interval must be at least 1 second")

        unknown = template_fields(self.download.filename_template) - ALLOWED_TEMPLATE_FIELDS
        if unknown:
            raise ConfigError(
                "download.filename_template uses unknown fields: "
                + ", ".join(sorted(unknown))
                + f" (allowed: {', '.join(sorted(ALLOWED_TEMPLATE_FIELDS))})"
            )

        if self.http.enabled and not self.http.public_base_url:
            raise ConfigError(
                "http.enabled is set but http.public_base_url is empty; PikPak "
                "needs a publicly reachable URL to fetch files from"
            )

        warnings: list[str] = []
        if not self.telegram.user_session and not self.telegram.user_session_file.exists():
            warnings.append(
                "no user session configured: only chats the bot itself is in can "
                "be read. Run `python -m tgmd.login` to add a user session."
            )
        if self.access.allow_all_users:
            warnings.append(
                "access.allow_all_users is true: anyone can pull media from every "
                "chat your user account can see."
            )
        elif not self.access.admin_user_ids and not self.access.allowed_user_ids:
            warnings.append(
                "no admin or allowed user ids configured, so the bot will refuse "
                "every request. Set ADMIN_USER_IDS."
            )
        if self.delivery.default_mode == "pikpak" and not self.pikpak.configured:
            if self.pikpak.allow_user_login and self.http.usable:
                warnings.append(
                    "default mode is pikpak with no shared account, so each user "
                    "must run /pikpak login before their first transfer."
                )
            else:
                warnings.append(
                    "default mode is pikpak but there is no shared account and "
                    "no way for users to connect their own."
                )
        if self.pikpak.configured and not self.http.usable:
            warnings.append(
                "PikPak is configured but the HTTP file server is not; magnet and "
                "URL transfers will work, Telegram-to-PikPak transfers will not."
            )
        return warnings


def _get(source: dict[str, Any], *path: str, default: Any = None) -> Any:
    """Read a nested key out of a plain dict, tolerating missing levels."""
    current: Any = source
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return default if current is None else current


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value.strip()


def load_yaml(path: Path | None) -> dict[str, Any]:
    """Load the YAML config file, or return an empty mapping if there is none."""
    if path is None:
        for candidate in DEFAULT_CONFIG_PATHS:
            if Path(candidate).is_file():
                path = Path(candidate)
                break
        else:
            return {}
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return data


def load_config(path: Path | None = None) -> Config:
    """Build a :class:`Config` from the YAML file plus environment overrides.

    Environment variables always win, which keeps secrets out of the file and
    makes the container image configurable without a bind mount.
    """
    if path is None:
        env_path = os.environ.get("TGMD_CONFIG", "").strip()
        path = Path(env_path) if env_path else None

    data = load_yaml(path)

    telegram = TelegramConfig(
        api_id=_env_int("TG_API_ID", int(_get(data, "telegram", "api_id", default=0) or 0)),
        api_hash=_env_str("TG_API_HASH", str(_get(data, "telegram", "api_hash", default=""))),
        bot_token=_env_str("TG_BOT_TOKEN", str(_get(data, "telegram", "bot_token", default=""))),
        user_session=_env_str(
            "TG_USER_SESSION", str(_get(data, "telegram", "user_session", default=""))
        ),
        session_dir=Path(
            _env_str("SESSION_DIR", str(_get(data, "telegram", "session_dir", default="sessions")))
        ),
    )

    access = AccessConfig(
        admin_user_ids=parse_id_list(
            os.environ.get("ADMIN_USER_IDS") or _get(data, "access", "admin_user_ids", default=[])
        ),
        allowed_user_ids=parse_id_list(
            os.environ.get("ALLOWED_USER_IDS")
            or _get(data, "access", "allowed_user_ids", default=[])
        ),
        allow_all_users=parse_bool(
            os.environ.get("ALLOW_ALL_USERS"),
            parse_bool(_get(data, "access", "allow_all_users", default=False)),
        ),
    )

    download = DownloadConfig(
        dir=Path(_env_str("DOWNLOAD_DIR", str(_get(data, "download", "dir", default="downloads")))),
        data_dir=Path(
            _env_str("DATA_DIR", str(_get(data, "download", "data_dir", default="data")))
        ),
        filename_template=_env_str(
            "FILENAME_TEMPLATE",
            str(_get(data, "download", "filename_template", default="{chat}/{message_id}_{name}")),
        ),
        concurrent=_env_int(
            "CONCURRENT_DOWNLOADS", int(_get(data, "download", "concurrent", default=2))
        ),
        max_queue_per_user=_env_int(
            "MAX_QUEUE_PER_USER", int(_get(data, "download", "max_queue_per_user", default=20))
        ),
        max_batch=_env_int("MAX_BATCH", int(_get(data, "download", "max_batch", default=50))),
        progress_interval=_env_float(
            "PROGRESS_INTERVAL", float(_get(data, "download", "progress_interval", default=5.0))
        ),
        delete_after_delivery=parse_bool(
            os.environ.get("DELETE_AFTER_DELIVERY"),
            parse_bool(_get(data, "download", "delete_after_delivery", default=True), True),
        ),
        auto_join_invites=parse_bool(
            os.environ.get("AUTO_JOIN_INVITES"),
            parse_bool(_get(data, "download", "auto_join_invites", default=False)),
        ),
    )

    cache_chat_raw = os.environ.get("CACHE_CHAT_ID") or _get(
        data, "delivery", "cache_chat_id", default=None
    )
    cache_chat_ids = parse_id_list(cache_chat_raw)

    delivery = DeliveryConfig(
        default_mode=_env_str(
            "DEFAULT_MODE", str(_get(data, "delivery", "default_mode", default="telegram"))
        ).lower(),
        max_upload_size_mb=_env_int(
            "MAX_UPLOAD_SIZE_MB", int(_get(data, "delivery", "max_upload_size_mb", default=2000))
        ),
        cache_chat_id=cache_chat_ids[0] if cache_chat_ids else None,
    )

    pikpak = PikPakConfig(
        username=_env_str("PIKPAK_USERNAME", str(_get(data, "pikpak", "username", default=""))),
        password=_env_str("PIKPAK_PASSWORD", str(_get(data, "pikpak", "password", default=""))),
        folder=_env_str("PIKPAK_FOLDER", str(_get(data, "pikpak", "folder", default="/TelegramMedia"))),
        task_timeout=_env_int("PIKPAK_TASK_TIMEOUT", int(_get(data, "pikpak", "task_timeout", default=600))),
        allow_user_login=parse_bool(
            os.environ.get("PIKPAK_ALLOW_USER_LOGIN"),
            parse_bool(_get(data, "pikpak", "allow_user_login", default=True), True),
        ),
        login_link_ttl=_env_int(
            "PIKPAK_LOGIN_LINK_TTL", int(_get(data, "pikpak", "login_link_ttl", default=900))
        ),
    )
    # PikPak turns itself on as soon as credentials exist, so a user who only
    # fills in .env does not also have to remember the enabled flag.
    pikpak.enabled = parse_bool(
        os.environ.get("PIKPAK_ENABLED"),
        parse_bool(_get(data, "pikpak", "enabled", default=False)),
    ) or bool(pikpak.username and pikpak.password)

    http = HttpConfig(
        enabled=parse_bool(
            os.environ.get("HTTP_ENABLED"),
            parse_bool(_get(data, "http", "enabled", default=False)),
        ),
        host=_env_str("HTTP_HOST", str(_get(data, "http", "host", default="0.0.0.0"))),
        port=_env_int("HTTP_PORT", int(_get(data, "http", "port", default=8080))),
        public_base_url=_env_str(
            "PUBLIC_BASE_URL", str(_get(data, "http", "public_base_url", default=""))
        ),
        url_ttl=_env_int("HTTP_URL_TTL", int(_get(data, "http", "url_ttl", default=3600))),
    )

    return Config(
        telegram=telegram,
        access=access,
        download=download,
        delivery=delivery,
        pikpak=pikpak,
        http=http,
        log_level=_env_str("LOG_LEVEL", str(_get(data, "log_level", default="INFO"))).upper(),
    )
