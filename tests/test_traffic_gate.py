"""M9: the download gate and rate limits in the paths they guard (docs/wms/M9 §D, §F)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from pikpak_wms.ops import fetch
from tgmd.botconfig import COMMAND_NAMES
from tgmd.config import Config, DeliveryConfig, TrafficConfig, load_config
from tgmd.db import Database
from tgmd.delivery import Delivery
from tgmd.downloader import DownloadCancelled, Downloader, MediaInfo
from tgmd.parallel import download_parts
from tgmd.traffic import TokenBucket, TrafficControl, TrafficService
from tgmd.traffic import ui as traffic_ui
from tgmd.traffic.gate import MB
from tgmd.traffic.store import TrafficStore

CONTENT = bytes(range(256)) * 41
PART = 1024


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "bot.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def settle(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


# -------------------------------------------------------------- token bucket


class VirtualTime:
    """A clock that only moves when somebody sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class TestTokenBucket:
    async def test_the_average_rate_holds_whatever_the_request_size(self):
        world = VirtualTime()
        bucket = TokenBucket(5 * MB, burst=0.1, clock=world.clock, sleep=world.sleep)
        started = world.now
        total = 0
        for size in [MB, 256 * 1024, 3 * MB, MB, 512 * 1024] * 40:
            await bucket.take(size)
            total += size
        rate = total / (world.now - started)
        assert rate == pytest.approx(5 * MB, rel=0.10)

    async def test_it_holds_across_concurrent_workers(self):
        bucket = TokenBucket(40 * MB, burst=0.05)
        started = time.monotonic()

        async def worker():
            for _ in range(40):
                await bucket.take(256 * 1024)       # 10 MiB each, 40 MiB over four

        await asyncio.gather(*(worker() for _ in range(4)))
        rate = 40 * MB / (time.monotonic() - started)
        assert rate == pytest.approx(40 * MB, rel=0.10)

    async def test_no_rate_means_no_waiting(self):
        world = VirtualTime()
        bucket = TokenBucket(0, clock=world.clock, sleep=world.sleep)
        await bucket.take(10**9)
        assert world.now == 1000.0

    async def test_the_rate_can_change_while_running(self):
        world = VirtualTime()
        bucket = TokenBucket(1 * MB, burst=0.1, clock=world.clock, sleep=world.sleep)
        await bucket.take(MB)
        bucket.set_rate(0)
        before = world.now
        await bucket.take(100 * MB)
        assert world.now == before


# --------------------------------------------------------------------- gate


class TestGate:
    async def test_a_closed_gate_holds_work_and_reopening_releases_it(self):
        control = TrafficControl(TrafficConfig())
        await control.pause()
        told: list[int] = []
        task = asyncio.create_task(control.before_file("download", notice=lambda: told.append(1)))
        await asyncio.sleep(0.05)
        assert not task.done() and told == [1]
        await control.resume()
        assert await asyncio.wait_for(task, 2) is True
        assert told == [1]

    async def test_cancelling_while_gated_gives_up_instead_of_waiting_forever(self):
        control = TrafficControl(TrafficConfig())
        await control.pause()
        cancel = asyncio.Event()
        task = asyncio.create_task(control.before_file("download", cancel=cancel))
        await asyncio.sleep(0.05)
        cancel.set()
        assert await asyncio.wait_for(task, 3) is False

    async def test_states_and_reasons(self):
        control = TrafficControl(TrafficConfig())
        assert control.state == "open"
        await control.set_over_budget("daily_cny:2026-10-02")
        assert control.state == "over_budget"
        await control.pause()
        assert control.state == "paused"
        await control.resume()
        assert control.state == "open"
        # Reopened by hand against that budget: not reasserted for it.
        assert await control.set_over_budget("daily_cny:2026-10-02") is False
        assert await control.set_over_budget("daily_cny:2026-10-03") is True

    async def test_state_survives_a_restart(self, db):
        config = TrafficConfig(media_rate_mbps=8)
        control = TrafficControl(config, db)
        await control.pause()
        await control.set_rate("media", 2)
        await control.set_rate("upload", 0)
        again = TrafficControl(config, db)
        await again.load()
        assert again.state == "paused"
        assert again.rate_mbps("media") == 2 and again.media.rate == 2 * MB
        assert again.rate_mbps("upload") == 0

    async def test_the_runtime_limit_overrides_the_environment_until_cleared(self, db):
        control = TrafficControl(TrafficConfig(media_rate_mbps=8, upload_rate_mbps=3), db)
        assert (control.rate_mbps("media"), control.rate_mbps("upload")) == (8, 3)
        await control.set_rate("media", 0)            # "no limit" is a choice too
        assert control.rate_mbps("media") == 0 and control.media.rate == 0
        await control.set_rate("media", None)
        assert control.rate_mbps("media") == 8


# ----------------------------------------------------------- download path


class Source:
    def __init__(self, hook=None) -> None:
        self.served: list[int] = []
        self.hook = hook

    async def get(self, location, offset: int, limit: int) -> bytes:
        await asyncio.sleep(0)
        self.served.append(offset)
        if self.hook is not None:
            await self.hook(len(self.served))
        return CONTENT[offset : offset + limit]


class TestParallelDownloadIsGated:
    async def test_a_gated_download_waits_and_resumes_with_the_file_intact(self, tmp_path):
        control = TrafficControl(TrafficConfig())
        await control.pause()
        sources = [Source(), Source()]
        path = tmp_path / "out.bin"

        async def gate():
            await control.before_part("download")

        task = asyncio.create_task(download_parts(
            sources, location="x", size=len(CONTENT), path=path, part_size=PART,
            before_part=gate))
        await asyncio.sleep(0.1)
        assert not task.done() and all(not s.served for s in sources)   # nothing fetched
        await control.resume()
        await asyncio.wait_for(task, 5)
        assert path.read_bytes() == CONTENT

    async def test_a_gate_that_closes_mid_download_stops_the_next_parts(self, tmp_path):
        control = TrafficControl(TrafficConfig())

        async def close_after_three(count):
            if count == 3:
                await control.pause()

        sources = [Source(close_after_three), Source(close_after_three)]
        path = tmp_path / "out.bin"

        async def gate():
            await control.before_part("download")

        task = asyncio.create_task(download_parts(
            sources, location="x", size=len(CONTENT), path=path, part_size=PART,
            before_part=gate))
        await asyncio.sleep(0.15)
        stalled = sum(len(s.served) for s in sources)
        assert not task.done() and stalled < len(CONTENT) // PART + 1
        await asyncio.sleep(0.1)
        assert sum(len(s.served) for s in sources) == stalled        # really stopped
        await control.resume()
        await asyncio.wait_for(task, 5)
        assert path.read_bytes() == CONTENT

    async def test_the_rate_limit_paces_the_parts(self, tmp_path):
        control = TrafficControl(TrafficConfig(media_rate_mbps=1))
        control.media = TokenBucket(20 * 1024, burst=0.1)           # 20 KiB/s, small burst
        started = time.monotonic()
        path = tmp_path / "out.bin"
        await download_parts(
            [Source(), Source()], location="x", size=len(CONTENT), path=path, part_size=PART,
            pace=lambda n: control.pace("media", n))
        elapsed = time.monotonic() - started
        assert path.read_bytes() == CONTENT
        # 10,496 bytes at 20 KiB/s, less a 2 KiB burst: about 0.4 s.
        assert 0.3 < elapsed < 0.8


class ChunkedClient:
    """download_media() that reports every chunk and writes it, as Telethon does."""

    def __init__(self, after_chunk=None) -> None:
        self.after_chunk = after_chunk
        self.calls = 0

    async def download_media(self, message, file, progress_callback=None):
        self.calls += 1
        written = bytearray()
        with open(file, "wb") as handle:
            for start in range(0, len(CONTENT), PART):
                chunk = CONTENT[start:start + PART]
                handle.write(chunk)
                written += chunk
                if progress_callback is not None:
                    await progress_callback(len(written), len(CONTENT))
                if self.after_chunk is not None:
                    await self.after_chunk(len(written))
        return file


MESSAGE = SimpleNamespace(
    id=1, document=None, photo=None,
    file=SimpleNamespace(name="clip.mp4", size=len(CONTENT), mime_type="video/mp4",
                         duration=None, width=None, height=None),
)


class TestDownloaderIsGated:
    async def test_a_paused_gate_holds_a_file_back_and_says_so(self, tmp_path):
        control = TrafficControl(TrafficConfig())
        await control.pause()
        client = ChunkedClient()
        told: list[int] = []

        async def on_gated():
            told.append(1)

        task = asyncio.create_task(Downloader(client, control=control).download(
            MESSAGE, tmp_path / "a" / "clip.mp4", on_gated=on_gated))
        await asyncio.sleep(0.1)
        assert client.calls == 0 and told == [1] and not task.done()
        await control.resume()
        path = await asyncio.wait_for(task, 5)
        assert path.read_bytes() == CONTENT

    async def test_a_forced_small_budget_pauses_a_download_and_resume_completes_it(
            self, tmp_path):
        """Acceptance: the budget trips mid-file; 恢复下载 lets it finish, intact."""
        control = TrafficControl(TrafficConfig(budget_daily_cny=0.5, on_budget="pause"))
        store = TrafficStore(tmp_path / "t.sqlite3")
        clock = [datetime(2026, 10, 2, 3, 0, tzinfo=UTC)]
        service = TrafficService(control._config, store, control, clock=lambda: clock[0])  # noqa: SLF001
        await service.start()
        counter = {"seen": 0}

        async def spend_when_halfway(received: int):
            if received >= len(CONTENT) // 2 and not counter["seen"]:
                counter["seen"] = received
                node = "node｜0.09元/G｜"
                big = {"id": "t", "upload": 0, "download": 10 * 1024**3,
                       "metadata": {"host": "149.154.167.50", "destinationIP": "",
                                    "destinationPort": "443"},
                       "chains": [node, "TG"]}
                clock[0] = clock[0].replace(minute=1)
                await service.ingest({"uploadTotal": 0, "downloadTotal": 0,
                                      "connections": []}, clock[0])
                clock[0] = clock[0].replace(minute=2)
                await service.ingest({"uploadTotal": 0, "downloadTotal": 10 * 1024**3,
                                      "connections": [big]}, clock[0])

        client = ChunkedClient(spend_when_halfway)
        downloader = Downloader(client, control=control)
        task = asyncio.create_task(downloader.download(MESSAGE, tmp_path / "b" / "clip.mp4"))
        await asyncio.sleep(0.3)
        assert control.state == "over_budget"
        assert not task.done()
        assert (tmp_path / "b" / "clip.mp4").stat().st_size < len(CONTENT)  # stopped part-way
        await control.resume()
        path = await asyncio.wait_for(task, 5)
        assert path.read_bytes() == CONTENT
        await service.stop()

    async def test_a_gated_download_can_still_be_cancelled(self, tmp_path):
        control = TrafficControl(TrafficConfig())
        await control.pause()
        cancel = asyncio.Event()
        task = asyncio.create_task(Downloader(ChunkedClient(), control=control).download(
            MESSAGE, tmp_path / "c" / "clip.mp4", cancel=cancel))
        await asyncio.sleep(0.05)
        cancel.set()
        with pytest.raises(DownloadCancelled):
            await asyncio.wait_for(task, 3)

    async def test_without_a_control_nothing_changes(self, tmp_path):
        path = await Downloader(ChunkedClient()).download(MESSAGE, tmp_path / "d" / "clip.mp4")
        assert path.read_bytes() == CONTENT


# -------------------------------------------------------------- upload path


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, object]] = []
        self.chunks_reported = 0

    async def send_file(self, chat_id, file, *, progress_callback=None, **_kwargs):
        if progress_callback is not None:
            for sent in range(PART, len(CONTENT) + PART, PART):
                await progress_callback(min(sent, len(CONTENT)), len(CONTENT))
                self.chunks_reported += 1
        self.sent.append((chat_id, file))
        return SimpleNamespace(id=1)


class TestUploadIsGated:
    def delivery(self, db, control, bot):
        return Delivery(bot, Config(delivery=DeliveryConfig()), db,
                        SimpleNamespace(), SimpleNamespace(), control=control)

    async def test_an_upload_waits_for_the_gate(self, tmp_path, db):
        control = TrafficControl(TrafficConfig())
        await control.pause()
        bot = FakeBot()
        path = tmp_path / "clip.mp4"
        path.write_bytes(CONTENT)
        told: list[int] = []
        task = asyncio.create_task(self.delivery(db, control, bot).to_telegram(
            7, path, MediaInfo(file_name="clip.mp4", size=len(CONTENT)),
            on_gated=lambda: told.append(1)))
        await asyncio.sleep(0.1)
        assert bot.sent == [] and told == [1] and not task.done()
        await control.resume()
        result = await asyncio.wait_for(task, 5)
        assert result.mode == "telegram" and len(bot.sent) == 1

    async def test_the_upload_limit_paces_the_parts(self, tmp_path, db):
        control = TrafficControl(TrafficConfig())
        control.upload = TokenBucket(20 * 1024, burst=0.1)
        bot = FakeBot()
        path = tmp_path / "clip.mp4"
        path.write_bytes(CONTENT)
        started = time.monotonic()
        await self.delivery(db, control, bot).to_telegram(
            7, path, MediaInfo(file_name="clip.mp4", size=len(CONTENT)))
        assert 0.3 < time.monotonic() - started < 0.8

    async def test_the_download_gate_and_the_upload_limit_are_separate(self):
        control = TrafficControl(TrafficConfig(media_rate_mbps=1))
        assert control.media.rate == MB and control.upload.rate == 0


# ------------------------------------------------- PikPak -> NAS stays direct


class FakeIO:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def probe(self, url):
        return len(self.data)

    async def stream(self, url, start, end):
        end = len(self.data) if end is None else end
        for at in range(start, end, 1000):
            yield self.data[at:min(at + 1000, end)]


async def links():
    return "link"


class TestOutboundDownloadIsDirect:
    @pytest.fixture(autouse=True)
    def clean_hook(self):
        yield
        fetch.throttle = None

    async def test_a_budget_pause_does_not_touch_it(self, tmp_path):
        control = TrafficControl(TrafficConfig())
        await control.set_over_budget("daily_cny:2026-10-02")
        fetch.throttle = control.before_direct
        data = bytes(range(256)) * 2000
        size = await asyncio.wait_for(
            fetch.download(links, FakeIO(data), tmp_path / "a.part", connections=2), 5)
        assert size == len(data) and (tmp_path / "a.part").read_bytes() == data

    async def test_the_direct_cap_holds_it_until_the_day_rolls_over(self, tmp_path):
        control = TrafficControl(TrafficConfig(direct_daily_gb=1))
        control.set_direct_over(True)
        fetch.throttle = control.before_direct
        data = bytes(range(256)) * 2000
        task = asyncio.create_task(
            fetch.download(links, FakeIO(data), tmp_path / "b.part", connections=2))
        await asyncio.sleep(0.1)
        assert not task.done()
        control.set_direct_over(False)
        assert await asyncio.wait_for(task, 5) == len(data)

    async def test_the_service_closes_the_direct_cap_from_what_it_measured(self, tmp_path):
        config = TrafficConfig(enabled=False, direct_daily_gb=0.001, bytes_per_gb=1_000_000,
                               daily_report_at="")
        control = TrafficControl(config)
        store = TrafficStore(tmp_path / "t.sqlite3")
        moment = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)
        service = TrafficService(config, store, control, clock=lambda: moment)
        await service.start()
        base = {"uploadTotal": 0, "downloadTotal": 0, "connections": []}
        await service.ingest(base, moment)
        pikpak = {"id": "p", "upload": 0, "download": 5000,
                  "metadata": {"host": "dl-a1.mypikpak.com", "destinationIP": "",
                               "destinationPort": "443"}, "chains": ["DIRECT"]}
        await service.ingest({**base, "downloadTotal": 5000, "connections": [pikpak]}, moment)
        assert control.direct_over is True
        assert control.state == "open"          # the Telegram gate is a different matter
        await service.stop()


# ------------------------------------------------------------ config and ui


class TestConfigAndMenu:
    def test_defaults_need_no_environment(self):
        traffic = load_config().traffic
        assert traffic.enabled and traffic.mihomo_api == "http://127.0.0.1:9090"
        assert traffic.poll_seconds == 5 and traffic.on_budget == "pause"
        assert traffic.budget_daily_cny == 0 and traffic.media_rate_mbps == 0
        assert traffic.conn_alert_mb == 500 and traffic.daily_report_at == "09:00"

    def test_the_environment_is_read(self, monkeypatch):
        for name, value in {
            "MIHOMO_API": "http://10.0.0.2:9090/", "TRAFFIC_POLL_SECONDS": "10",
            "TRAFFIC_BUDGET_DAILY_CNY": "2", "TRAFFIC_BUDGET_MONTHLY_CNY": "30",
            "TRAFFIC_ON_BUDGET": "WARN", "TG_MEDIA_RATE_LIMIT_MBPS": "5",
            "TG_UPLOAD_RATE_LIMIT_MBPS": "2", "TRAFFIC_DIRECT_DAILY_GB": "50",
            "TRAFFIC_ENABLED": "0", "TRAFFIC_DAILY_REPORT_AT": "",
        }.items():
            monkeypatch.setenv(name, value)
        traffic = load_config().traffic
        assert traffic.mihomo_api == "http://10.0.0.2:9090" and traffic.poll_seconds == 10
        assert (traffic.budget_daily_cny, traffic.budget_monthly_cny) == (2, 30)
        assert traffic.on_budget == "warn" and not traffic.enabled
        assert (traffic.media_rate_mbps, traffic.upload_rate_mbps) == (5, 2)
        assert traffic.direct_daily_gb == 50 and traffic.daily_report_at == ""

    def test_a_bad_budget_mode_is_refused(self):
        from tgmd.config import ConfigError

        config = Config(traffic=TrafficConfig(on_budget="explode"))
        config.telegram.api_id, config.telegram.api_hash = 1, "x"
        config.telegram.bot_token = "t"
        with pytest.raises(ConfigError, match="TRAFFIC_ON_BUDGET"):
            config.validate()

    def test_the_command_is_in_the_menu(self):
        assert "traffic" in COMMAND_NAMES

    def test_buttons_fit_telegram_and_offer_what_the_spec_lists(self, chinese_ui):
        markup = traffic_ui.main_buttons("today", gate_open=True)
        labels = [b.text for row in markup.rows for b in row.buttons]
        assert labels == ["今天", "本周", "本月", "暂停下载", "限速"]
        closed = traffic_ui.main_buttons("7d", gate_open=False)
        assert [b.text for row in closed.rows for b in row.buttons][3] == "恢复下载"
        rates = traffic_ui.rate_buttons("today")
        down = [b.text for b in rates.rows[0].buttons]
        assert down == ["⬇ 不限", "⬇ 2", "⬇ 5", "⬇ 10", "⬇ 20"]


@pytest.fixture
def chinese_ui():
    from tgmd import i18n

    previous = i18n.language()
    i18n.set_language("zh")
    yield
    i18n.set_language(previous)


class FakeEvent:
    def __init__(self, data: str) -> None:
        self.data = data.encode()
        self.edits: list[str] = []
        self.answers: list[str] = []

    async def edit(self, text, **_kwargs):
        self.edits.append(text)

    async def answer(self, text="", **_kwargs):
        self.answers.append(text)


class TestButtons:
    async def make(self, tmp_path):
        config = TrafficConfig(enabled=False, daily_report_at="")
        control = TrafficControl(config)
        service = TrafficService(config, TrafficStore(tmp_path / "t.sqlite3"), control)
        await service.start()
        return service, control

    async def test_pause_resume_and_rate_buttons_act_and_redraw(self, tmp_path, chinese_ui):
        service, control = await self.make(tmp_path)
        pause = FakeEvent("traffic:pause:today")
        await traffic_ui.handle_button(pause, service)
        assert control.state == "paused" and "已暂停（手动）" in pause.edits[-1]
        assert pause.answers == ["已暂停下载"]
        resume = FakeEvent("traffic:resume:today")
        await traffic_ui.handle_button(resume, service)
        assert control.state == "open" and "下载闸门：开启" in resume.edits[-1]
        menu = FakeEvent("traffic:rate:today")
        await traffic_ui.handle_button(menu, service)
        assert "限速（MB/s）" in menu.edits[-1]
        choose = FakeEvent("traffic:set:media:5:today")
        await traffic_ui.handle_button(choose, service)
        assert control.rate_mbps("media") == 5 and control.media.rate == 5 * MB
        none = FakeEvent("traffic:set:up:0:today")
        await traffic_ui.handle_button(none, service)
        assert control.rate_mbps("upload") == 0
        week = FakeEvent("traffic:show:7d")
        await traffic_ui.handle_button(week, service)
        assert "近 7 天" in week.edits[-1]
        await service.stop()


def test_a_traffic_message_for_a_job_is_in_both_languages():
    from tgmd.i18n import t

    assert "流量闸门" in t("job.gated", lang="zh", prefix="", label="x")
    assert "traffic gate" in t("job.gated", lang="en", prefix="", label="x")
    assert Path  # keep the import honest


# ------------------------------------------------------------- the command


class CommandEvent:
    def __init__(self, user_id: int) -> None:
        self.raw_text = "/traffic"
        self.sender_id = user_id
        self.chat_id = user_id
        self.is_private = True
        self.replies: list[tuple[str, dict]] = []

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))


class TestTrafficCommand:
    async def handlers(self, tmp_path):
        from tgmd.handlers import BotHandlers

        config = Config()
        config.access.admin_user_ids = [1]
        config.access.allowed_user_ids = [2]
        handlers = BotHandlers(bot=object(), config=config, db=object(), queue=object(),
                               pikpak=object(), portal=object())
        tconfig = TrafficConfig(enabled=False, daily_report_at="")
        service = TrafficService(tconfig, TrafficStore(tmp_path / "t.sqlite3"),
                                 TrafficControl(tconfig))
        await service.start()
        return handlers, service

    async def test_an_admin_gets_today_with_buttons(self, tmp_path, chinese_ui):
        handlers, service = await self.handlers(tmp_path)
        handlers.attach_traffic(service)
        event = CommandEvent(1)
        await handlers.on_traffic(event)
        text, kwargs = event.replies[0]
        assert text.startswith("<b>代理流量（计费）  今天") and kwargs["buttons"] is not None
        await service.stop()

    async def test_a_member_who_is_not_an_admin_is_refused(self, tmp_path, chinese_ui):
        handlers, service = await self.handlers(tmp_path)
        handlers.attach_traffic(service)
        event = CommandEvent(2)
        await handlers.on_traffic(event)
        assert event.replies[0][0] == "只有管理员可以查看流量。"
        await service.stop()

    async def test_without_a_meter_it_says_so(self, tmp_path, chinese_ui):
        handlers, service = await self.handlers(tmp_path)
        event = CommandEvent(1)
        await handlers.on_traffic(event)
        assert event.replies[0][0] == "流量统计没有运行。"
        await service.stop()

    def test_the_command_is_registered(self):
        from tgmd.handlers import BotHandlers

        registered = []

        class Recorder:
            def add_event_handler(self, callback, event):
                registered.append((callback.__name__, getattr(event, "pattern", None)))

        BotHandlers(bot=Recorder(), config=Config(), db=object(), queue=object(),
                    pikpak=object(), portal=object()).register()
        names = [name for name, _ in registered]
        assert "on_traffic" in names and "handle_traffic_button" in names
        assert names.index("on_traffic") < names.index("on_message")


# -------------------------------------------------------------------- the CLI


class TestCommandLineReport:
    def seed(self, tmp_path):
        from tgmd.traffic.store import hour_of

        store = TrafficStore(tmp_path / "traffic.sqlite3")
        store.open()
        store.add_hours([((hour_of(datetime.now(UTC)), "telegram", "proxy", "n｜0.09元/G｜"),
                          1000, 2_000_000, 0.5)])
        store.close()

    def test_it_prints_the_same_report_as_text_and_json(self, tmp_path, monkeypatch, capsys):
        import json

        from tgmd.traffic.__main__ import main

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TRAFFIC_ENABLED", "0")
        monkeypatch.setenv("TRAFFIC_BUDGET_DAILY_CNY", "2")
        self.seed(tmp_path)
        assert main(["report", "--period", "today"]) == 0
        text = capsys.readouterr().out
        assert "Proxy traffic (billed)" in text and "<b>" not in text
        assert "today 1.9 MB ≈ ¥0.50" in text
        assert main(["report", "--period", "month", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["proxy"]["rows"][0]["node"] == "n｜0.09元/G｜"
        assert data["budgets"]["daily_cny"] == {"used": 0.5, "limit": 2.0}

    def test_without_a_database_it_says_so(self, tmp_path, monkeypatch, capsys):
        from tgmd.traffic.__main__ import main

        monkeypatch.setenv("DATA_DIR", str(tmp_path / "nowhere"))
        assert main(["report"]) == 1
        assert "no traffic data yet" in capsys.readouterr().err
