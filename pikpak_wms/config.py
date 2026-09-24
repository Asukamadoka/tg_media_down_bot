"""WMS configuration: YAML for behaviour, the environment for anything secret.

A missing config file is not an error: every setting has a default, so
``wms --help`` and the bot's integration work in an empty directory. Paths
that hold state default to ``$DATA_DIR``, which in the container is the
``/data/db`` volume the bot already uses, so the index and the token survive
a rebuild exactly like the bot's own database does.
"""

from __future__ import annotations

import os
from datetime import tzinfo
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_PATH = Path("config/wms.yaml")
DEFAULT_RULES_PATH = Path("config/rules.yaml")


def data_dir() -> Path:
    """Where state lives: ``DATA_DIR`` when set (the container), else ``data``."""
    return Path(os.environ.get("DATA_DIR", "").strip() or "data")


class RuntimeConfig(BaseModel):
    dry_run: bool = True
    """Rule 1: writes only produce a plan unless ``--apply`` is given."""

    allow_permanent_delete: bool = False
    """Rule 2: even when true, each command must also say ``--forever``."""

    max_actions_per_run: int = 500


class RateLimitConfig(BaseModel):
    requests_per_second: float = 4.0
    burst: int = 8
    max_retries: int = 3
    initial_backoff_seconds: float = 3.0


class StoreConfig(BaseModel):
    database: Path | None = None
    """None means ``$DATA_DIR/wms.sqlite3``."""

    token_file: Path | None = None
    """None means ``$DATA_DIR/wms-token.json``."""

    @property
    def database_path(self) -> Path:
        return self.database or data_dir() / "wms.sqlite3"

    @property
    def token_path(self) -> Path:
        return self.token_file or data_dir() / "wms-token.json"


class LayoutConfig(BaseModel):
    inbox: str = "/Inbox"
    temp: str = "/Temp"
    archive: str = "/Media"
    ensure: list[str] = Field(default_factory=list)


class StocktakeConfig(BaseModel):
    roots: list[str] = Field(default_factory=lambda: ["/"])
    incremental: bool = True
    page_size: int = 100


JOB_NAMES = ("stocktake", "stocktake-full", "inbound-poll", "layout", "organize", "cleanup")


class ScheduledJob(BaseModel):
    name: str
    cron: str
    enabled: bool = True
    apply: bool = False
    """organize / cleanup / layout: apply the plan instead of only saving it.
    Never permanent deletion: no scheduled job can delete forever (rule 2)."""

    @field_validator("name")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in JOB_NAMES:
            raise ValueError(f"unknown job {value!r}; use one of {', '.join(JOB_NAMES)}")
        return value


class ScheduleConfig(BaseModel):
    timezone: str = "Asia/Shanghai"
    """Cron times, ``{created|date:...}`` and dates in rules are read in this zone."""
    jobs: list[ScheduledJob] = Field(default_factory=list)

    @property
    def tz(self) -> tzinfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {self.timezone!r}") from exc


class Aria2Config(BaseModel):
    rpc_url: str = "http://127.0.0.1:6800/jsonrpc"
    dir: str = "/downloads"
    """Where aria2 (not this container) saves files."""
    # The secret is read from ARIA2_SECRET only: no credential in YAML.


class OutboundConfig(BaseModel):
    downloader: Literal["none", "aria2", "local"] = "none"
    """none: show direct links; aria2: hand them to aria2; local: fetch into local_dir."""

    aria2: Aria2Config = Field(default_factory=Aria2Config)
    local_dir: Path | None = None
    """None means the bot's ``MEDIA_DIR`` (else ``DOWNLOAD_DIR``), so files land
    in the same NAS folder the bot already writes to."""

    @property
    def local_path(self) -> Path | None:
        if self.local_dir is not None:
            return self.local_dir
        for name in ("MEDIA_DIR", "DOWNLOAD_DIR"):
            value = os.environ.get(name, "").strip()
            if value:
                return Path(value)
        return None


class Config(BaseModel):
    version: int = 1
    rules_file: Path | None = None
    """None means ``WMS_RULES``, else ``config/rules.yaml``."""
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    ratelimit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)
    layout: LayoutConfig = Field(default_factory=LayoutConfig)
    stocktake: StocktakeConfig = Field(default_factory=StocktakeConfig)
    outbound: OutboundConfig = Field(default_factory=OutboundConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)


def config_path() -> Path:
    """``WMS_CONFIG`` when set, else ``config/wms.yaml``."""
    raw = os.environ.get("WMS_CONFIG", "").strip()
    return Path(raw) if raw else DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> Config:
    """Read the config file; defaults for anything it does not say."""
    target = path or config_path()
    if not target.exists():
        return Config()
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return Config.model_validate(raw)


class Credentials(BaseModel):
    """For the command line on its own. The bot hands over its own client."""

    username: str | None = None
    password: str | None = None
    encoded_token: str | None = None

    @classmethod
    def from_environment(cls) -> Credentials:
        def read(name: str) -> str | None:
            value = os.environ.get(name, "").strip()
            return value or None

        return cls(
            username=read("PIKPAK_USERNAME"),
            password=read("PIKPAK_PASSWORD"),
            encoded_token=read("PIKPAK_ENCODED_TOKEN"),
        )

    @property
    def usable(self) -> bool:
        return bool(self.encoded_token or (self.username and self.password))
