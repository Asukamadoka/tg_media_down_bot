"""M9.1 §C: two links and adaptive connections for the PikPak -> NAS download."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from wms_fakes import FakeDrive, provider_for

from pikpak_wms.core.client import WmsClient
from pikpak_wms.core.ratelimit import TokenBucket
from pikpak_wms.ops import fetch
from pikpak_wms.ops.fetch import FetchStats, Links

WEB = "https://dl-a10b-1553.mypikpak.com/web?sig=1"
ORIGIN = "https://dl-a10b-1532.mypikpak.com/origin?sig=2"
KIB = 1024


def payload(size: int) -> bytes:
    return bytes((i * 7 + i // 251) % 256 for i in range(size))


@pytest.fixture(autouse=True)
def small_ranges(monkeypatch):
    """Many small ranges, so there is always something left to hand to the faster link."""
    monkeypatch.setattr(fetch, "MIN_SEGMENT", 256 * KIB)
    monkeypatch.setattr(fetch, "SEGMENT", 256 * KIB)


class TwoLinkIO:
    """A server with two URLs for one file; each can be slower, wrong or expire."""

    def __init__(self, data: bytes, *, delays=None, chunk: int = 64 * KIB, origin=None) -> None:
        self.data = data
        self.bodies = {WEB: data, ORIGIN: data if origin is None else origin}
        self.delays = {WEB: 0.0, ORIGIN: 0.0, **(delays or {})}
        self.chunk = chunk
        self.served = {WEB: 0, ORIGIN: 0}
        self.probes: list[str] = []
        self.live = 0
        self.peak = 0
        self.samples: list[int] = []
        self.requests = 0
        self.expire_after: int | None = None
        self.valid = {WEB, ORIGIN}
        self.refreshed = 0
        self.fail_after: int | None = None
        self.error_on_request: tuple[int, Exception] | None = None
        self.chunks = 0
        self.after_error: float | None = None

    def rename(self, new_web: str, new_origin: str) -> None:
        self.bodies = {new_web: self.data, new_origin: self.data}
        self.delays = {new_web: 0.0, new_origin: 0.0}
        self.served.update({new_web: 0, new_origin: 0})
        self.valid = {new_web, new_origin}

    async def probe(self, url):
        self.probes.append(url)
        if url not in self.valid:
            raise fetch.LinkExpired("403")
        return len(self.bodies[url])

    async def stream(self, url, start, end):
        self.requests += 1
        if self.error_on_request and self.requests == self.error_on_request[0]:
            self.after_error = asyncio.get_running_loop().time()
            raise self.error_on_request[1]
        self.live += 1
        self.peak = max(self.peak, self.live)
        if self.after_error is not None:
            self.samples.append(self.live)
        try:
            body = self.bodies[url]
            for at in range(start, end if end is not None else len(body), self.chunk):
                if url not in self.valid:
                    raise fetch.LinkExpired("403")
                if self.expire_after is not None and self.chunks >= self.expire_after:
                    self.expire_after = None
                    self.refreshed += 1
                    self.rename(WEB + "&n=2", ORIGIN + "&n=2")
                    raise fetch.LinkExpired("403")
                if self.fail_after is not None and self.chunks >= self.fail_after:
                    raise ConnectionResetError("boom")
                self.chunks += 1
                await asyncio.sleep(self.delays.get(url, 0.0))
                piece = body[at:min(at + self.chunk, end if end is not None else len(body))]
                self.served[url] = self.served.get(url, 0) + len(piece)
                yield piece
        finally:
            self.live -= 1


class Source:
    """``url_for`` returning both links and counting how often it is asked."""

    def __init__(self, io: TwoLinkIO | None = None) -> None:
        self.calls = 0
        self.io = io

    async def __call__(self):
        self.calls += 1
        if self.io is not None and self.io.refreshed:
            return Links(WEB + "&n=2", ORIGIN + "&n=2")
        return Links(WEB, ORIGIN)


async def no_sleep(_seconds):
    return None


FAST = {"rebalance_after": 0.04, "adapt_every": 0.05, "tick": 0.005}


class TestTheOriginLink:
    async def test_an_identical_origin_is_used_and_the_file_is_exact(self, tmp_path):
        data = payload(4 * 1024 * KIB)
        io = TwoLinkIO(data, delays={WEB: 0.001, ORIGIN: 0.001})
        stats = FetchStats()
        part = tmp_path / "a.part"
        assert await fetch.download(Source(), io, part, connections=4, stats=stats, **FAST) \
            == len(data)
        assert part.read_bytes() == data
        assert stats.links == "web+origin" and io.served[WEB] > 0 and io.served[ORIGIN] > 0
        assert stats.hosts == ["dl-a10b-1553.mypikpak.com", "dl-a10b-1532.mypikpak.com"]
        assert hashlib.sha256(part.read_bytes()).digest() == hashlib.sha256(data).digest()

    async def test_an_origin_of_a_different_size_is_ignored_with_a_reason(self, tmp_path):
        data = payload(2 * 1024 * KIB)
        io = TwoLinkIO(data, origin=data[:-100])
        stats = FetchStats()
        await fetch.download(Source(), io, tmp_path / "b.part", connections=4, stats=stats, **FAST)
        assert (tmp_path / "b.part").read_bytes() == data
        assert stats.links == "web" and io.served[ORIGIN] == 0
        assert "size" in stats.origin_note

    async def test_an_origin_with_different_first_bytes_is_ignored(self, tmp_path):
        data = payload(2 * 1024 * KIB)
        other = b"X" + data[1:]
        io = TwoLinkIO(data, origin=other)
        stats = FetchStats()
        await fetch.download(Source(), io, tmp_path / "c.part", connections=4, stats=stats, **FAST)
        assert (tmp_path / "c.part").read_bytes() == data
        assert stats.links == "web" and io.served[ORIGIN] <= 256 * KIB   # only the check read it
        assert "content" in stats.origin_note

    async def test_without_an_origin_link_it_is_the_old_behaviour(self, tmp_path):
        data = payload(1024 * KIB)
        io = TwoLinkIO(data)

        async def only_web():
            return WEB

        stats = FetchStats()
        await fetch.download(only_web, io, tmp_path / "d.part", connections=4, stats=stats, **FAST)
        assert stats.links == "web" and stats.origin_note == "" and io.served[ORIGIN] == 0

    async def test_a_broken_origin_check_falls_back_to_the_web_link(self, tmp_path):
        data = payload(1024 * KIB)
        io = TwoLinkIO(data)
        original = io.probe

        async def probe(url):
            if url == ORIGIN:
                raise OSError("reset")
            return await original(url)

        io.probe = probe
        stats = FetchStats()
        await fetch.download(Source(), io, tmp_path / "e.part", connections=4, stats=stats, **FAST)
        assert (tmp_path / "e.part").read_bytes() == data and stats.links == "web"
        assert "could not be checked" in stats.origin_note

    async def test_only_the_urls_the_api_returned_are_ever_requested(self, tmp_path):
        data = payload(1024 * KIB)
        io = TwoLinkIO(data)
        requested = []
        original = io.stream

        def stream(url, start, end):
            requested.append(url)
            return original(url, start, end)

        io.stream = stream
        await fetch.download(Source(), io, tmp_path / "f.part", connections=4, **FAST)
        assert set(requested) <= {WEB, ORIGIN}


class TestRebalancing:
    async def test_pending_ranges_move_to_the_faster_link(self, tmp_path):
        data = payload(6 * 1024 * KIB)
        io = TwoLinkIO(data, delays={WEB: 0.006, ORIGIN: 0.0005})
        await fetch.download(Source(), io, tmp_path / "g.part", connections=4, **FAST)
        assert (tmp_path / "g.part").read_bytes() == data
        assert io.served[ORIGIN] > 2 * io.served[WEB]

    async def test_equal_links_share_the_work(self, tmp_path):
        data = payload(4 * 1024 * KIB)
        io = TwoLinkIO(data, delays={WEB: 0.001, ORIGIN: 0.001})
        await fetch.download(Source(), io, tmp_path / "h.part", connections=4, **FAST)
        low, high = sorted(io.served.values())
        assert low > 0.3 * high


class TestRampAndBackOff:
    async def test_connections_ramp_up_by_four_while_speed_keeps_rising(self, tmp_path):
        data = payload(24 * 1024 * KIB)
        io = TwoLinkIO(data, delays={WEB: 0.003, ORIGIN: 0.003})
        stats = FetchStats()
        await fetch.download(Source(), io, tmp_path / "i.part", connections=4, max_connections=12,
                             stats=stats, **FAST)
        assert (tmp_path / "i.part").read_bytes() == data
        assert 8 <= stats.peak_connections <= 12 and io.peak <= 12

    async def test_it_never_goes_past_the_ceiling(self, tmp_path):
        data = payload(16 * 1024 * KIB)
        io = TwoLinkIO(data, delays={WEB: 0.002, ORIGIN: 0.002})
        stats = FetchStats()
        await fetch.download(Source(), io, tmp_path / "j.part", connections=4, max_connections=6,
                             stats=stats, **FAST)
        assert stats.peak_connections <= 6

    async def test_the_hard_cap_is_32(self, tmp_path):
        data = payload(2 * 1024 * KIB)
        io = TwoLinkIO(data)
        stats = FetchStats()
        await fetch.download(Source(), io, tmp_path / "k.part", connections=4, max_connections=500,
                             stats=stats, **FAST)
        assert stats.peak_connections <= fetch.MAX_CONNECTIONS_HARD == 32

    async def test_http_429_drops_four_connections(self, tmp_path):
        class TooMany(Exception):
            status = 429

        data = payload(12 * 1024 * KIB)
        io = TwoLinkIO(data, delays={WEB: 0.002, ORIGIN: 0.002})
        io.error_on_request = (10, TooMany())
        stats = FetchStats()
        await fetch.download(Source(), io, tmp_path / "l.part", connections=8, sleep=no_sleep,
                             stats=stats, rebalance_after=0.04, adapt_every=100.0, tick=0.005)
        assert (tmp_path / "l.part").read_bytes() == data
        assert stats.peak_connections == 8
        assert io.samples and max(io.samples[len(io.samples) // 2:]) <= 4

    async def test_repeated_resets_also_back_off(self, tmp_path):
        data = payload(12 * 1024 * KIB)
        io = TwoLinkIO(data, delays={WEB: 0.002, ORIGIN: 0.002})
        resets = {"left": 2}
        original = io.stream

        def stream(url, start, end):
            if resets["left"] and io.requests >= 8:
                resets["left"] -= 1
                io.requests += 1
                io.after_error = asyncio.get_event_loop().time()

                async def broken():
                    raise ConnectionResetError("reset")
                    yield b""

                return broken()
            return original(url, start, end)

        io.stream = stream
        await fetch.download(Source(), io, tmp_path / "m.part", connections=8, sleep=no_sleep,
                             rebalance_after=0.04, adapt_every=100.0, tick=0.005)
        assert (tmp_path / "m.part").read_bytes() == data
        assert max(io.samples[len(io.samples) // 2:]) <= 4


class TestRelinkAndResume:
    async def test_relinking_asks_for_both_links_again_and_checks_them(self, tmp_path):
        data = payload(4 * 1024 * KIB)
        io = TwoLinkIO(data, delays={WEB: 0.001, ORIGIN: 0.001})
        io.expire_after = 12
        source = Source(io)
        stats = FetchStats()
        await fetch.download(source, io, tmp_path / "n.part", connections=4, stats=stats, **FAST)
        assert (tmp_path / "n.part").read_bytes() == data
        assert source.calls >= 2
        assert ORIGIN + "&n=2" in io.probes and WEB + "&n=2" in io.probes
        assert stats.links == "web+origin" and io.served[ORIGIN + "&n=2"] > 0

    async def test_a_download_cut_short_resumes_from_the_state_file(self, tmp_path):
        data = payload(4 * 1024 * KIB)
        io = TwoLinkIO(data, delays={WEB: 0.001, ORIGIN: 0.001})
        io.fail_after = 20
        part = tmp_path / "o.part"
        with pytest.raises(ConnectionResetError):
            await fetch.download(Source(), io, part, connections=4, sleep=no_sleep, **FAST)
        assert part.exists() and fetch.state_path(part).exists()
        done = sum(done for _, _, done in json.loads(fetch.state_path(part).read_text())["ranges"])
        assert 0 < done < len(data)
        again = TwoLinkIO(data, delays={WEB: 0.001, ORIGIN: 0.001})
        assert await fetch.download(Source(), again, part, connections=4, **FAST) == len(data)
        assert part.read_bytes() == data
        assert sum(again.served.values()) < len(data)           # only what was missing
        assert not fetch.state_path(part).exists()

    async def test_a_server_without_ranges_still_gets_one_plain_stream(self, tmp_path):
        data = payload(300 * KIB)

        class Plain:
            async def probe(self, url):
                return None

            async def stream(self, url, start, end):
                assert (start, end) == (0, None)
                yield data

        got = await fetch.download(Source(), Plain(), tmp_path / "p.part", connections=4)
        assert got == len(data) and (tmp_path / "p.part").read_bytes() == data


class TestPlanning:
    def test_a_big_file_gets_more_ranges_than_connections(self, monkeypatch):
        monkeypatch.setattr(fetch, "MIN_SEGMENT", 4 * 1024 * 1024)
        monkeypatch.setattr(fetch, "SEGMENT", 64 * 1024 * 1024)
        gib = 1024 * 1024 * 1024
        assert fetch._range_count(16 * gib, 12) == 256                  # noqa: SLF001
        assert fetch._range_count(20 * 1024 * 1024, 4) == 4             # noqa: SLF001
        assert fetch._range_count(100_000, 8) == 1                      # noqa: SLF001

    def test_stats_report_the_average(self):
        stats = FetchStats(bytes=50 * 1024 * 1024, seconds=10, peak_connections=12,
                           links="web+origin", hosts=["a", "b"])
        assert stats.as_dict() == {"avg_mib_s": 5.0, "links": "web+origin",
                                   "peak_connections": 12, "hosts": ["a", "b"]}


class TestBench:
    async def test_it_measures_each_connection_count_and_discards_the_bytes(self, tmp_path):
        data = payload(8 * 1024 * KIB)
        io = TwoLinkIO(data, delays={WEB: 0.002})
        one = await fetch.bench(io, WEB, len(data), connections=1, seconds=0.2)
        four = await fetch.bench(io, WEB, len(data), connections=4, seconds=0.2)
        assert four > one > 0
        assert list(tmp_path.iterdir()) == []


class TestLinksFromPikPak:
    async def make(self, response):
        drive = FakeDrive()

        drive.download_info = lambda file_id: response
        return WmsClient(provider_for(drive), limiter=TokenBucket(1e9, 1_000_000), sleep=no_sleep)

    async def test_web_and_origin_come_from_one_call(self):
        client = await self.make({
            "web_content_link": WEB,
            "medias": [{"is_origin": False, "link": {"url": "https://x/transcoded"}},
                       {"is_origin": True, "link": {"url": ORIGIN}}]})
        assert await client.download_links("id") == (WEB, ORIGIN)
        assert await client.download_url("id") == WEB

    async def test_no_origin_means_none(self):
        client = await self.make({"web_content_link": WEB, "medias": []})
        assert await client.download_links("id") == (WEB, None)

    async def test_a_media_link_stands_in_when_there_is_no_web_link(self):
        client = await self.make({"medias": [{"is_origin": True, "link": {"url": ORIGIN}}]})
        assert await client.download_links("id") == (ORIGIN, None)

    async def test_no_link_at_all_is_an_error(self):
        from pikpak_wms.core.errors import WmsError

        client = await self.make({"medias": []})
        with pytest.raises(WmsError):
            await client.download_links("id")


class TestReporting:
    def test_the_progress_message_shows_average_connections_and_sources(self):
        from pikpak_wms.config import Config
        from pikpak_wms.i18n import set_language
        from pikpak_wms.ops.runs import Run
        from tgmd import i18n
        from tgmd.config import Config as BotConfig
        from tgmd.wms import WmsInBot

        previous = i18n.language()
        i18n.set_language("zh")
        set_language("zh")
        try:
            run = Run(plan_id=7, total=3, done=1)
            run.note_bytes("big.mkv", 10 * 1024 * 1024, 100 * 1024 * 1024)
            run.note_info(12, "web+origin")
            run.file_started -= 2.0
            wms = WmsInBot(BotConfig(), SimpleNamespace())
            text = wms.progress_text(run)
            assert "平均 5.0 MB/s · 连接 12 · 源 web+origin" in text
            assert Config  # imported for parity with the other WMS tests
        finally:
            i18n.set_language(previous)
            set_language(None)

    def test_the_final_summary_lists_each_file(self):
        from pikpak_wms.ops.plans import ApplyReport
        from pikpak_wms.ops.runs import Run
        from tgmd import i18n
        from tgmd.config import Config as BotConfig
        from tgmd.wms import WmsInBot

        previous = i18n.language()
        i18n.set_language("zh")
        try:
            report = ApplyReport(plan_id=7, applied=1)
            report.fetches.append({"path": "/Media/big.mkv", "avg_mib_s": 6.42,
                                   "peak_connections": 16, "links": "web+origin"})
            run = Run(plan_id=7, total=1, done=1)
            run.report = report
            text, _ = WmsInBot(BotConfig(), SimpleNamespace()).finished_message(run)
            assert "⬇ big.mkv：平均 6.4 MB/s · 连接 16 · 源 web+origin" in text
            assert report.to_result()["fetches"][0]["links"] == "web+origin"
        finally:
            i18n.set_language(previous)

    def test_the_default_connection_count_is_12_with_a_ceiling_of_16(self, monkeypatch):
        from pikpak_wms.config import OutboundConfig

        config = OutboundConfig()
        assert (config.parallel, config.max_parallel) == (12, 16)
        monkeypatch.setenv("OUTBOUND_CONNECTIONS", "20")
        monkeypatch.setenv("OUTBOUND_MAX_CONNECTIONS", "99")
        assert (config.parallel, config.max_parallel) == (20, 32)
        monkeypatch.setenv("OUTBOUND_MAX_CONNECTIONS", "4")
        assert config.max_parallel == 20            # never below where it starts
