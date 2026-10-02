"""M9.1: node selection, the mihomo write whitelist, subscription health and
direct-first routing (docs/wms/M9.1 §A, §B, §D)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest

from tgmd import i18n
from tgmd.config import TrafficConfig
from tgmd.db import Database
from tgmd.traffic import TrafficControl
from tgmd.traffic import proxy_ui as ui
from tgmd.traffic.classify import classify
from tgmd.traffic.direct import DirectRouting, judge, validate_host
from tgmd.traffic.mihomo import ForbiddenWrite, MihomoClient, http_send
from tgmd.traffic.nodes import Busy, NodeManager
from tgmd.traffic.probe import HeadResult, NodeResult, Prober, best_node
from tgmd.traffic.store import TrafficStore

CHEAP_FAST = "🖤东京01｜0.01元/G｜Reality｜"
CHEAP_SLOW = "🇯🇵日本02｜0.02元/G｜hy2｜"
MID = "🇺🇸美国03｜0.05元/G｜vless｜"
DEAR = "😈英格兰002｜0.09元/G｜hy2｜"
TOO_DEAR = "💎特级04｜0.30元/G｜hy2｜"
NOTICE = "剩余流量：未知｜官网"
MB = 1024 * 1024


@pytest.fixture
def chinese():
    previous = i18n.language()
    i18n.set_language("zh")
    yield
    i18n.set_language(previous)


class Controller:
    """A fake mihomo that answers reads and records every write."""

    def __init__(self, nodes=None) -> None:
        names = nodes or [CHEAP_FAST, CHEAP_SLOW, MID, DEAR, TOO_DEAR, NOTICE]
        self.nodes = [{"name": n, "alive": True} for n in names]
        self.groups = {"FAST": "unprobed-node", "TG-PICK": "FAST", "PROXY": "AUTO-LATENCY",
                       "PROBE": "DIRECT", "AUTO-LATENCY": CHEAP_FAST}
        self.delays = {n: 80 for n in names}
        self.connections: list[dict] = []
        self.writes: list[tuple[str, str]] = []
        self.reloads = 0
        self.provider_error: Exception | None = None

    def fetch(self, url: str) -> dict:
        path = urlsplit(url).path
        if path == "/connections":
            return {"connections": list(self.connections)}
        if path.endswith("/healthcheck"):
            name = unquote(path[len("/providers/proxies/main/"):-len("/healthcheck")])
            delay = self.delays.get(name)
            if not delay:
                raise urllib.error.URLError("timeout")
            return {"delay": delay}
        if path.startswith("/providers/proxies/"):
            if self.provider_error is not None:
                raise self.provider_error
            return {"proxies": self.nodes}
        if path.startswith("/proxies/"):
            name = unquote(path[len("/proxies/"):])
            return {"now": self.groups[name]} if name in self.groups else {"type": "Vless"}
        raise AssertionError(f"unexpected read {url}")

    def send(self, method: str, url: str, body):
        path = urlsplit(url).path
        self.writes.append((method, path))
        if method == "PUT" and path.startswith("/proxies/"):
            self.groups[path.rsplit("/", 1)[1]] = body["name"]
        elif method == "DELETE":
            ident = path.rsplit("/", 1)[1]
            self.connections = [c for c in self.connections if c["id"] != ident]
        elif path == "/providers/rules/direct-auto":
            self.reloads += 1
        return None

    def client(self) -> MihomoClient:
        return MihomoClient("http://mihomo", fetch=self.fetch, send=self.send)

    def tg_connection(self, ident="c1"):
        self.connections.append({"id": ident, "chains": [DEAR, "TG-PICK", "TG"]})

    def writes_to(self, group: str) -> list[str]:
        return [p for m, p in self.writes if m == "PUT" and p == f"/proxies/{group}"]


class FakeNet:
    """Speeds by node; ``PROBE`` says which node a transfer would go through."""

    def __init__(self, controller: Controller, speeds: dict[str, float]) -> None:
        self.c, self.speeds = controller, speeds
        self.heads: dict[str, HeadResult] = {}
        self.downloaded: list[str] = []

    def download(self, url, *, cap, seconds):
        node = self.c.groups["PROBE"]
        self.downloaded.append(node)
        mbps = self.speeds.get(node, 10.0)
        return 8_000_000, 8_000_000 * 8 / 1e6 / mbps

    def upload(self, url, *, size, seconds):
        return size, size * 8 / 1e6 / 5.0

    def head(self, host, *, url, seconds):
        key = f"{host}|{self.c.groups['PROBE']}"
        return self.heads.get(key) or self.heads.get(host) or HeadResult(
            reachable=True, tls_ok=True, latency_ms=100)


@pytest.fixture
def store(tmp_path):
    handle = TrafficStore(tmp_path / "t.sqlite3")
    handle.open()
    yield handle
    handle.close()


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "bot.sqlite3")
    await database.connect()
    yield database
    await database.close()


SPEEDS = {CHEAP_FAST: 80.0, CHEAP_SLOW: 78.0, MID: 40.0, DEAR: 90.0, TOO_DEAR: 500.0}


def make(store, db=None, *, controller=None, speeds=None, clock=None, **settings):
    controller = controller or Controller()
    config = TrafficConfig(enabled=False, **settings)
    client = controller.client()
    control = TrafficControl(config, db)
    net = FakeNet(controller, speeds if speeds is not None else dict(SPEEDS))
    sent: list[str] = []

    async def notify(text, buttons=None):
        sent.append(text)

    now = clock or (lambda: 1_000_000.0)
    manager = NodeManager(config, client, store, db, control, Prober(config, client, net, store,
                                                                     clock=now),
                          notify=notify, clock=now)
    return SimpleNamespace(controller=controller, client=client, control=control, net=net,
                           manager=manager, sent=sent, config=config)


# ----------------------------------------------------------- picking and probing


def result(name, down, price, alive=True):
    return NodeResult(name, price, 50, down, 5.0, alive, 0.0)


class TestPicking:
    def test_the_cheapest_within_15_percent_of_the_best_wins(self):
        results = [result("a", 100, 0.05), result("b", 90, 0.01), result("c", 80, 0.001)]
        assert best_node(results).name == "b"       # c is 20% down; b is 10% down and cheaper

    def test_the_fastest_wins_when_nothing_else_is_close(self):
        assert best_node([result("a", 100, 0.09), result("b", 50, 0.01)]).name == "a"

    def test_dead_and_zero_speed_nodes_never_win(self):
        assert best_node([result("a", 100, 0.01, alive=False), result("b", 0, 0.01)]) is None


class TestProbe:
    async def test_skip_rules_cap_and_cleanup(self, store):
        w = make(store)
        w.controller.nodes[1]["alive"] = False        # CHEAP_SLOW is dead
        run = await w.manager._prober.run_async()     # noqa: SLF001
        tested = {r.name for r in run.results}
        assert tested == {CHEAP_FAST, MID, DEAR}
        skipped = dict(run.skipped)
        assert skipped[TOO_DEAR] == "price" and skipped[CHEAP_SLOW] == "dead"
        assert NOTICE not in skipped and NOTICE not in tested          # notices are not nodes
        assert w.controller.groups["PROBE"] == "DIRECT"                # always put back
        stored = {row["name"]: row for row in store.nodes()}
        assert stored[CHEAP_FAST]["down_mbps"] == pytest.approx(80, rel=0.01)
        assert stored[CHEAP_SLOW]["alive"] == 0

    async def test_a_run_stays_under_the_megabyte_cap(self, store):
        w = make(store, probe_max_mb=25)                # room for two nodes at 10 MB each
        run = await w.manager._prober.run_async()       # noqa: SLF001
        assert len(run.results) == 2 and run.spent_bytes <= 25 * MB
        assert [reason for _, reason in run.skipped].count("cap") >= 1
        # Cheap nodes first, so the cap cuts the dear ones.
        assert {r.name for r in run.results} == {CHEAP_FAST, CHEAP_SLOW}

    async def test_a_node_that_does_not_answer_is_dead_and_nothing_goes_through_it(self, store):
        w = make(store)
        w.controller.delays[MID] = 0
        run = await w.manager._prober.run_async()       # noqa: SLF001
        assert MID not in w.net.downloaded
        assert not next(r for r in run.results if r.name == MID).alive

    async def test_it_does_not_run_without_a_probe_group(self, store):
        w = make(store)

        def refuse(method, url, body):
            raise urllib.error.HTTPError(url, 404, "no such group", {}, None)

        w.manager._prober._client = MihomoClient("http://x", fetch=w.controller.fetch,  # noqa: SLF001
                                                 send=refuse)
        assert await w.manager.probe_now() is None
        assert "404" in w.manager.last_error


class TestAutoSpeed:
    async def probed(self, store, speeds, **kw):
        w = make(store, speeds=speeds, **kw)
        await w.manager.probe_now()
        return w

    async def test_fast_follows_the_best_node(self, store):
        w = await self.probed(store, dict(SPEEDS))
        # DEAR is 90 Mbps vs CHEAP_FAST 80: within 15%, so the cheaper one is picked.
        assert w.controller.groups["FAST"] == CHEAP_FAST
        assert len(w.sent) == 1 and CHEAP_FAST in w.sent[0]

    async def test_a_small_gain_does_not_switch(self, store):
        t = [1_000_000.0]
        w = make(store, speeds=dict(SPEEDS), clock=lambda: t[0])
        await w.manager.probe_now()
        assert w.controller.groups["FAST"] == CHEAP_FAST
        t[0] += 3600
        w.net.speeds[MID] = 88.0              # best is MID at 88 vs 80 now: only 10% faster
        w.net.speeds[CHEAP_FAST] = 80.0
        w.net.speeds[DEAR] = 60.0
        await w.manager.probe_now()
        assert w.controller.groups["FAST"] == CHEAP_FAST

    async def test_a_big_gain_switches_but_not_inside_the_minimum_interval(self, store):
        t = [1_000_000.0]
        w = make(store, speeds={CHEAP_FAST: 50.0, CHEAP_SLOW: 10.0, MID: 20.0, DEAR: 10.0},
                 clock=lambda: t[0])
        await w.manager.probe_now()
        assert w.controller.groups["FAST"] == CHEAP_FAST
        w.net.speeds[MID] = 100.0                              # 2x faster
        t[0] += 60                                              # a minute later
        await w.manager.probe_now()
        assert w.controller.groups["FAST"] == CHEAP_FAST       # too soon
        t[0] += 31 * 60
        await w.manager.probe_now()
        assert w.controller.groups["FAST"] == MID

    async def test_a_dead_current_node_is_replaced_without_the_25_percent_rule(self, store):
        t = [1_000_000.0]
        w = make(store, speeds={CHEAP_FAST: 80.0, CHEAP_SLOW: 78.0, MID: 40.0, DEAR: 10.0},
                 clock=lambda: t[0])
        await w.manager.probe_now()
        w.controller.delays[CHEAP_FAST] = 0                     # it stops answering
        t[0] += 31 * 60
        await w.manager.probe_now()
        assert w.controller.groups["FAST"] == CHEAP_SLOW

    async def test_nothing_changes_when_no_group_is_in_auto_speed(self, store, db):
        w = make(store, db, speeds=dict(SPEEDS))
        await w.manager.set_mode("TG-PICK", "auto-latency")
        before = list(w.controller.writes_to("FAST"))
        await w.manager.probe_now()
        assert w.controller.writes_to("FAST") == before


class TestExitSafety:
    async def test_changing_tg_pick_closes_the_telegram_connections_only(self, store, db):
        w = make(store, db)
        w.controller.tg_connection("tg1")
        w.controller.connections.append({"id": "web1", "chains": [CHEAP_FAST, "PROXY"]})
        await w.manager.set_mode("TG-PICK", "manual", DEAR)
        assert w.controller.groups["TG-PICK"] == DEAR
        assert [c["id"] for c in w.controller.connections] == ["web1"]
        assert ("DELETE", "/connections/tg1") in w.controller.writes

    async def test_changing_fast_while_tg_pick_is_on_fast_closes_them_too(self, store):
        w = make(store, speeds=dict(SPEEDS))
        w.controller.tg_connection("tg1")
        await w.manager.probe_now()                 # TG-PICK is FAST; FAST moves
        assert w.controller.connections == []

    async def test_changing_fast_while_tg_pick_is_elsewhere_leaves_them(self, store, db):
        w = make(store, db, speeds=dict(SPEEDS))
        w.controller.groups["TG-PICK"] = DEAR
        w.controller.tg_connection("tg1")
        await w.manager.set_mode("PROXY", "auto-speed")
        await w.manager.probe_now()
        assert [c["id"] for c in w.controller.connections] == ["tg1"]

    async def test_the_exit_is_not_switched_while_an_upload_is_finishing(self, store, db):
        w = make(store, db, speeds=dict(SPEEDS))
        token = object()
        w.control.note_upload(token, 99 * MB, 100 * MB)         # the last part
        with pytest.raises(Busy):
            await w.manager.set_mode("TG-PICK", "manual", DEAR)
        assert w.controller.writes == []
        w.control.note_upload(token, 10 * MB, 100 * MB)         # mid-way: fine
        await w.manager.set_mode("TG-PICK", "manual", DEAR)
        w.control.note_upload(token, 99 * MB, 100 * MB)
        await w.manager.probe_now()                              # auto: deferred
        assert w.controller.groups["FAST"] == "unprobed-node"    # unchanged
        w.control.end_upload(token)

    async def test_manual_switches_are_not_rate_limited(self, store, db):
        w = make(store, db)
        for node in (DEAR, MID, CHEAP_FAST):
            await w.manager.set_mode("TG-PICK", "manual", node)
        assert w.controller.groups["TG-PICK"] == CHEAP_FAST


class TestModesPersist:
    async def test_modes_survive_a_restart_and_are_applied_again(self, store, db):
        first = make(store, db)
        await first.manager.set_mode("PROXY", "manual", MID)
        await first.manager.set_mode("TG-PICK", "auto-latency")
        second = make(store, db)          # a fresh process and a fresh mihomo
        await second.manager.load()
        assert second.manager.mode_of("PROXY") == ("manual", MID)
        assert second.manager.mode_of("TG-PICK") == ("auto-latency", None)
        await second.manager.apply_saved()
        assert second.controller.groups["PROXY"] == MID
        assert second.controller.groups["TG-PICK"] == "AUTO-LATENCY"

    async def test_a_manual_pick_stops_auto_repicks(self, store, db):
        w = make(store, db, speeds=dict(SPEEDS))
        await w.manager.set_mode("TG-PICK", "manual", DEAR)
        await w.manager.probe_now()
        assert w.controller.groups["TG-PICK"] == DEAR

    async def test_groups_never_chosen_are_left_alone_at_start(self, store, db):
        w = make(store, db)
        await w.manager.load()
        await w.manager.apply_saved()
        assert w.controller.writes == []

    async def test_only_the_two_groups_and_three_modes_are_accepted(self, store, db):
        w = make(store, db)
        with pytest.raises(ValueError):
            await w.manager.set_mode("FAST", "manual", MID)
        with pytest.raises(ValueError):
            await w.manager.set_mode("PROXY", "manual")


# ------------------------------------------------------------------- whitelist


class Recorder:
    """A real HTTP server that records every non-GET request it receives."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _any(self):
                owner.requests.append((self.command, self.path))
                self.send_response(204)
                self.end_headers()

            do_PUT = do_DELETE = do_POST = do_PATCH = _any

            def log_message(self, *_a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class TestWriteWhitelist:
    def test_only_the_listed_calls_reach_the_wire(self):
        server = Recorder()
        try:
            client = MihomoClient(server.url, fetch=lambda u: {})
            client.select("FAST", "node")
            client.select("TG-PICK", "FAST")
            client.select("PROXY", "AUTO-LATENCY")
            client.select("PROBE", "DIRECT")
            client.close_connection("abc-123")
            client.reload_rules()
            assert server.requests == [
                ("PUT", "/proxies/FAST"), ("PUT", "/proxies/TG-PICK"),
                ("PUT", "/proxies/PROXY"), ("PUT", "/proxies/PROBE"),
                ("DELETE", "/connections/abc-123"), ("PUT", "/providers/rules/direct-auto")]
        finally:
            server.stop()

    @pytest.mark.parametrize(("method", "path"), [
        ("PUT", "/configs"), ("PUT", "/configs?force=true"), ("PATCH", "/configs"),
        ("POST", "/restart"), ("POST", "/upgrade"), ("DELETE", "/connections"),
        ("PUT", "/proxies/AUTO-LATENCY"), ("PUT", "/proxies/TG"), ("PUT", "/proxies/DIRECT"),
        ("PUT", "/providers/proxies/main"), ("PUT", "/providers/rules/other"),
        ("DELETE", "/proxies/FAST"), ("PUT", "/proxies/FAST/../../configs"),
        ("PUT", "/proxies/FAST/extra"), ("GET", "/proxies/FAST"),
    ])
    def test_everything_else_is_refused_before_a_request_is_made(self, method, path):
        server = Recorder()
        try:
            client = MihomoClient(server.url, fetch=lambda u: {})
            with pytest.raises(ForbiddenWrite):
                client._write(method, path, {})            # noqa: SLF001
            assert server.requests == []
        finally:
            server.stop()

    def test_a_group_the_bot_does_not_own_cannot_be_switched(self):
        client = MihomoClient("http://x", fetch=lambda u: {}, send=lambda *a: pytest.fail("sent"))
        for group in ("AUTO-LATENCY", "TG", "GLOBAL", "fast"):
            with pytest.raises(ForbiddenWrite):
                client.select(group, "x")

    def test_a_whole_node_workflow_never_writes_anything_else(self, store):
        w = make(store)
        asyncio.run(w.manager.probe_now())
        for method, path in w.controller.writes:
            assert (method, path) in {("PUT", f"/proxies/{g}")
                                      for g in ("FAST", "TG-PICK", "PROXY", "PROBE")} \
                or (method == "DELETE" and path.startswith("/connections/")) \
                or path == "/providers/rules/direct-auto", (method, path)

    def test_http_send_keeps_to_http(self):
        with pytest.raises(ValueError):
            http_send("PUT", "file:///etc/passwd", None)


# ------------------------------------------------------------ subscription health


class TestSubscriptionHealth:
    BAD = ("代理订阅可能失效（余额不足或已过期）：存活 {a}/{m} 个节点。"
           "请续费或把新订阅发给 Cowork 更换。")

    def kill(self, w, count):
        real = [n for n in w.controller.nodes if n["name"] != NOTICE]
        for node in real[:count]:
            node["alive"] = False

    async def test_mostly_dead_for_ten_minutes_alerts_once_then_recovers_once(self, store, chinese):
        w = make(store)
        self.kill(w, 4)                                   # 4 of 5 real nodes: 80%
        await w.manager.health(1000.0)
        assert w.sent == []                               # not yet ten minutes
        await w.manager.health(1000.0 + 599)
        assert w.sent == []
        await w.manager.health(1000.0 + 601)
        await w.manager.health(1000.0 + 700)
        await w.manager.health(1000.0 + 900)
        assert w.sent == [self.BAD.format(a=1, m=5)]
        for node in w.controller.nodes:
            node["alive"] = True
        await w.manager.health(2000.0)
        await w.manager.health(2100.0)
        assert w.sent[1:] == ["代理订阅已恢复：存活 5/5 个节点。"]

    async def test_a_brief_dip_is_not_an_incident(self, store, chinese):
        w = make(store)
        self.kill(w, 5)
        await w.manager.health(1000.0)
        for node in w.controller.nodes:
            node["alive"] = True
        await w.manager.health(1300.0)
        self.kill(w, 5)
        await w.manager.health(1400.0)
        await w.manager.health(1500.0)
        assert w.sent == []

    async def test_a_failing_provider_alerts_at_once(self, store, chinese):
        w = make(store)
        w.controller.provider_error = urllib.error.HTTPError("u", 502, "bad gateway", {}, None)
        await w.manager.health(1000.0)
        assert len(w.sent) == 1 and "代理订阅可能失效" in w.sent[0]

    async def test_an_empty_provider_counts_as_failing(self, store, chinese):
        w = make(store)
        w.controller.nodes = [{"name": NOTICE, "alive": True}]
        await w.manager.health(1000.0)
        assert len(w.sent) == 1

    async def test_mihomo_being_down_says_nothing_about_the_subscription(self, store, chinese):
        w = make(store)
        w.controller.provider_error = urllib.error.URLError("refused")
        await w.manager.health(1000.0)
        assert w.sent == []

    async def test_the_incident_survives_a_restart_without_a_second_alert(self, store, db, chinese):
        w = make(store, db)
        w.controller.provider_error = urllib.error.HTTPError("u", 502, "x", {}, None)
        await w.manager.health(1000.0)
        again = make(store, db, controller=w.controller)
        await again.manager.load()
        await again.manager.health(1100.0)
        assert again.sent == []


# --------------------------------------------------------- metering and rendering


class TestProbeTrafficIsMetered:
    def test_connections_from_the_probe_listener_are_category_probe(self):
        connection = {"id": "1", "chains": [CHEAP_FAST, "PROBE"],
                      "metadata": {"host": "speed.cloudflare.com", "inboundName": "probe",
                                   "destinationIP": "", "destinationPort": "443"}}
        info = classify(connection)
        assert (info.category, info.outbound) == ("probe", "proxy")
        direct = classify({**connection, "chains": ["DIRECT"]})
        assert (direct.category, direct.outbound) == ("probe", "direct")

    def test_other_inbounds_are_unaffected(self):
        info = classify({"id": "1", "chains": [CHEAP_FAST, "PROXY"],
                         "metadata": {"host": "example.com", "inboundName": "tun"}})
        assert info.category == "other"


class FakeEvent:
    def __init__(self, data: str) -> None:
        self.data = data.encode()
        self.edits: list[tuple[str, object]] = []
        self.answers: list[str] = []

    async def edit(self, text, **kwargs):
        self.edits.append((text, kwargs.get("buttons")))

    async def answer(self, text="", **_kwargs):
        self.answers.append(text)


def labels(markup) -> list[str]:
    return [b.text for row in markup.rows for b in row.buttons]


class TestProxyCommand:
    async def test_status_snapshot(self, store, db, chinese):
        w = make(store, db, speeds=dict(SPEEDS))
        await w.manager.probe_now()
        await w.manager.health(1000.0)
        text = await ui.render_status(w.manager)
        lines = text.splitlines()
        assert lines[0] == "<b>节点</b>"
        assert lines[1] == ("访问（PROXY）：自动·延迟最低 → 东京01 0.01元/G"
                            " · 延迟 80 ms · 速度 ↓80 / ↑5 Mbps")
        assert lines[2].startswith("下载/Telegram（TG-PICK）：自动·速度最快 → 东京01 0.01元/G")
        assert lines[3] == "订阅：存活 5/5 个节点"
        assert lines[4].startswith("上次测速：1970-01-")
        assert labels(ui.main_buttons()) == [
            "访问·延迟最低", "访问·速度最快", "访问·手动选择…",
            "下载·延迟最低", "下载·速度最快", "下载·手动选择…", "立即测速", "直连检测"]

    async def test_picking_a_node_by_hand(self, store, db, chinese):
        w = make(store, db, speeds=dict(SPEEDS))
        await w.manager.probe_now()
        listing = FakeEvent("proxy:pick:TG-PICK:0")
        await ui.handle_button(listing, w.manager, None)
        text, _ = listing.edits[-1]
        assert "为 下载/Telegram（TG-PICK） 选择节点" in text
        names = w.manager.pick_lists["TG-PICK"]
        assert names[0] == DEAR                                   # fastest first (90 Mbps)
        choose = FakeEvent(f"proxy:set:TG-PICK:{names.index(MID)}")
        await ui.handle_button(choose, w.manager, None)
        assert w.controller.groups["TG-PICK"] == MID
        assert w.manager.mode_of("TG-PICK") == ("manual", MID)
        assert "手动 → 美国03" in choose.edits[-1][0]

    async def test_a_stale_pick_is_refused_politely(self, store, db, chinese):
        w = make(store, db)
        event = FakeEvent("proxy:set:PROXY:99")
        await ui.handle_button(event, w.manager, None)
        assert event.answers == ["这个列表已经过期，重新列出。"]
        assert w.controller.writes == []

    async def test_mode_buttons_take_effect_in_mihomo(self, store, db, chinese):
        w = make(store, db, speeds=dict(SPEEDS))
        await w.manager.probe_now()
        await ui.handle_button(FakeEvent("proxy:mode:PROXY:spd"), w.manager, None)
        assert w.controller.groups["PROXY"] == "FAST"
        await ui.handle_button(FakeEvent("proxy:mode:TG-PICK:lat"), w.manager, None)
        assert w.controller.groups["TG-PICK"] == "AUTO-LATENCY"

    async def test_probe_now_reports_in_the_same_message(self, store, db, chinese):
        w = make(store, db, speeds=dict(SPEEDS))
        event = FakeEvent("proxy:probe")
        await ui.handle_button(event, w.manager, None)
        assert event.edits[0][0] == "⏳ 正在逐个测速，需要几分钟…"
        assert "测了 4 个节点，约用了 " in event.edits[-1][0]
        assert event.answers == ["测速完成"]

    async def test_a_busy_exit_is_an_alert_not_a_crash(self, store, db, chinese):
        w = make(store, db)
        w.control.note_upload(object(), 99 * MB, 100 * MB)
        event = FakeEvent("proxy:mode:TG-PICK:lat")
        await ui.handle_button(event, w.manager, None)
        assert event.answers == ["有 Telegram 上传正在收尾，请稍后再试。"]

    async def test_the_command_is_admin_only_and_registered(self, store, db, chinese):
        from tgmd.config import Config
        from tgmd.handlers import BotHandlers

        config = Config()
        config.access.admin_user_ids = [1]
        config.access.allowed_user_ids = [2]
        handlers = BotHandlers(bot=object(), config=config, db=object(), queue=object(),
                               pikpak=object(), portal=object())
        w = make(store, db)
        handlers.attach_nodes(w.manager, None)

        class Event:
            raw_text = "/proxy"
            is_private = True

            def __init__(self, user):
                self.sender_id = self.chat_id = user
                self.replies = []

            async def reply(self, text, **kw):
                self.replies.append((text, kw))

        member, admin = Event(2), Event(1)
        await handlers.on_proxy(member)
        assert member.replies[0][0] == "只有管理员可以查看流量。"
        await handlers.on_proxy(admin)
        assert admin.replies[0][0].startswith("<b>节点</b>")


# --------------------------------------------------------------- direct routing


def head(ms, *, tls=True, ok=True, error=""):
    return HeadResult(reachable=ok, tls_ok=tls, latency_ms=ms, error=error)


class TestValidator:
    @pytest.mark.parametrize("host", [
        "telegram.org", "web.telegram.org", "t.me", "cdn.telegra.ph", "149.154.167.50",
        "bujidao.cc", "sub.bujidao.cc", "api.bujidao.com", "nas.local", "192.168.0.5",
        "10.0.0.1", "localhost", "", "no spaces.com", "a.com/path", "x",
    ])
    def test_these_are_never_routed_direct(self, host):
        assert validate_host(host) != ""

    @pytest.mark.parametrize("host", ["registry.ollama.ai", "github.com", "pypi.org",
                                      "files.pythonhosted.org", "a.r2.cloudflarestorage.com"])
    def test_ordinary_hosts_are_fine(self, host):
        assert validate_host(host) == ""


class TestVerdicts:
    def test_a_failed_tls_check(self):
        v = judge("h.com", head(None, tls=False, ok=False, error="TLS: SSLCertVerificationError"),
                  head(100))
        assert not v.ok and v.reason.startswith("TLS")

    def test_a_reset_or_timeout(self):
        v = judge("h.com", head(None, tls=False, ok=False, error="ConnectionResetError"),
                  head(100))
        assert not v.ok and "ConnectionResetError" in v.reason
        v = judge("h.com", HeadResult(reachable=False, tls_ok=True, error="HTTP 503"), head(100))
        assert not v.ok

    def test_a_slow_direct_route_by_latency(self):
        assert not judge("h.com", head(400), head(100)).ok       # 4x
        assert judge("h.com", head(290), head(100)).ok           # under 3x

    def test_throughput_must_reach_70_percent_or_2_mib_per_s(self):
        assert not judge("h.com", head(100), head(100), direct_mbps=10, proxy_mbps=100).ok
        assert judge("h.com", head(100), head(100), direct_mbps=75, proxy_mbps=100).ok
        assert judge("h.com", head(100), head(100), direct_mbps=20, proxy_mbps=100).ok  # 2.4 MiB/s
        assert judge("h.com", head(100), head(100), direct_mbps=1, proxy_mbps=1).ok

    def test_a_good_direct_route(self):
        v = judge("h.com", head(120), head(100))
        assert v.ok and (v.direct_ms, v.proxy_ms) == (120, 100)

    def test_a_dead_proxy_does_not_fail_a_working_direct_route(self):
        assert judge("h.com", head(300), head(None, ok=False)).ok


def make_direct(store, tmp_path, controller=None, db=None, **settings):
    controller = controller or Controller()
    rules = tmp_path / "rules" / "direct-auto.txt"
    config = TrafficConfig(enabled=False, direct_rules_file=str(rules), **settings)
    net = FakeNet(controller, {})
    sent = []

    async def notify(text, buttons=None):
        sent.append((text, buttons))

    routing = DirectRouting(config, controller.client(), net, store, db, notify=notify,
                            clock=lambda: 1_000_000.0)
    return SimpleNamespace(routing=routing, controller=controller, net=net, rules=rules,
                           sent=sent, config=config)


class TestDirectRouting:
    async def test_candidates_are_heavy_proxied_hosts_plus_the_configured_list(
            self, store, tmp_path):
        from datetime import UTC, datetime

        d = make_direct(store, tmp_path, direct_probe_hosts=("pypi.org", "telegram.org"))
        day = datetime.now(UTC).astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai"))
        key = day.date().isoformat()
        store.add_hosts([((key, "big.example.com", "other", "proxy", ""), 80 * MB),
                         ((key, "small.example.com", "other", "proxy", ""), 10 * MB),
                         ((key, "web.telegram.org", "telegram", "proxy", ""), 900 * MB),
                         ((key, "api.bujidao.cc", "proxy-sub", "proxy", ""), 90 * MB),
                         ((key, "direct.example.com", "other", "direct", ""), 90 * MB)])
        assert d.routing.candidates() == ["big.example.com", "pypi.org"]

    async def test_a_test_runs_direct_then_through_the_current_node_and_resets_probe(
            self, store, tmp_path):
        d = make_direct(store, tmp_path)
        d.net.heads["good.example.com|DIRECT"] = head(120)
        d.net.heads[f"good.example.com|{CHEAP_FAST}"] = head(100)
        (verdict,) = await d.routing.run_checks(["good.example.com"])
        assert verdict.ok
        assert [p for _, p in d.controller.writes] == ["/proxies/PROBE"] * 3
        assert d.controller.groups["PROBE"] == "DIRECT"
        assert store.direct_hosts()[0]["state"] == "candidate"

    async def test_failing_and_telegram_hosts(self, store, tmp_path):
        d = make_direct(store, tmp_path)
        d.net.heads["bad.example.com|DIRECT"] = head(None, tls=False, ok=False, error="TLS: X")
        verdicts = await d.routing.run_checks(["bad.example.com", "telegram.org"])
        assert [v.ok for v in verdicts] == [False, False]
        assert verdicts[1].reason == "Telegram"
        assert {r["state"] for r in store.direct_hosts() if r["host"] == "bad.example.com"} == {
            "failed"}

    async def test_applying_writes_the_file_atomically_and_reloads_the_provider(
            self, store, tmp_path):
        d = make_direct(store, tmp_path)
        await d.routing.apply("registry.ollama.ai")
        await d.routing.apply("a.example.org")
        assert d.rules.read_text() == "+.a.example.org\n+.registry.ollama.ai\n"
        assert d.controller.reloads == 2
        assert oct(d.rules.stat().st_mode & 0o777) == "0o644"
        assert [p.name for p in d.rules.parent.iterdir()] == ["direct-auto.txt"]   # no temp left

    async def test_a_failed_write_leaves_the_old_file_and_the_old_state(
            self, store, tmp_path, monkeypatch):
        d = make_direct(store, tmp_path)
        await d.routing.apply("a.example.org")
        before = d.rules.read_text()

        def boom(*_a):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            await d.routing.apply("b.example.org")
        assert d.rules.read_text() == before
        assert [p.name for p in d.rules.parent.iterdir()] == ["direct-auto.txt"]
        states = {r["host"]: r["state"] for r in store.direct_hosts()}
        assert states["b.example.org"] != "applied"

    async def test_telegram_and_bujidao_can_never_be_applied(self, store, tmp_path):
        d = make_direct(store, tmp_path)
        for host in ("web.telegram.org", "bujidao.cc", "149.154.167.50"):
            with pytest.raises(ValueError):
                await d.routing.apply(host)
        assert not d.rules.exists() and d.controller.reloads == 0
        with pytest.raises(ValueError):
            d.routing.write_file(["telegram.org"])

    async def test_keeping_the_proxy_removes_the_host_and_stops_suggesting_it(
            self, store, tmp_path):
        d = make_direct(store, tmp_path, direct_probe_hosts=("a.example.org",))
        await d.routing.apply("a.example.org")
        await d.routing.restore("a.example.org")
        assert d.rules.read_text() == ""
        assert "a.example.org" not in d.routing.candidates()

    async def test_the_file_is_rewritten_from_what_is_stored_at_start(self, store, tmp_path):
        d = make_direct(store, tmp_path)
        await d.routing.apply("a.example.org")
        d.rules.write_text("")                       # a fresh, empty file on the mount
        d.controller.reloads = 0
        await d.routing.sync_at_start()
        assert d.rules.read_text() == "+.a.example.org\n" and d.controller.reloads == 1

    async def test_auto_apply_routes_passing_hosts_and_tells_the_owner(self, store, tmp_path, db,
                                                                       chinese):
        d = make_direct(store, tmp_path, db=db, direct_auto_apply=True,
                        direct_probe_hosts=("good.example.com", "bad.example.com"))
        d.net.heads["bad.example.com|DIRECT"] = head(None, tls=False, ok=False, error="TLS: X")
        await d.routing.tick(10_000_000.0)
        assert d.rules.read_text() == "+.good.example.com\n"
        assert [text for text, _ in d.sent] == ["已改为直连：<code>good.example.com</code>。"]

    async def test_without_auto_apply_nothing_is_changed_by_the_daily_look(self, store, tmp_path):
        d = make_direct(store, tmp_path, direct_probe_hosts=("good.example.com",))
        await d.routing.tick(10_000_000.0)
        assert not d.rules.exists() and d.sent == []

    async def test_a_weekly_recheck_alerts_once_and_offers_to_restore(self, store, tmp_path, db,
                                                                      chinese):
        d = make_direct(store, tmp_path, db=db)
        await d.routing.apply("a.example.org")
        d.net.heads["a.example.org|DIRECT"] = head(None, tls=False, ok=False, error="reset")
        await d.routing.tick(10_000_000.0)
        assert len(d.sent) == 1
        text, buttons = d.sent[0]
        assert "a.example.org" in text and "恢复走代理" in text
        assert labels(buttons) == ["恢复代理"]
        await d.routing.tick(10_000_000.0 + 8 * 24 * 3600)      # a week later, still broken
        assert len(d.sent) == 1
        assert {r["host"]: r["state"] for r in store.direct_hosts()}["a.example.org"] == "broken"

    async def test_the_direct_check_screen(self, store, tmp_path, chinese):
        d = make_direct(store, tmp_path)
        d.net.heads["good.example.com|DIRECT"] = head(120)
        d.net.heads["bad.example.com|DIRECT"] = head(None, tls=False, ok=False, error="TLS: X")
        await d.routing.run_checks(["good.example.com", "bad.example.com"])
        text, buttons = ui.render_direct(store.direct_hosts())
        assert "✅ <code>good.example.com</code> 可以直连 (直连 120 ms，代理 100 ms)" in text
        assert "❌ <code>bad.example.com</code> 需要走代理 (TLS: X)" in text
        assert "设为直连 good.example.com" in labels(buttons)
        assert "保持代理 good.example.com" in labels(buttons)
        manager = SimpleNamespace(pick_lists={})
        event = FakeEvent("proxy:dset:good.example.com")
        await ui.handle_button(event, manager, d.routing)
        assert d.rules.read_text() == "+.good.example.com\n"
        assert event.answers == ["已改为直连"]
        assert "🔀 <code>good.example.com</code> 已走直连" in event.edits[-1][0]


class TestSettings:
    def test_defaults(self):
        from tgmd.config import load_config

        t = load_config().traffic
        assert (t.probe_hours, t.probe_max_price, t.probe_max_mb, t.switch_min_minutes) == (
            6, 0.09, 150, 30)
        assert t.probe_url == "https://speed.cloudflare.com/__down?bytes=8000000"
        assert t.probe_up_url == "https://speed.cloudflare.com/__up"
        assert t.probe_listener == "http://127.0.0.1:7899"
        assert t.direct_candidate_mb == 50 and t.direct_auto_apply is False
        assert "registry.ollama.ai" in t.direct_probe_hosts and len(t.direct_probe_hosts) == 7
        assert t.direct_test_urls == {} and t.direct_rules_file == "/mihomo-rules/direct-auto.txt"

    def test_the_environment(self, monkeypatch):
        from tgmd.config import load_config

        for name, value in {
            "PROXY_PROBE_HOURS": "0", "PROXY_PROBE_MAX_MB": "80", "DIRECT_AUTO_APPLY": "true",
            "DIRECT_PROBE_HOSTS": "A.com, b.org ,", "DIRECT_CANDIDATE_MB": "10",
            "DIRECT_TEST_URLS": "a.com=https://a.com/x.bin, b.org=https://b.org/y",
            "DIRECT_RULES_FILE": "/tmp/r.txt",
        }.items():
            monkeypatch.setenv(name, value)
        t = load_config().traffic
        assert (t.probe_hours, t.probe_max_mb, t.direct_candidate_mb) == (0, 80, 10)
        assert t.direct_auto_apply is True and t.direct_probe_hosts == ("a.com", "b.org")
        assert t.direct_test_urls == {"a.com": "https://a.com/x.bin", "b.org": "https://b.org/y"}
        assert t.direct_rules_file == "/tmp/r.txt"


class TestLiveFixes:
    def test_latency_uses_the_provider_healthcheck_not_proxies_delay(self):
        seen = []

        def fetch(url):
            seen.append(url)
            return {"delay": 993}

        client = MihomoClient("http://m", fetch=fetch)
        assert client.delay("🖤东京01｜0.01元/G｜") == 993
        assert seen[0].startswith("http://m/providers/proxies/main/")
        assert "/healthcheck?" in seen[0] and "timeout=5000" in seen[0]
        assert "/proxies/🖤" not in seen[0]
        assert "%F0%9F%96%A4" in seen[0]                     # the node name is URL-quoted

    def test_a_node_that_does_not_answer_is_none(self):
        def fetch(url):
            raise urllib.error.HTTPError(url, 504, "timeout", {}, None)

        assert MihomoClient("http://m", fetch=fetch).delay("n") is None

    def test_every_request_carries_a_browser_user_agent(self, monkeypatch):
        import urllib.request

        from tgmd.traffic.probe import BROWSER_UA, UrllibNet

        agents = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                return b""

        def fake_open(self, req, data=None, timeout=None):
            found = {k.lower(): v for k, v in
                     [*self.addheaders, *getattr(req, "headers", {}).items()]}
            agents.append(found.get("user-agent"))
            return Response()

        monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)
        net = UrllibNet("http://127.0.0.1:7899")
        net.download("https://speed.example/down", cap=10, seconds=1)
        net.upload("https://speed.example/up", size=10, seconds=1)
        assert agents == [BROWSER_UA, BROWSER_UA]
        assert BROWSER_UA == ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                              "Chrome/120 Safari/537.36")

    def test_head_requests_carry_it_too(self, monkeypatch):
        import http.client

        from tgmd.traffic.probe import BROWSER_UA, UrllibNet

        sent = {}

        class Conn:
            def __init__(self, *a, **k):
                pass

            def set_tunnel(self, host):
                sent["tunnel"] = host

            def request(self, method, path, headers=None):
                sent["headers"] = headers

            def getresponse(self):
                return SimpleNamespace(status=200)

            def close(self):
                pass

        monkeypatch.setattr(http.client, "HTTPSConnection", Conn)
        result = UrllibNet("http://127.0.0.1:7899").head("a.example.org", url=None, seconds=1)
        assert result.reachable and sent["headers"]["User-Agent"] == BROWSER_UA


class TestCli:
    def setup(self, tmp_path, monkeypatch, controller):
        from tgmd.traffic import __main__ as cli

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DIRECT_RULES_FILE", str(tmp_path / "rules" / "direct-auto.txt"))
        monkeypatch.setattr(cli, "MihomoClient", lambda base: controller.client())
        net = FakeNet(controller, dict(SPEEDS))
        monkeypatch.setattr("tgmd.traffic.probe.UrllibNet", lambda listener: net)
        return cli, net

    def test_probe_saves_results_prints_a_table_and_leaves_fast_alone(
            self, tmp_path, monkeypatch, capsys):
        controller = Controller()
        cli, _ = self.setup(tmp_path, monkeypatch, controller)
        assert cli.main(["probe"]) == 0
        out = capsys.readouterr().out
        assert "node" in out and "東京" not in out and "东京01" in out and "skipped (price)" in out
        assert controller.groups["FAST"] == "unprobed-node"
        assert controller.groups["PROBE"] == "DIRECT"
        store = TrafficStore(tmp_path / "traffic.sqlite3")
        store.open()
        assert {r["name"] for r in store.nodes()} >= {CHEAP_FAST, MID, DEAR}
        store.close()

    def test_probe_apply_switches_fast_and_json_is_machine_readable(
            self, tmp_path, monkeypatch, capsys):
        controller = Controller()
        cli, _ = self.setup(tmp_path, monkeypatch, controller)
        assert cli.main(["probe", "--apply", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["fast_switched"] is True and controller.groups["FAST"] == CHEAP_FAST
        row = next(r for r in data["nodes"] if r["node"] == CHEAP_FAST)
        assert set(row) == {"node", "latency_ms", "down_mbps", "up_mbps", "price", "alive"}

    def test_direct_test_prints_verdicts_and_never_writes_the_rules_file(
            self, tmp_path, monkeypatch, capsys):
        controller = Controller()
        cli, net = self.setup(tmp_path, monkeypatch, controller)
        net.heads["bad.example.com|DIRECT"] = head(None, tls=False, ok=False, error="TLS: X")
        assert cli.main(["direct-test", "good.example.com", "bad.example.com",
                         "web.telegram.org"]) == 0
        out = capsys.readouterr().out
        assert "ok   good.example.com" in out and "FAIL bad.example.com" in out
        assert "FAIL web.telegram.org" in out and "(Telegram)" in out
        assert not (tmp_path / "rules").exists() and controller.reloads == 0
        assert cli.main(["direct-test", "good.example.com", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)[0]["ok"] is True
