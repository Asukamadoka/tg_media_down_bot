"""Shared fixtures."""

from __future__ import annotations

import pytest

# Every environment variable the loader reads. Tests must not inherit the
# developer's own shell or a stray .env.
ENV_VARS = (
    "TGMD_CONFIG",
    "TG_API_ID",
    "TG_API_HASH",
    "TG_BOT_TOKEN",
    "TG_USER_SESSION",
    "SESSION_DIR",
    "ADMIN_USER_IDS",
    "ALLOWED_USER_IDS",
    "ALLOW_ALL_USERS",
    "DOWNLOAD_DIR",
    "DATA_DIR",
    "FILENAME_TEMPLATE",
    "CONCURRENT_DOWNLOADS",
    "DOWNLOAD_CONNECTIONS",
    "MAX_QUEUE_PER_USER",
    "MAX_BATCH",
    "PROGRESS_INTERVAL",
    "DELETE_AFTER_DELIVERY",
    "AUTO_JOIN_INVITES",
    "DEFAULT_MODE",
    "MAX_UPLOAD_SIZE_MB",
    "CACHE_CHAT_ID",
    "PIKPAK_ENABLED",
    "PIKPAK_USERNAME",
    "PIKPAK_PASSWORD",
    "PIKPAK_FOLDER",
    "PIKPAK_TASK_TIMEOUT",
    "PIKPAK_ALLOW_USER_LOGIN",
    "PIKPAK_LOGIN_LINK_TTL",
    "HTTP_ENABLED",
    "HTTP_HOST",
    "HTTP_PORT",
    "PUBLIC_BASE_URL",
    "HTTP_URL_TTL",
    "LOG_LEVEL",
    # Which message catalogue tgmd.i18n reads. POSIX LANG is deliberately not
    # one of these: images set it to C.UTF-8 for unrelated reasons.
    "TGMD_LANG",
    "BOT_LANG",
    # Hosting platforms export these; they must not leak into tests.
    "PORT",
    "RENDER_EXTERNAL_URL",
    "KOYEB_PUBLIC_DOMAIN",
    "RAILWAY_PUBLIC_DOMAIN",
    "SPACE_HOST",
    "FLY_APP_NAME",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Remove every setting the config loader looks at."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_pikpak_network(monkeypatch):
    """Make a real PikPak call fail loudly instead of hanging.

    Every request in the library funnels through these two methods, so a test
    that reaches PikPak by accident (a renamed attribute breaking a stub, say)
    fails in milliseconds with a clear message rather than stalling on a
    network timeout.
    """

    async def refuse(*_args, **_kwargs):
        raise AssertionError(
            "a test tried to reach PikPak over the network; stub the client instead"
        )

    monkeypatch.setattr("pikpakapi.PikPakApi.login", refuse)
    monkeypatch.setattr("pikpakapi.PikPakApi._make_request", refuse)
