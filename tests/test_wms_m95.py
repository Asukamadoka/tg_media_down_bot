"""WMS M9.5: PikPak's captcha scoped to one request, and an event feed that may fail.

The fault: pikpakapi parks a captcha on the shared api object while it fetches a
download link, so any request sent meanwhile (``/do``'s events call) carried a
token minted for another action, and a failed fetch left it there for good.
"""

# ruff: noqa: SLF001
from __future__ import annotations

import asyncio
import json

import pytest
from pikpakapi.PikpakException import PikpakException
from wms_fakes import FakeDrive, provider_for

from pikpak_wms.config import Config
from pikpak_wms.core.client import WmsClient
from pikpak_wms.core.errors import AuthError, CaptchaError
from pikpak_wms.core.models import render_note
from pikpak_wms.core.ratelimit import TokenBucket
from pikpak_wms.i18n import set_language
from pikpak_wms.ops import eventsync
from pikpak_wms.ops.context import Context
from pikpak_wms.ops.stocktake import stocktake
from pikpak_wms.store.db import Store

EVENTS = "GET:/drive/v1/events"


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language(None)


async def _no_sleep(_seconds: float) -> None:
    return None


def make_client(drive: FakeDrive) -> WmsClient:
    return WmsClient(provider_for(drive), limiter=TokenBucket(1e9, 1_000_000), sleep=_no_sleep)


class TestDownloadLinksOwnTheirCaptcha:
    async def test_it_never_sets_the_shared_token(self):
        drive = FakeDrive()
        seen: list[object] = []
        original = drive._make_request

        async def watching(*args, **kwargs):
            seen.append(drive.captcha_token)
            return await original(*args, **kwargs)

        drive._make_request = watching
        web, origin = await make_client(drive).download_links("f1")
        assert (web, origin) == ("https://download.example/f1", None)
        assert seen == [None] and drive.captcha_token is None
        assert drive.captcha_actions == ["GET:/drive/v1/files/f1"]
        assert drive.requests[-1][1] == "captcha-for:GET:/drive/v1/files/f1"

    async def test_a_failed_fetch_leaves_nothing_behind(self):
        drive = FakeDrive()
        drive.download_info = lambda _id: (_ for _ in ()).throw(PikpakException("boom"))
        with pytest.raises(Exception, match="boom"):
            await make_client(drive).download_links("f1")
        assert drive.captcha_token is None

    async def test_events_during_a_slow_fetch_carry_no_captcha_of_another_action(self):
        drive = FakeDrive()
        drive.add("/Movies/a.mkv", size=1)
        gate = asyncio.Event()
        started = asyncio.Event()
        original = drive._make_request

        async def slow(method, url, data=None, params=None, headers=None):
            if "/drive/v1/files/" in url:
                started.set()
                await gate.wait()
            return await original(method, url, data=data, params=params, headers=headers)

        drive._make_request = slow
        client = make_client(drive)
        fetch = asyncio.create_task(client.download_links("f1"))
        await started.wait()
        page = await client.events()          # plain request, mid-fetch
        gate.set()
        await fetch
        assert "events" in page
        assert all(token is None or "files/f1" in token for _url, token in drive.requests)
        assert "Verification" not in str(drive.calls)
        assert drive.captcha_token is None


class TestCaptchaRefusals:
    async def test_a_leaked_token_is_cleared_and_the_call_retried_for_its_own_action(
        self, caplog
    ):
        drive = FakeDrive()
        drive.captcha_token = "leaked-secret-token"
        with caplog.at_level("WARNING"):
            page = await make_client(drive).events()
        assert "events" in page
        assert drive.captcha_token is None
        assert drive.captcha_actions == [EVENTS]            # exactly one extra captcha
        assert drive.requests == [("https://api-drive.mypikpak.com/drive/v1/events",
                                   f"captcha-for:{EVENTS}")]
        assert "leaked-secret-token" not in caplog.text
        assert "cleared a captcha token" in caplog.text

    async def test_a_second_refusal_is_a_captcha_error(self):
        drive = FakeDrive()
        drive.captcha_token = "leaked"

        async def refuse(**_kw):
            raise PikpakException("Verification code is invalid")

        drive._make_request = lambda *a, **k: refuse()
        client = make_client(drive)
        with pytest.raises(CaptchaError, match="events"):
            await client.events()
        assert drive.captcha_actions == [EVENTS]            # one retry, not a loop

    async def test_download_links_retries_once_then_gives_up(self):
        drive = FakeDrive()

        async def refuse(*_a, **_k):
            raise PikpakException("captcha_invalid")

        drive._make_request = refuse
        with pytest.raises(CaptchaError):
            await make_client(drive).download_links("f1")
        assert len(drive.captcha_actions) == 2              # the first try and the one retry


@pytest.fixture
async def setup(tmp_path):
    config = Config()
    config.rules_file = tmp_path / "no-rules.yaml"
    drive = FakeDrive(propagate=False)
    async with Store(tmp_path / "wms.sqlite3") as store:
        yield drive, Context(config=config, client=make_client(drive), store=store)


class _Plan:
    def __init__(self) -> None:
        self.notes: list[dict] = []

    def note(self, key: str, **args) -> None:
        self.notes.append({"key": key, "args": args})


class TestEventsFailureNeverFailsTheRun:
    async def baseline(self, drive, ctx):
        drive.add("/Movies/old.mkv", size=1)
        await stocktake(ctx.client, ctx.store, full=True)
        await eventsync.sync_events(ctx.client, ctx.store)

    async def test_a_first_page_error_runs_an_incremental_stocktake(self, setup):
        drive, ctx = setup
        await self.baseline(drive, ctx)
        before = await ctx.store.get_meta(eventsync.CURSOR_KEY)
        drive.add("/Movies/new.mkv", size=1)
        drive.fail_next.append(PikpakException("Verification code is invalid"))
        # Two refusals in a row: the plain call and the retry.
        drive.fail_next.append(PikpakException("Verification code is invalid"))

        result = await eventsync.refresh_index(ctx, allow_full=True)

        assert result.kind == "incremental"
        assert result.events.failed and "the event feed failed" in result.events.reason
        assert await ctx.store.get_meta(eventsync.CURSOR_KEY) == before   # not rebased
        assert result.alert                                               # the admins hear
        assert (await eventsync.refresh_index(ctx)).alert == ""            # but only once
        assert (await ctx.store.node(drive.id_at("/Movies/new.mkv"))) is not None

    async def test_the_plan_says_so_in_both_languages(self, setup):
        drive, ctx = setup
        await self.baseline(drive, ctx)
        drive.fail_next.extend([PikpakException("Verification code is invalid")] * 2)
        await eventsync.refresh_index(ctx)
        plan = _Plan()
        await eventsync.note_freshness(ctx, [plan])
        assert [n["key"] for n in plan.notes] == ["sync.updated", "sync.failed"]
        rendered = render_note(plan.notes[1])
        assert rendered.startswith("Event sync failed (") and "incremental stocktake" in rendered
        set_language("zh")
        assert "事件同步失败" in render_note(plan.notes[1])

    async def test_a_good_sync_clears_the_failure(self, setup):
        drive, ctx = setup
        await self.baseline(drive, ctx)
        drive.fail_next.extend([PikpakException("Verification code is invalid")] * 2)
        await eventsync.refresh_index(ctx)
        assert await eventsync.failure(ctx)
        await eventsync.refresh_index(ctx)
        assert await eventsync.failure(ctx) == ""
        assert json.loads(await ctx.store.get_meta(eventsync.SYNC_KEY))["kind"] == "events"

    async def test_an_auth_error_still_propagates(self, setup):
        drive, ctx = setup
        await self.baseline(drive, ctx)
        drive.fail_next.append(PikpakException("invalid_grant"))
        with pytest.raises(AuthError):
            await eventsync.refresh_index(ctx)
