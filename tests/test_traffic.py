"""M9: the traffic meter: prices, classification, delta accounting, storage,
budgets, alerts and the report (docs/wms/M9 §A–C, §F)."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import pytest

from tgmd import i18n
from tgmd.config import TrafficConfig
from tgmd.traffic import TrafficControl, TrafficService
from tgmd.traffic.classify import classify
from tgmd.traffic.meter import Meter
from tgmd.traffic.mihomo import MihomoClient
from tgmd.traffic.pricing import parse_price, short_node
from tgmd.traffic.report import Budgets, build_report, fmt_bytes, render
from tgmd.traffic.store import TrafficStore, hour_of, period_for

SH = ZoneInfo("Asia/Shanghai")
TG_NODE = "😈英格兰002｜0.09元/G｜hy2｜"
MODEL_IP = "10.0.0.1"  # a placeholder LAN model host
JP_NODE = "🖤东京京X06｜0.01元/G｜Reality｜"
MB = 1024 * 1024


@pytest.fixture
def chinese():
    previous = i18n.language()
    i18n.set_language("zh")
    yield
    i18n.set_language(previous)


def conn(conn_id, host="example.com", *, up=0, down=0, chains=("DIRECT",), ip="", port="443",
         rule=""):
    return {
        "id": conn_id,
        "metadata": {"host": host, "destinationIP": ip, "destinationPort": port,
                     "network": "tcp"},
        "upload": up,
        "download": down,
        "chains": list(chains),
        "rule": rule,
    }


def snap(conns, up=None, down=None):
    return {
        "uploadTotal": sum(c["upload"] for c in conns) if up is None else up,
        "downloadTotal": sum(c["download"] for c in conns) if down is None else down,
        "connections": conns,
    }


# ------------------------------------------------------------------- prices


class TestPricing:
    @pytest.mark.parametrize(("name", "price"), [
        (JP_NODE, 0.01),
        (TG_NODE, 0.09),
        ("🇨🇦加拿大01｜0.07元/G｜vless｜", 0.07),
        ("node 1元/G", 1.0),
        ("node 0.5 元 / G", 0.5),
        ("日本｜０．０２元／Ｇ｜", 0.02),  # full-width digits, stop and slash
        ("node 0.05元/g", 0.05),
    ])
    def test_the_price_is_read_from_the_name(self, name, price):
        assert parse_price(name) == price

    @pytest.mark.parametrize("name", ["DIRECT", "TG-OTHER", "", "香港 01｜Reality｜"])
    def test_a_name_without_a_price_has_none(self, name):
        assert parse_price(name) is None

    def test_short_names_drop_the_decoration_and_keep_the_price(self):
        assert short_node(TG_NODE) == "英格兰002 0.09元/G"
        assert short_node("🖤东京京X06｜Reality｜") == "东京京X06"
        assert short_node("") == ""


# ----------------------------------------------------------------- classify


class TestClassify:
    def test_telegram_by_group(self):
        info = classify(conn("1", "149.154.167.50", chains=[TG_NODE, "TG-OTHER", "TG"]))
        assert (info.category, info.outbound) == ("telegram", "proxy")
        assert info.node == TG_NODE and info.group == "TG-OTHER" and info.price == 0.09

    def test_telegram_by_domain_and_by_cidr(self):
        assert classify(conn("1", "web.telegram.org", chains=["PROXY"])).category == "telegram"
        by_ip = conn("2", "", ip="91.108.56.130", chains=["DIRECT"])
        assert classify(by_ip).category == "telegram"

    def test_pikpak_downloads_are_direct(self):
        info = classify(conn("1", "dl-a10b-123.mypikpak.com"))
        assert (info.category, info.outbound, info.leak) == ("pikpak", "direct", False)

    def test_pikpak_through_the_proxy_is_a_route_leak(self):
        info = classify(conn("1", "api-drive.mypikpak.com", chains=[JP_NODE, "PROXY"]))
        assert (info.category, info.outbound, info.leak) == ("pikpak", "proxy", True)

    def test_models_are_ollama_hosts_and_the_mac(self):
        pull = classify(conn("1", "registry.ollama.ai", chains=[JP_NODE, "PROXY"]))
        assert (pull.category, pull.leak) == ("model", False)  # a billed pull, not a leak
        assert classify(conn("2", "x.r2.cloudflarestorage.com")).category == "model"
        mac = classify(conn("3", "", ip=MODEL_IP, port="11434"), (MODEL_IP, 11434))
        assert (mac.category, mac.outbound) == ("model", "direct")
        # With no model host configured, nothing is built in: the address is just a LAN one.
        assert classify(conn("3", "", ip=MODEL_IP, port="11434")).category == "lan"

    def test_the_mac_through_the_proxy_is_a_route_leak(self):
        mac = classify(conn("3", "", ip=MODEL_IP, port="11434", chains=[JP_NODE, "PROXY"]),
                       (MODEL_IP, 11434))
        assert mac.leak is True

    def test_lan_and_loopback(self):
        assert classify(conn("1", "", ip="192.168.0.5")).category == "lan"
        assert classify(conn("2", "", ip="127.0.0.1")).category == "lan"
        assert classify(conn("3", "", ip="192.168.0.5", chains=["PROXY", JP_NODE])).leak is True

    def test_the_subscription_host(self):
        assert classify(conn("1", "sub.example.invalid"),
                        sub_hosts=("example.invalid",)).category == "proxy-sub"
        # No domain is built in: without SUB_HOSTS it is an ordinary host.
        assert classify(conn("1", "sub.example.invalid")).category == "other"

    def test_everything_else(self):
        info = classify(conn("1", "example.com", chains=[JP_NODE, "PROXY"]))
        assert (info.category, info.outbound, info.leak) == ("other", "proxy", False)

    def test_the_node_is_found_whichever_end_of_the_chain_it_is_on(self):
        a = classify(conn("1", "x.com", chains=[JP_NODE, "PROXY"]))
        b = classify(conn("2", "x.com", chains=["PROXY", JP_NODE]))
        assert a.node == b.node == JP_NODE and a.group == b.group == "PROXY"


# -------------------------------------------------------------------- meter


class TestMeter:
    def test_the_first_snapshot_only_sets_the_baseline(self):
        step = Meter().ingest(snap([conn("a", up=500, down=9000, chains=[JP_NODE, "PROXY"])]))
        assert step.primed is False and step.deltas == []

    def test_a_growing_connection_adds_its_difference(self):
        meter = Meter()
        meter.ingest(snap([conn("a", up=10, down=100)]))
        step = meter.ingest(snap([conn("a", up=15, down=400)]))
        assert [(d.up, d.down, d.total) for d in step.deltas] == [(5, 300, 415)]
        assert (step.unattributed_up, step.unattributed_down) == (0, 0)

    def test_a_new_connection_counts_in_full(self):
        meter = Meter()
        meter.ingest(snap([]))
        step = meter.ingest(snap([conn("b", up=1, down=2000)]))
        assert [(d.up, d.down) for d in step.deltas] == [(1, 2000)]

    def test_bytes_before_a_connection_vanished_are_not_lost_to_the_total(self):
        meter = Meter()
        meter.ingest(snap([conn("a", up=0, down=100)]))
        # "a" moved 900 more and closed between polls; the global total shows it.
        step = meter.ingest(snap([], up=0, down=1000))
        assert step.deltas == []
        assert step.unattributed_down == 900

    def test_a_reappearing_connection_with_a_new_id_is_a_new_connection(self):
        meter = Meter()
        meter.ingest(snap([conn("a", down=100)]))
        meter.ingest(snap([conn("a", down=150)]))
        step = meter.ingest(snap([conn("z", down=40)], up=0, down=190))
        assert [(d.conn_id, d.down) for d in step.deltas] == [("z", 40)]

    def test_a_counter_that_goes_backwards_means_the_proxy_restarted(self, caplog):
        meter = Meter()
        meter.ingest(snap([conn("a", down=1_000_000)]))
        with caplog.at_level(logging.WARNING):
            step = meter.ingest(snap([conn("n", down=700)]))
            again = meter.ingest(snap([conn("n", down=900)]))
        assert step.reset is True and [d.down for d in step.deltas] == [700]
        assert step.unattributed_down == 0
        assert again.reset is False and [d.down for d in again.deltas] == [200]
        assert sum("restarted" in r.message for r in caplog.records) == 1

    def test_one_connection_going_backwards_is_counted_from_zero(self):
        meter = Meter()
        meter.ingest(snap([conn("a", down=500)], up=0, down=500))
        step = meter.ingest(snap([conn("a", down=80)], up=0, down=600))
        assert [d.down for d in step.deltas] == [80]

    def test_the_remainder_is_what_the_connections_did_not_show(self):
        meter = Meter()
        meter.ingest(snap([conn("a", up=10, down=10)]))
        step = meter.ingest(snap([conn("a", up=20, down=110)], up=50, down=500))
        assert (step.unattributed_up, step.unattributed_down) == (30, 390)


# -------------------------------------------------------------------- store


@pytest.fixture
def store(tmp_path):
    handle = TrafficStore(tmp_path / "traffic.sqlite3")
    handle.open()
    yield handle
    handle.close()


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestStore:
    def test_buckets_accumulate_and_are_keyed_by_hour(self, store):
        h = hour_of(utc(2026, 10, 2, 3, 20))
        store.add_hours([((h, "telegram", "proxy", TG_NODE), 10, 100, 0.5)])
        store.add_hours([((h, "telegram", "proxy", TG_NODE), 5, 50, 0.25),
                         ((h + 3600, "telegram", "proxy", TG_NODE), 1, 1, 0.01)])
        period = period_for("today", utc(2026, 10, 2, 3, 30), SH)
        (row,) = store.rows(period)
        assert (row.up, row.down, round(row.cost, 2)) == (16, 151, 0.76)

    def test_the_hour_rolls_over_at_the_top_of_the_hour(self):
        assert hour_of(utc(2026, 10, 2, 3, 59, 59)) + 3600 == hour_of(utc(2026, 10, 2, 4, 0, 0))

    def test_a_shanghai_day_starts_at_16_00_utc(self, store):
        before = hour_of(utc(2026, 10, 1, 15, 30))   # 23:30 on the 1st in Shanghai
        after = hour_of(utc(2026, 10, 1, 16, 30))    # 00:30 on the 2nd
        store.add_hours([((before, "other", "proxy", "n"), 0, 1000, 0),
                         ((after, "other", "proxy", "n"), 0, 10, 0)])
        today = period_for("today", utc(2026, 10, 1, 17, 0), SH)
        assert [r.down for r in store.rows(today)] == [10]
        yesterday = period_for("yesterday", utc(2026, 10, 1, 17, 0), SH)
        assert [r.down for r in store.rows(yesterday)] == [1000]

    def test_a_month_runs_on_shanghai_dates(self, store):
        end_of_sept = hour_of(utc(2026, 9, 30, 15, 30))   # 23:30 on 30 Sept, Shanghai
        start_of_oct = hour_of(utc(2026, 9, 30, 16, 30))  # 00:30 on 1 Oct
        store.add_hours([((end_of_sept, "other", "proxy", "n"), 0, 7, 0),
                         ((start_of_oct, "other", "proxy", "n"), 0, 3, 0)])
        month = period_for("month", utc(2026, 10, 15), SH)
        assert [r.down for r in store.rows(month)] == [3]
        assert period_for("month", utc(2026, 12, 15), SH).end.astimezone(SH).month == 1

    def test_seven_days_are_today_and_the_six_before(self):
        span = period_for("7d", utc(2026, 10, 2, 3), SH)
        assert span.days == ("2026-09-26", "2026-10-02")

    def test_old_rows_are_pruned(self, store):
        now = utc(2026, 10, 2, 3)
        old = hour_of(now - timedelta(days=401))
        recent = hour_of(now - timedelta(days=399))
        store.add_hours([((old, "other", "proxy", "n"), 0, 1, 0),
                         ((recent, "other", "proxy", "n"), 0, 1, 0)])
        store.add_hosts([(("2026-06-01", "old.example", "other", "proxy", ""), 5 * MB),
                         (("2026-09-01", "new.example", "other", "proxy", ""), 5 * MB)])
        store.prune(now, SH)
        everything = period_for("month", now, SH)
        assert store.rows(everything) == []
        epoch = type(everything)("all", utc(2020, 1, 1), utc(2030, 1, 1), SH)
        assert [r.down for r in store.rows(epoch)] == [1]
        assert [h[0] for h in store.top_hosts(period_for("7d", utc(2026, 9, 2), SH))] == [
            "new.example"]
        assert store.top_hosts(period_for("7d", utc(2026, 6, 2), SH)) == []


# ------------------------------------------------------------------ service


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def make_service(tmp_path, store, clock, **settings):
    config = TrafficConfig(
        enabled=False, bytes_per_gb=1_000_000, default_price=0.10, daily_report_at="",
        conn_alert_mb=0, **settings,
    )
    control = TrafficControl(config)
    sent: list[str] = []

    async def notify(text):
        sent.append(text)

    service = TrafficService(config, store, control, notify=notify, clock=clock)
    return service, control, sent


class Feed:
    """Cumulative snapshots of one proxied Telegram connection."""

    def __init__(self, service, clock, *, host="149.154.167.50", node=TG_NODE) -> None:
        self.service, self.clock = service, clock
        self.chains = [node, "TG-OTHER", "TG"]
        self.host, self.total = host, 0

    async def send(self, nbytes: int, *, seconds: int = 5):
        self.clock.advance(seconds=seconds)
        self.total += nbytes
        await self.service.ingest(snap([conn("c1", self.host, down=self.total,
                                             chains=self.chains)]), self.clock())


async def started(service):
    await service.start()
    # The first snapshot sets the baseline.
    await service.ingest(snap([]), service._clock())  # noqa: SLF001


class TestAccounting:
    async def test_bytes_land_in_the_right_bucket_with_a_price(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 3, 0))
        service, _control, _sent = make_service(tmp_path, store, clock)
        await started(service)
        feed = Feed(service, clock)
        await feed.send(2_000_000)
        await service.flush()
        (row,) = store.rows(period_for("today", clock(), SH))
        assert (row.category, row.outbound, row.node, row.down) == (
            "telegram", "proxy", TG_NODE, 2_000_000)
        assert row.cost == pytest.approx(2 * 0.09)

    async def test_a_node_without_a_price_uses_the_default(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 3, 0))
        service, _control, _sent = make_service(tmp_path, store, clock)
        await started(service)
        await Feed(service, clock, node="unpriced node").send(1_000_000)
        await service.flush()
        (row,) = store.rows(period_for("today", clock(), SH))
        assert row.cost == pytest.approx(0.10)

    async def test_direct_traffic_costs_nothing(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 3, 0))
        service, _control, _sent = make_service(tmp_path, store, clock)
        await started(service)
        clock.advance(seconds=5)
        await service.ingest(snap([conn("d", "dl-a10b-123.mypikpak.com", down=5_000_000)]),
                             clock())
        await service.flush()
        (row,) = store.rows(period_for("today", clock(), SH))
        assert (row.category, row.outbound, row.cost) == ("pikpak", "direct", 0)

    async def test_the_unattributed_remainder_is_stored_and_not_priced(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 3, 0))
        service, _control, _sent = make_service(tmp_path, store, clock)
        await started(service)
        clock.advance(seconds=5)
        await service.ingest(snap([], up=0, down=777), clock())
        await service.flush()
        (row,) = store.rows(period_for("today", clock(), SH))
        assert (row.category, row.outbound, row.down, row.cost) == (
            "unattributed", "unknown", 777, 0)

    async def test_hosts_are_kept_only_above_one_megabyte_a_day(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 3, 0))
        service, _control, _sent = make_service(tmp_path, store, clock)
        await started(service)
        await Feed(service, clock, host="small.example").send(500_000)
        await Feed(service, clock, host="big.example").send(3 * MB)
        await service.flush()
        hosts = store.top_hosts(period_for("today", clock(), SH))
        assert [h[0] for h in hosts] == ["big.example"]

    async def test_a_new_day_starts_the_totals_again(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 1, 15, 59, 0))   # 23:59 on the 1st in Shanghai
        service, control, _sent = make_service(tmp_path, store, clock, budget_daily_cny=1.0)
        await started(service)
        await Feed(service, clock).send(12_000_000, seconds=1)  # ¥1.08: over
        assert control.state == "over_budget"
        clock.advance(minutes=2)                      # 00:01 on the 2nd
        await service.ingest(snap([]), clock())
        assert service._spend.day_cost == 0           # noqa: SLF001
        assert control.state == "open"                # the new period clears it
        await service.flush()
        yesterday = store.rows(period_for("yesterday", clock(), SH))
        assert sum(r.cost for r in yesterday) == pytest.approx(1.08)


class TestBudgets:
    async def test_alerts_at_80_and_100_percent_once_each(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 3, 0))
        service, control, sent = make_service(tmp_path, store, clock, budget_daily_cny=1.0)
        await started(service)
        feed = Feed(service, clock)
        await feed.send(5_000_000)              # ¥0.45
        assert sent == [] and control.state == "open"
        await feed.send(4_000_000)              # ¥0.81 -> 80%
        await feed.send(500_000)                # still 80%, no repeat
        assert len(sent) == 1 and "80%" in sent[0] and control.state == "open"
        await feed.send(3_000_000)              # ¥1.08 -> 100%
        await feed.send(1_000_000)
        assert len(sent) == 2 and "100%" in sent[1]
        assert control.state == "over_budget"

    async def test_warn_mode_alerts_but_never_pauses(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 3, 0))
        service, control, sent = make_service(tmp_path, store, clock, budget_daily_cny=1.0,
                                              on_budget="warn")
        await started(service)
        await Feed(service, clock).send(12_000_000)
        assert control.state == "open"
        assert any("100%" in text for text in sent)

    async def test_the_monthly_and_the_volume_budgets(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 3, 0))
        service, control, sent = make_service(
            tmp_path, store, clock, budget_monthly_cny=0.5, budget_daily_proxy_gb=0.004)
        await started(service)
        await Feed(service, clock).send(5_000_000)   # ¥0.45 (90% of month), 5 "GB"-units
        texts = " ".join(sent)
        assert "80%" in texts and "100%" in texts
        assert control.state == "over_budget"

    async def test_raising_the_budget_clears_the_gate(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 3, 0))
        service, control, _sent = make_service(tmp_path, store, clock, budget_daily_cny=1.0)
        await started(service)
        await Feed(service, clock).send(12_000_000)
        assert control.state == "over_budget"
        service._config.budget_daily_cny = 5.0  # noqa: SLF001 - what a restart with a new env does
        await service.ingest(snap([]), clock())
        assert control.state == "open"

    async def test_resuming_by_hand_is_not_undone_by_the_same_budget(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 3, 0))
        service, control, _sent = make_service(tmp_path, store, clock, budget_daily_cny=1.0)
        await started(service)
        feed = Feed(service, clock)
        await feed.send(12_000_000)
        await control.resume()
        await feed.send(1_000_000)
        assert control.state == "open"

    async def test_nothing_happens_without_a_budget(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 3, 0))
        service, control, sent = make_service(tmp_path, store, clock)
        await started(service)
        await Feed(service, clock).send(500_000_000)
        assert sent == [] and control.state == "open"

    async def test_alerts_already_sent_survive_a_restart(self, tmp_path, store):
        from tgmd.db import Database

        db = Database(tmp_path / "bot.sqlite3")
        await db.connect()
        clock = Clock(utc(2026, 10, 2, 3, 0))
        config = TrafficConfig(enabled=False, bytes_per_gb=1_000_000, daily_report_at="",
                               budget_daily_cny=1.0)
        first = TrafficControl(config, db)
        await first.load()
        service = TrafficService(config, store, first, clock=clock)
        await started(service)
        await Feed(service, clock).send(12_000_000)
        await service.flush()
        second = TrafficControl(config, db)
        await second.load()
        assert second.state == "over_budget"
        assert await second.claim_alert("budget:daily_cny:100:2026-10-02", "2026-10-02") is False
        await db.close()


class TestAlerts:
    async def test_a_single_heavy_proxied_connection(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 3, 0))
        service, _control, sent = make_service(tmp_path, store, clock)
        service._config.conn_alert_mb = 1  # noqa: SLF001
        await started(service)
        feed = Feed(service, clock, host="media.example")
        await feed.send(2 * MB)
        await feed.send(2 * MB)
        assert len(sent) == 1
        assert "media.example" in sent[0] and "英格兰002" in sent[0]

    async def test_a_route_leak_is_reported_once_per_host_per_day(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 3, 0))
        service, _control, sent = make_service(tmp_path, store, clock)
        await started(service)
        for step in range(3):
            clock.advance(seconds=5)
            leak = conn("l", "api-drive.mypikpak.com", down=1000 * (step + 1),
                        chains=[JP_NODE, "PROXY"])
            await service.ingest(snap([leak]), clock())
        assert len(sent) == 1 and "api-drive.mypikpak.com" in sent[0]
        clock.advance(days=1)
        await service.ingest(snap([conn("l", "api-drive.mypikpak.com", down=9000,
                                        chains=[JP_NODE, "PROXY"])]), clock())
        assert len(sent) == 2

    async def test_a_sustained_spike_is_reported_once(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 3, 0))
        service, _control, sent = make_service(tmp_path, store, clock, spike_mbps=1.0)
        await started(service)
        feed = Feed(service, clock, host="firehose.example")
        for _ in range(20):                      # 200 s at 2 MB/s: not yet 5 minutes
            await feed.send(20 * MB, seconds=10)
        assert sent == []
        for _ in range(15):
            await feed.send(20 * MB, seconds=10)
        spikes = [text for text in sent if "firehose.example" in text]
        assert len(spikes) == 1

    async def test_a_brief_burst_is_not_a_spike(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 3, 0))
        service, _control, sent = make_service(tmp_path, store, clock, spike_mbps=1.0)
        await started(service)
        feed = Feed(service, clock)
        await feed.send(100 * MB, seconds=10)
        for _ in range(40):
            await feed.send(0, seconds=10)
        assert sent == []


class TestDailySummary:
    async def test_it_is_sent_once_for_yesterday(self, tmp_path, store, chinese):
        clock = Clock(utc(2026, 10, 1, 4, 0))      # 12:00 on the 1st, Shanghai
        service, control, sent = make_service(tmp_path, store, clock)
        service._config.daily_report_at = "09:00"  # noqa: SLF001
        await started(service)
        await Feed(service, clock).send(3_000_000)
        clock.now = utc(2026, 10, 2, 0, 30)        # 08:30 on the 2nd: too early
        assert await service.maybe_report(clock()) is False
        clock.now = utc(2026, 10, 2, 1, 5)         # 09:05
        assert await service.maybe_report(clock()) is True
        assert await service.maybe_report(clock()) is False
        assert len(sent) == 1
        assert sent[0].startswith("<b>昨日代理流量</b>\n<b>代理流量（计费）  昨天 2.9 MB")
        assert control.last_report_day == "2026-10-02"

    async def test_it_is_skipped_when_nothing_went_through_the_proxy(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 1, 5))
        service, control, sent = make_service(tmp_path, store, clock)
        service._config.daily_report_at = "09:00"  # noqa: SLF001
        await started(service)
        assert await service.maybe_report(clock()) is False
        assert sent == [] and control.last_report_day == "2026-10-02"

    async def test_an_alert_yesterday_makes_a_quiet_day_worth_reporting(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 1, 4, 0))
        service, control, sent = make_service(tmp_path, store, clock)
        service._config.daily_report_at = "09:00"  # noqa: SLF001
        await started(service)
        await control.claim_alert("leak:2026-10-01:x", "2026-10-01")
        clock.now = utc(2026, 10, 2, 1, 5)
        assert await service.maybe_report(clock()) is True and len(sent) == 1

    async def test_it_can_be_turned_off(self, tmp_path, store):
        clock = Clock(utc(2026, 10, 2, 1, 5))
        service, _control, sent = make_service(tmp_path, store, clock)
        await started(service)
        assert await service.maybe_report(clock()) is False and sent == []


# ------------------------------------------------------------------- report


def seeded(store):
    h = hour_of(utc(2026, 10, 2, 3, 0))
    gb = 1024 * MB
    store.add_hours([
        ((h, "telegram", "proxy", TG_NODE), int(0.05 * gb), int(1.70 * gb), 0.1431),
        ((h, "other", "proxy", JP_NODE), 0, int(0.09 * gb), 0.0017),
        ((h, "pikpak", "direct", ""), 0, int(18.1 * gb), 0),
        ((h, "lan", "direct", ""), 0, int(0.1 * gb), 0),
        ((h, "unattributed", "unknown", ""), 0, int(0.02 * gb), 0),
    ])
    store.add_hosts([
        (("2026-10-02", "149.154.167.50", "telegram", "proxy", TG_NODE), int(1.5 * gb)),
        (("2026-10-02", "example.com", "other", "proxy", JP_NODE), 200 * MB),
    ])


class TestReport:
    def report(self, store, period):
        return build_report(
            store, period, utc(2026, 10, 2, 4, 0), SH,
            budgets=Budgets(2.0, 30.0, 0.0),
            exit_chain=["TG", "TG-OTHER", TG_NODE],
        )

    def test_today(self, store, chinese):
        seeded(store)
        assert render(self.report(store, "today"), html=False) == "\n".join([
            "代理流量（计费）  今天 1.84 GB ≈ ¥0.14",
            "  Telegram 下载 1.70 GB · 上传 0.05 GB   英格兰002 0.09元/G",
            "  其他 0.09 GB   东京京X06 0.01元/G",
            "直连流量（不计费）  今天 18.20 GB",
            "  PikPak 18.10 GB · 局域网/模型 0.10 GB",
            "未归属 0.02 GB",
            "预算：今日 ¥0.14 / ¥2.00 · 本月 ¥0.14 / ¥30.00",
            "当前出口：TG → 英格兰002 0.09元/G",
            "下载闸门：开启（不限速）",
            "",
            "代理流量最大的主机",
            "1. 149.154.167.50 — 1.50 GB（英格兰002 0.09元/G）",
            "2. example.com — 0.20 GB（东京京X06 0.01元/G）",
            "",
            "下面这些未知主机走代理超过 100 MB，可以考虑加规则：",
            "  example.com — 0.20 GB",
        ])

    def test_week_and_month_use_the_same_numbers_with_their_own_heading(self, store, chinese):
        seeded(store)
        week = render(self.report(store, "7d"), html=False)
        month = render(self.report(store, "month"), html=False)
        assert week.splitlines()[0] == "代理流量（计费）  近 7 天 1.84 GB ≈ ¥0.14"
        assert month.splitlines()[0] == "代理流量（计费）  本月 1.84 GB ≈ ¥0.14"
        assert week.splitlines()[1:3] == month.splitlines()[1:3]
        assert week.splitlines()[4:] == month.splitlines()[4:]

    def test_html_escapes_what_comes_from_the_network(self, store, chinese):
        h = hour_of(utc(2026, 10, 2, 3, 0))
        store.add_hours([((h, "other", "proxy", "<b>x</b>"), 0, MB, 0.1)])
        store.add_hosts([(("2026-10-02", "<evil>.example", "other", "proxy", "<b>x</b>"),
                          5 * MB)])
        text = render(self.report(store, "today"))
        assert "<evil>" not in text and "&lt;evil&gt;.example" in text

    def test_json_carries_the_same_figures(self, store):
        seeded(store)
        data = self.report(store, "today").as_dict()
        assert data["proxy"]["cost_cny"] == pytest.approx(0.1448)
        assert data["budgets"]["daily_cny"]["limit"] == 2.0
        assert data["exit"][0] == "TG"
        json.dumps(data, ensure_ascii=False)

    def test_an_empty_day(self, store, chinese):
        text = render(self.report(store, "today"), html=False)
        assert text.startswith("代理流量（计费）  今天 0 KB ≈ ¥0.00\n  没有经过代理的流量")

    def test_the_gate_line_names_what_closed_it(self, store, chinese):
        report = build_report(store, "today", utc(2026, 10, 2, 4, 0), SH,
                              budgets=Budgets(), gate="over_budget",
                              gate_reason="daily_cny:2026-10-02", media_rate=5, upload_rate=0)
        assert "下载闸门：已暂停（超出预算：每日费用）（下载限速 5 MB/s）" in render(
            report, html=False)

    def test_sizes(self):
        assert fmt_bytes(1.84 * 1024 * MB) == "1.84 GB"
        assert fmt_bytes(5.3 * MB) == "5.3 MB"
        assert fmt_bytes(12.3 * MB) == "0.01 GB"
        assert fmt_bytes(480 * 1024) == "480 KB"


# ----------------------------------------------------- mihomo and the service


class FakeMihomo:
    """A tiny HTTP server standing in for mihomo's controller."""

    def __init__(self, port: int = 0) -> None:
        self.body = snap([])
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/connections":
                    payload = json.dumps(owner.body).encode()
                elif self.path.startswith("/proxies/"):
                    name = self.path.rsplit("/", 1)[1]
                    nxt = {"TG": "TG-OTHER", "TG-OTHER": TG_NODE}.get(
                        __import__("urllib.parse").parse.unquote(name))
                    payload = json.dumps({"now": nxt} if nxt else {"type": "Vless"}).encode()
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        self.server = HTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.02)


class TestMihomoClient:
    def test_it_reads_connections_and_follows_a_group_to_its_node(self):
        server = FakeMihomo()
        try:
            client = MihomoClient(server.url)
            assert client.connections()["connections"] == []
            assert client.current_exit("TG") == ["TG", "TG-OTHER", TG_NODE]
        finally:
            server.stop()


class TestTheMeterOutlivesMihomo:
    async def test_it_polls_survives_an_outage_and_recovers(self, tmp_path, store, caplog):
        port = free_port()
        server = FakeMihomo(port)
        config = TrafficConfig(enabled=True, mihomo_api=server.url, poll_seconds=0.05,
                               daily_report_at="", conn_alert_mb=0)
        control = TrafficControl(config)
        service = TrafficService(config, store, control)
        server.body = snap([conn("a", "example.com", down=1000, chains=[JP_NODE, "PROXY"])])
        try:
            with caplog.at_level(logging.INFO):
                await service.start()
                await wait_for(lambda: service.reachable is True)
                server.body = snap([conn("a", "example.com", down=5000,
                                         chains=[JP_NODE, "PROXY"])])
                await wait_for(lambda: service._hours)  # noqa: SLF001
                server.stop()
                await wait_for(lambda: service.reachable is False)
                await asyncio.sleep(0.4)                 # several failed polls
                assert service._task is not None and not service._task.done()  # noqa: SLF001
                warnings = [r for r in caplog.records
                            if r.levelno == logging.WARNING and "cannot read mihomo" in r.message]
                assert len(warnings) == 1                # once per outage, not per poll
                server = FakeMihomo(port)
                server.body = snap([conn("a", "example.com", down=9000,
                                         chains=[JP_NODE, "PROXY"])])
                await wait_for(lambda: service.reachable is True, timeout=15)
        finally:
            await service.stop()
            server.stop()
        assert any("reachable again" in r.message for r in caplog.records)

    async def test_it_never_raises_into_the_caller_when_nothing_listens(self, tmp_path, store):
        config = TrafficConfig(enabled=True, mihomo_api=f"http://127.0.0.1:{free_port()}",
                               poll_seconds=0.05, daily_report_at="")
        service = TrafficService(config, store, TrafficControl(config))
        await service.start()
        try:
            await wait_for(lambda: service.reachable is False)
            assert not service._task.done()  # noqa: SLF001
        finally:
            await service.stop()
