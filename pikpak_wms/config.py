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
    events: bool = True
    """Keep the index current from PikPak's event feed (docs/wms/M8.1). False:
    back to the ``modified_time`` pruning alone, which misses files restored
    into an existing folder."""


JOB_NAMES = (
    "stocktake", "stocktake-full", "inbound-poll", "layout", "organize", "cleanup",
    # M7:
    "organize-tree", "organize-inbox", "dedupe", "big-report",
)


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


BUILTIN_JOBS = (
    # docs/wms/M7 §4: the entry folders hourly, the whole tree daily, dedupe
    # weekly and carried out (the owner asked for it to run on its own; the
    # whitelist keeps protected and shared copies), the big-files report weekly.
    ScheduledJob(name="organize-inbox", cron="0 * * * *"),
    ScheduledJob(name="organize-tree", cron="30 4 * * *"),
    ScheduledJob(name="dedupe", cron="0 5 * * 1", apply=True),
    ScheduledJob(name="big-report", cron="0 10 * * 1"),
    # docs/wms/M8.1: the index from the event feed every ten minutes. A deployment
    # that lists "stocktake" itself keeps its own schedule (and now runs this).
    ScheduledJob(name="stocktake", cron="*/10 * * * *"),
)


class ScheduleConfig(BaseModel):
    timezone: str = "Asia/Shanghai"
    """Cron times, ``{created|date:...}`` and dates in rules are read in this zone."""
    jobs: list[ScheduledJob] = Field(default_factory=list)
    builtin: bool = True
    """Add the M7 jobs (:data:`BUILTIN_JOBS`) that ``jobs`` does not list itself.
    List one with ``enabled: false`` to turn it off, or set this to false."""

    def effective_jobs(self) -> list[ScheduledJob]:
        listed = {job.name for job in self.jobs}
        extra = [job for job in BUILTIN_JOBS if job.name not in listed] if self.builtin else []
        return [*self.jobs, *extra]

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

    library_dir: Path | None = None
    """The NAS share ``资源库`` as the container sees it; None means ``LIBRARY_DIR``.
    When there is one, files are filed by :mod:`pikpak_wms.ops.library` instead
    of under ``local_dir`` (docs/wms/M8.3 §H)."""
    default_layout: str = "资源/整理/{Y}/{Y}.{M}/{Y}.{M}.{D}"
    """Where a download goes when no place is named, under the library. ``{Y}``,
    ``{M}``, ``{D}`` are the year, month and day the download runs, unpadded."""
    connections: int | None = None
    """Parallel HTTP Range connections per file; None means ``OUTBOUND_CONNECTIONS`` (8)."""
    verify: Literal["off", "size", "hash"] | None = None
    """What to check once a file is complete; None means ``OUTBOUND_VERIFY`` (size)."""

    @property
    def local_path(self) -> Path | None:
        if self.local_dir is not None:
            return self.local_dir
        for name in ("MEDIA_DIR", "DOWNLOAD_DIR"):
            value = os.environ.get(name, "").strip()
            if value:
                return Path(value)
        return None

    @property
    def library_path(self) -> Path | None:
        """The library when one is mounted and ``local_dir`` is not set explicitly."""
        if self.local_dir is not None:
            return None
        if self.library_dir is not None:
            return self.library_dir
        value = os.environ.get("LIBRARY_DIR", "").strip()
        return Path(value) if value else None

    @property
    def parallel(self) -> int:
        if self.connections is not None:
            return max(self.connections, 1)
        try:
            return max(int(os.environ.get("OUTBOUND_CONNECTIONS", "") or 8), 1)
        except ValueError:
            return 8

    @property
    def verify_mode(self) -> str:
        mode = self.verify or os.environ.get("OUTBOUND_VERIFY", "").strip().lower() or "size"
        return mode if mode in ("off", "size", "hash") else "size"


class NlConfig(BaseModel):
    """What natural-language commands mean by 分类 / 归档 / 下载 (docs/wms/M6)."""

    classify: dict[str, str] = Field(default_factory=lambda: {
        "video": "/Media/视频", "image": "/Media/图片", "audio": "/Media/音频",
        "document": "/Media/文档", "archive": "/Media/压缩包", "subtitle": "/Media/字幕",
    })
    """File type → folder, for 按类型分类."""

    archive_root: str = "/Archive"
    """归档 moves files to ``<archive_root>/<year-month they arrived>``."""

    download_to: str = "PikPak"
    """下载 fetches into this sub-folder of the NAS media folder."""


class DedupeConfig(BaseModel):
    scope: str = "/"
    keep_under: list[str] = Field(default_factory=list)
    """Prefer the copy under one of these folders (protected copies come first anyway)."""


class ProtectConfig(BaseModel):
    """The whitelist (docs/wms/M7 §1): nothing under these is ever touched.

    It wins over every rule and every job: a plan loses any action whose
    source or destination is protected before it is even saved.
    """

    paths: list[str] = Field(
        default_factory=lambda: ["/收藏", "/Cosplaytales Nako EP#1-24", "/小千"]
    )
    shared: bool = True
    """Also protect everything ever shared (expired shares included), read
    live from PikPak before each plan."""

    @field_validator("paths", mode="before")
    @classmethod
    def _paths(cls, value: object) -> list[str]:
        items = [value] if isinstance(value, str) else list(value or [])
        return ["/" + "/".join(p for p in str(item).split("/") if p) for item in items]


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
    nl: NlConfig = Field(default_factory=NlConfig)
    protect: ProtectConfig = Field(default_factory=ProtectConfig)
    dedupe: DedupeConfig = Field(default_factory=DedupeConfig)


def _first_existing(env: str, name: str, default: Path) -> Path:
    """The ``env`` path when set; else ``$DATA_DIR/<name>`` if it exists; else ``default``.

    ``$DATA_DIR`` comes second so a container keeps its WMS files on the
    data volume (``/data/db`` in the image) with no extra setting, while a
    checkout keeps using ``config/``.
    """
    raw = os.environ.get(env, "").strip()
    if raw:
        return Path(raw)
    on_volume = data_dir() / name
    return on_volume if on_volume.exists() else default


def config_path() -> Path:
    """``WMS_CONFIG``, else ``$DATA_DIR/wms.yaml`` if present, else ``config/wms.yaml``."""
    return _first_existing("WMS_CONFIG", "wms.yaml", DEFAULT_CONFIG_PATH)


def rules_path() -> Path:
    """``WMS_RULES``, else ``$DATA_DIR/rules.yaml`` if present, else ``config/rules.yaml``."""
    return _first_existing("WMS_RULES", "rules.yaml", DEFAULT_RULES_PATH)


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
