"""M9.6: subscription revival (docs/wms/M9.6). No network: the fetch, the DNS answer and
mihomo are all fakes, and every host is under ``example.invalid``."""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
import yaml

from tgmd import i18n
from tgmd.config import Config, SubscriptionConfig
from tgmd.db import Database
from tgmd.handlers import BotHandlers
from tgmd.subscription import detect, ui
from tgmd.subscription.fetch import Fetched, FetchError
from tgmd.subscription.redact import redact_url, scrub
from tgmd.subscription.service import SubscriptionService
from tgmd.subscription.switch import BACKUPS_KEPT, Switcher
from tgmd.subscription.validate import (
    Rejected,
    check_url,
    digest_of_file_text,
    validate_content,
    validate_url,
)
from tgmd.traffic.mihomo import ForbiddenWrite, MihomoClient

ADMIN = 4242
URL = "https://sub.example.invalid/s/SECRETTOKEN0123456789"
URL2 = "https://sub.example.invalid/s/OTHERTOKEN9876543210"
PUBLIC = ["93.184.216.34"]  # what the fake DNS answers (a global address)
SECRETS = ("SECRETTOKEN0123456789", "OTHERTOKEN9876543210")


@pytest.fixture(autouse=True)
def chinese():
    previous = i18n.language()
    i18n.set_language("zh")
    yield
    i18n.set_language(previous)


# ------------------------------------------------------------------ fixtures


def proxies(count: int, prefix: str = "n", *, extra=()) -> list[dict]:
    nodes = [{"name": f"{prefix}{i}｜0.0{i % 9 + 1}元/G", "type": "vless",
              "server": f"{prefix}{i}.example.invalid", "port": 443, "uuid": f"id-{i}",
              "tls": True} for i in range(count)]
    return nodes + list(extra)


def yaml_body(count: int = 5, prefix: str = "n", **kw) -> bytes:
    return yaml.safe_dump({"proxies": proxies(count, prefix, **kw)}, allow_unicode=True).encode()


def uri_body(*, wrap: bool = True) -> bytes:
    vmess = base64.b64encode(json.dumps({
        "ps": "vm", "add": "vm.example.invalid", "port": "443", "id": "u", "aid": "0",
        "net": "ws", "path": "/p", "host": "h.example.invalid", "tls": "tls"}).encode()).decode()
    ss = base64.urlsafe_b64encode(b"aes-256-gcm:pw").decode().rstrip("=")
    lines = [
        "vless://uuid-1@example.invalid:443?security=reality&sni=s.example.invalid"
        "&pbk=KEY&sid=ab&fp=chrome&flow=xtls-rprx-vision#Node%20A",
        "hysteria2://pw@example.invalid:8443?sni=s.example.invalid&insecure=1#Node%20B",
        "trojan://pw@example.invalid:443?sni=s.example.invalid#Node%20C",
        f"ss://{ss}@example.invalid:8388#Node%20D",
        f"vmess://{vmess}",
    ]
    raw = "\n".join(lines).encode()
    return base64.b64encode(raw) if wrap else raw


class FakeMihomo:
    """A controller whose ``main`` provider reads a file when asked to, like mihomo's."""

    def __init__(self, path: Path, names: list[str] | None = None) -> None:
        self.path = path
        self.names = names if names is not None else [f"old{i}｜0.01元/G" for i in range(4)]
        self.stamp = 1
        self.alive = True
        self.reload_works = True
        self.tg_exit: str | None = None
        self.connections: list[dict] = []
        self.writes: list[tuple[str, str]] = []
        self.on_put = None
        self.after_put = None

    def fetch(self, url: str) -> dict:
        path = unquote(urlsplit(url).path)
        if path == "/connections":
            return {"connections": self.connections}
        if path.endswith("/healthcheck"):
            if not self.alive:
                raise urllib.error.URLError("timeout")
            return {"delay": 90}
        if path == "/providers/proxies/main":
            return {"updatedAt": f"t{self.stamp}",
                    "proxies": [{"name": n, "alive": self.alive} for n in self.names]}
        if path.startswith("/proxies/"):
            name = path[len("/proxies/"):]
            if name == "TG":
                return {"now": "TG-OTHER"}
            if name == "TG-OTHER":
                return {"now": self.tg_exit or self.names[0]}
            return {}
        raise AssertionError(f"unexpected read {path}")

    def send(self, method: str, url: str, body):
        path = urlsplit(url).path
        self.writes.append((method, path))
        if (method, path) == ("PUT", "/providers/proxies/main"):
            if self.on_put:
                self.on_put(self)
            if self.reload_works:
                data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
                self.names = [p["name"] for p in data["proxies"]]
                self.stamp += 1
            if self.after_put:
                self.after_put(self)

    def client(self) -> MihomoClient:
        return MihomoClient("http://mihomo", fetch=self.fetch, send=self.send)


class Ticker:
    """A clock that moves a second each time it is read (backup names stay distinct)."""

    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def make_config(tmp_path: Path, **kw) -> SubscriptionConfig:
    base = {"enabled": True, "provider_file": str(tmp_path / "main.yaml"),
            "verify_seconds": 30.0, "min_nodes": 3, "allow_private": True}
    return SubscriptionConfig(**{**base, **kw})


def seed_file(config: SubscriptionConfig, count: int = 4, prefix: str = "old") -> list[str]:
    nodes = proxies(count, prefix)
    Path(config.provider_file).write_text(yaml.safe_dump({"proxies": nodes}, allow_unicode=True))
    return [n["name"] for n in nodes]


class Rig:
    def __init__(self, tmp_path: Path, **kw) -> None:
        self.config = make_config(tmp_path, **kw)
        old = seed_file(self.config) if self.config.provider_file else None
        self.mihomo = FakeMihomo(Path(self.config.provider_file or tmp_path / "unused"), old)
        self.clock = Ticker()
        self.now = 1_800_000_000.0
        self.said: list[tuple[str, dict]] = []
        self.pages: dict[str, Fetched | Exception] = {}
        self.db = Database(tmp_path / "t.sqlite3")
        self.fetches: list[str] = []

    async def start(self) -> SubscriptionService:
        await self.db.connect()

        def fetch(url: str) -> Fetched:
            self.fetches.append(redact_url(url))
            page = self.pages.get(url)
            if isinstance(page, Exception):
                raise page
            if page is None:
                raise FetchError("network")
            return page

        async def say(kind, data):
            self.said.append((kind, data))

        switcher = Switcher(self.config, self.mihomo.client(), clock=self.clock,
                            sleep=lambda _s: None)
        self.service = SubscriptionService(
            self.config, self.mihomo.client(), self.db, say=say,
            sub_hosts=("example.invalid",), fetch=fetch, resolve=lambda _h: PUBLIC,
            clock=lambda: self.now, switcher=switcher)
        await self.service.load()
        return self.service

    async def stop(self) -> None:
        await self.db.close()


@pytest.fixture
async def rig(tmp_path):
    r = Rig(tmp_path)
    await r.start()
    yield r
    await r.stop()


# --------------------------------------------------------------------- detect


class TestDetect:
    SENTINEL = detect.compile_sentinel("TOPUP")
    NAMES = tuple(f"node{i}" for i in range(10))

    def snap(self, **kw):
        return detect.Snapshot(now=1000.0, **{"names": self.NAMES, **kw})

    def test_a_healthy_provider_has_no_signal(self):
        assert detect.signals(self.snap(healthy_real=10, fetch_status=200)) == []
        assert detect.level([]) == detect.OK

    @pytest.mark.parametrize("status", [401, 403, 404])
    def test_the_fetch_answering_gone_is_dead(self, status):
        found = detect.signals(self.snap(fetch_status=status))
        assert found == [detect.FETCH_DEAD] and detect.level(found) == detect.DEAD

    @pytest.mark.parametrize("status", [500, 502, 503, None])
    def test_a_5xx_or_no_answer_is_never_dead(self, status):
        assert detect.signals(self.snap(fetch_status=status)) == []

    def test_dead_nodes_are_m91s_verdict(self):
        found = detect.signals(self.snap(nodes_dead=True))
        assert found == [detect.NODES_DEAD] and detect.level(found) == detect.DEAD

    def test_the_sentinel_is_off_by_default(self):
        names = [*self.NAMES, "TOPUP to unlock"]
        assert detect.signals(self.snap(names=names)) == []
        found = detect.signals(self.snap(names=names), sentinel=self.SENTINEL)
        assert found == [detect.SENTINEL] and detect.level(found) == detect.WARNING

    def test_pseudo_nodes_are_not_real_nodes(self):
        names = ["a", "b", "DIRECT", "REJECT-DROP", "TOPUP now", "剩余流量：未知｜官网"]
        assert detect.real_count(names, self.SENTINEL) == 2
        assert detect.real_count(names) == 3  # without a sentinel regex the top-up entry counts

    @pytest.mark.parametrize(("real", "shrunk"), [(10, False), (8, False), (7, True), (3, True)])
    def test_shrinking_by_30_percent(self, real, shrunk):
        names = [f"n{i}" for i in range(real)]
        found = detect.signals(self.snap(names=names, healthy_real=10))
        assert (detect.NODES_SHRUNK in found) is shrunk

    def test_shrinking_needs_a_healthy_snapshot(self):
        assert detect.signals(self.snap(names=["a"], healthy_real=None)) == []

    def test_userinfo_expiry_is_read_from_a_synthetic_header(self):
        soon = "upload=0; download=10; total=1000; expire=" + str(1000 + 2 * 86400)
        later = "upload=0; download=10; total=1000; expire=" + str(1000 + 9 * 86400)
        low = "upload=0; download=980; total=1000"
        assert detect.signals(self.snap(userinfo=soon)) == [detect.USERINFO_EXPIRY]
        assert detect.signals(self.snap(userinfo=later)) == []
        assert detect.signals(self.snap(userinfo=low)) == [detect.USERINFO_EXPIRY]

    @pytest.mark.parametrize("header", [None, "", "garbage", "total=0", "expire=0"])
    def test_an_empty_userinfo_stays_silent(self, header):
        assert detect.signals(self.snap(userinfo=header)) == []

    def test_signals_combine_in_a_fixed_order(self):
        found = detect.signals(self.snap(names=["a"], healthy_real=10, fetch_status=404,
                                         nodes_dead=True))
        assert found == [detect.FETCH_DEAD, detect.NODES_DEAD, detect.NODES_SHRUNK]
        assert detect.level(found) == detect.DEAD


# ------------------------------------------------------------------- validate


class TestValidate:
    def test_a_yaml_subscription(self):
        v = validate_content(yaml_body(5))
        assert v.real_count == 5 and v.protocols == {"vless": 5}
        assert yaml.safe_load(v.provider_yaml)["proxies"][0]["uuid"] == "id-0"

    @pytest.mark.parametrize("wrap", [True, False])
    def test_a_uri_list_base64_or_plain(self, wrap):
        v = validate_content(uri_body(wrap=wrap))
        assert v.real_count == 5
        assert v.protocols == {"vless": 1, "hysteria2": 1, "trojan": 1, "ss": 1, "vmess": 1}
        by_name = {p["name"]: p for p in yaml.safe_load(v.provider_yaml)["proxies"]}
        assert by_name["Node A"]["reality-opts"]["public-key"] == "KEY"
        assert by_name["Node D"]["cipher"] == "aes-256-gcm" and by_name["Node D"]["port"] == 8388
        assert by_name["vm"]["network"] == "ws"

    def test_too_few_nodes(self):
        with pytest.raises(Rejected) as err:
            validate_content(yaml_body(2))
        assert err.value.code == "too_few"

    def test_pseudo_nodes_do_not_count_toward_the_minimum(self):
        extra = [{"name": "TOPUP to unlock", "type": "vless", "server": "x.example.invalid",
                  "port": 1}]
        with pytest.raises(Rejected) as err:
            validate_content(yaml_body(2, extra=extra), sentinel=detect.compile_sentinel("TOPUP"))
        assert err.value.code == "too_few"

    @pytest.mark.parametrize("port", [0, 65536, "x", None])
    def test_a_bad_port(self, port):
        entries = proxies(4)
        entries[1]["port"] = port
        with pytest.raises(Rejected) as err:
            validate_content(yaml.safe_dump({"proxies": entries}).encode())
        assert err.value.code == "bad_port"

    def test_no_server(self):
        entries = proxies(4)
        entries[0]["server"] = " "
        with pytest.raises(Rejected) as err:
            validate_content(yaml.safe_dump({"proxies": entries}).encode())
        assert err.value.code == "bad_server"

    def test_duplicate_names_after_trimming(self):
        entries = proxies(4)
        entries[1]["name"] = entries[0]["name"] + "  "
        with pytest.raises(Rejected) as err:
            validate_content(yaml.safe_dump({"proxies": entries}).encode())
        assert err.value.code == "dup_names"

    @pytest.mark.parametrize("kind", ["direct", "reject"])
    def test_direct_and_reject_entries_are_refused(self, kind):
        entries = [*proxies(4), {"name": "x", "type": kind}]
        with pytest.raises(Rejected) as err:
            validate_content(yaml.safe_dump({"proxies": entries}).encode())
        assert err.value.code == "forbidden_type"

    def test_an_unknown_type(self):
        entries = proxies(4)
        entries[0]["type"] = "telnet"
        with pytest.raises(Rejected) as err:
            validate_content(yaml.safe_dump({"proxies": entries}).encode())
        assert err.value.code == "bad_type"

    def test_oversize(self):
        with pytest.raises(Rejected) as err:
            validate_content(yaml_body(501))
        assert err.value.code == "too_many"
        assert validate_content(yaml_body(500)).real_count == 500

    @pytest.mark.parametrize("body", [b"", b"<html>nope</html>", b"\xff\xfe\x00", b"a: b",
                                      b"- 1\n- 2"])
    def test_unknown_format(self, body):
        with pytest.raises(Rejected) as err:
            validate_content(body)
        assert err.value.code == "format"

    def test_same_as_the_current_subscription(self, tmp_path):
        first = validate_content(yaml_body(5))
        again = digest_of_file_text(first.provider_yaml)
        assert again == first.digest
        with pytest.raises(Rejected) as err:
            validate_content(yaml_body(5), current_digest=again)
        assert err.value.code == "same"
        assert validate_content(yaml_body(6), current_digest=again).real_count == 6


class TestUrlHygiene:
    def resolve(self, addresses):
        return lambda _host: addresses

    @pytest.mark.parametrize(("url", "code"), [
        ("ftp://sub.example.invalid/x", "url_scheme"),
        ("file:///etc/passwd", "url_scheme"),
        ("javascript:alert(1)", "url_scheme"),
        ("https://user:pw@example.invalid/x", "url_userinfo"),
        ("https://user@example.invalid/x", "url_userinfo"),
        ("https://sub.example.invalid/" + "a" * 2100, "url_long"),
        ("https:///nohost", "url_invalid"),
    ])
    def test_refused(self, url, code):
        with pytest.raises(Rejected) as err:
            check_url(url, resolve=self.resolve(PUBLIC))
        assert err.value.code == code

    @pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.0.1",
                                         "169.254.1.1", "::1", "0.0.0.0"])
    def test_a_host_that_resolves_private_is_refused(self, address):
        with pytest.raises(Rejected) as err:
            check_url("https://sub.example.invalid/x", resolve=self.resolve([address]))
        assert err.value.code == "url_private"

    def test_one_private_answer_among_public_ones_is_enough_to_refuse(self):
        with pytest.raises(Rejected):
            check_url("https://sub.example.invalid/x", resolve=self.resolve([*PUBLIC, "10.0.0.9"]))

    def test_a_literal_private_ip_is_refused_without_a_lookup(self):
        def boom(_h):
            raise AssertionError("no lookup for a literal")

        with pytest.raises(Rejected) as err:
            check_url("http://127.0.0.1:9090/x", resolve=boom)
        assert err.value.code == "url_private"

    def test_the_private_switch(self):
        assert check_url("http://127.0.0.1/x", allow_private=True) == "127.0.0.1"

    def test_unresolvable(self):
        with pytest.raises(Rejected) as err:
            check_url("https://sub.example.invalid/x", resolve=self.resolve([]))
        assert err.value.code == "url_unresolved"

    def test_a_good_url(self):
        assert check_url(URL, resolve=self.resolve(PUBLIC)) == "sub.example.invalid"

    def test_a_host_that_would_go_through_a_node_is_not_fetched(self):
        def no_fetch(_url):
            raise AssertionError("must not fetch through a node")

        for verdict in (False, None):
            with pytest.raises(Rejected) as err:
                validate_url(URL, fetch=no_fetch, is_direct=lambda _h, v=verdict: v,
                             resolve=self.resolve(PUBLIC))
            assert err.value.code == "not_direct"

    def test_fetch_errors_and_statuses_become_rejections(self):
        def boom(_url):
            raise FetchError("timeout")

        kw = {"is_direct": lambda _h: True, "resolve": self.resolve(PUBLIC)}
        with pytest.raises(Rejected) as err:
            validate_url(URL, fetch=boom, **kw)
        assert (err.value.code, err.value.detail) == ("fetch", "timeout")
        with pytest.raises(Rejected) as err:
            validate_url(URL, fetch=lambda _u: Fetched(404), **kw)
        assert (err.value.code, err.value.detail) == ("status", "404")

    def test_a_valid_fetch(self):
        v, fetched = validate_url(URL, fetch=lambda _u: Fetched(200, yaml_body(4), {"x": "y"}),
                                  is_direct=lambda _h: True, resolve=self.resolve(PUBLIC))
        assert v.real_count == 4 and fetched.headers == {"x": "y"}


# ---------------------------------------------------------------------- switch


class TestSwitch:
    def setup(self, tmp_path, **kw):
        config = make_config(tmp_path, **kw)
        old = seed_file(config)
        mihomo = FakeMihomo(Path(config.provider_file), old)
        switcher = Switcher(config, mihomo.client(), clock=Ticker(), sleep=lambda _s: None)
        return config, mihomo, switcher

    def test_the_order_of_operations(self, tmp_path):
        config, mihomo, switcher = self.setup(tmp_path)
        active = Path(config.provider_file)
        new = validate_content(yaml_body(5, "new"))
        seen = {}

        def checkpoint(meta):
            seen["checkpoint"] = dict(meta)
            seen["new_at_checkpoint"] = switcher.new_path.exists()
            seen["backup_at_checkpoint"] = Path(meta["backup"]).exists()
            seen["active_still_old"] = "old0" in active.read_text()

        def at_put(_m):
            seen["new_at_put"] = switcher.new_path.exists()
            seen["active_new_at_put"] = "new0" in active.read_text()

        mihomo.on_put = at_put
        result = switcher.switch(new, checkpoint=checkpoint)
        assert result.ok and result.reason == ""
        # written and backed up first, the active file replaced last, then the one PUT
        assert seen["new_at_checkpoint"] and seen["backup_at_checkpoint"]
        assert seen["active_still_old"]
        assert seen["new_at_put"] is False and seen["active_new_at_put"] is True
        assert mihomo.writes == [("PUT", "/providers/proxies/main")]
        assert mihomo.names == list(new.names)
        assert not switcher.new_path.exists()
        [backup] = switcher.backups()
        assert "old0" in backup.read_text() and ".bak-" in backup.name
        assert seen["checkpoint"]["digest"] == new.digest

    def test_a_verify_failure_restores_the_backup_and_puts_again(self, tmp_path):
        config, mihomo, switcher = self.setup(tmp_path)
        mihomo.reload_works = False

        def works_the_second_time(m):
            m.reload_works = len(m.writes) >= 2

        # the first PUT is ignored by mihomo; the restoring PUT is not
        mihomo.on_put = works_the_second_time
        result = switcher.switch(validate_content(yaml_body(5, "new")))
        assert not result.ok and result.reason == "not_reloaded"
        assert result.rolled_back and result.restored_ok is True
        assert [m for m, _ in mihomo.writes] == ["PUT", "PUT"]
        assert "old0" in Path(config.provider_file).read_text()
        assert mihomo.names[0].startswith("old")
        assert not switcher.new_path.exists()

    def test_no_node_answering_is_a_failure(self, tmp_path):
        _config, mihomo, switcher = self.setup(tmp_path)

        def die(m):
            m.alive = False

        mihomo.on_put = die
        result = switcher.switch(validate_content(yaml_body(5, "new")))
        assert result.reason == "no_alive_node" and result.rolled_back
        assert result.restored_ok is False  # still nothing answers: said plainly, no loop
        assert len(mihomo.writes) == 2

    def test_a_count_mismatch_is_a_failure(self, tmp_path):
        _config, mihomo, switcher = self.setup(tmp_path)

        def lose_one(m):
            if len(m.writes) == 1:
                m.names = m.names[:-1]

        mihomo.after_put = lose_one
        result = switcher.switch(validate_content(yaml_body(5, "new")))
        assert result.reason == "count_mismatch" and result.restored_ok is True

    def test_telegram_exit_pointing_at_a_vanished_node(self, tmp_path):
        _config, mihomo, switcher = self.setup(tmp_path)
        mihomo.tg_exit = mihomo.names[0]  # an old node the new list does not have
        result = switcher.switch(validate_content(yaml_body(5, "new")))
        assert result.reason == "tg_exit_missing" and result.rolled_back

    def test_telegram_exit_on_direct_is_a_failure(self, tmp_path):
        _config, mihomo, switcher = self.setup(tmp_path)
        mihomo.tg_exit = "DIRECT"
        assert switcher.switch(validate_content(yaml_body(5, "new"))).reason == "tg_no_exit"

    def test_a_crash_between_the_rename_and_the_verify_resumes_into_rollback(self, tmp_path):
        config, mihomo, switcher = self.setup(tmp_path)
        meta = {}

        def crash(_m):
            raise KeyboardInterrupt  # the process dies after the rename

        mihomo.on_put = crash
        with pytest.raises(KeyboardInterrupt):
            switcher.switch(validate_content(yaml_body(5, "new")), checkpoint=meta.update)
        assert "new0" in Path(config.provider_file).read_text()  # renamed, never verified
        mihomo.on_put = None
        result = switcher.recover(meta)
        assert result.reason == "interrupted" and result.rolled_back and result.restored_ok
        assert "old0" in Path(config.provider_file).read_text()
        assert mihomo.names[0].startswith("old")
        assert not switcher.new_path.exists()

    def test_a_crash_before_the_rename_leaves_the_old_file_and_no_new_file(self, tmp_path):
        config, mihomo, switcher = self.setup(tmp_path)
        meta = {}

        def crash_after_backup(m):
            meta.update(m)
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            switcher.switch(validate_content(yaml_body(5, "new")), checkpoint=crash_after_backup)
        assert not switcher.new_path.exists()  # never left behind, even on a crash
        result = switcher.recover(meta)
        assert result.reason == "interrupted" and not result.rolled_back
        assert "old0" in Path(config.provider_file).read_text()
        assert mihomo.writes == []

    def test_backups_are_pruned_to_five(self, tmp_path):
        config, _mihomo, switcher = self.setup(tmp_path)
        for i in range(BACKUPS_KEPT + 3):
            assert switcher.switch(validate_content(yaml_body(4 + i % 2, f"v{i}"))).ok
        backups = switcher.backups()
        assert len(backups) == BACKUPS_KEPT
        assert backups == sorted(backups, key=lambda p: p.name, reverse=True)
        assert not switcher.new_path.exists()
        assert len([p for p in Path(config.provider_file).parent.iterdir()]) == BACKUPS_KEPT + 1

    def test_rollback_restores_the_newest_backup(self, tmp_path):
        config, _mihomo, switcher = self.setup(tmp_path)
        assert switcher.switch(validate_content(yaml_body(5, "new"))).ok
        result = switcher.rollback()
        assert result.ok and "old0" in Path(config.provider_file).read_text()

    def test_rollback_without_a_backup(self, tmp_path):
        _config, _mihomo, switcher = self.setup(tmp_path)
        assert switcher.rollback().reason == "no_backup"


class TestWhitelist:
    def test_exactly_one_new_path(self):
        sent = []
        client = MihomoClient("http://m", fetch=lambda u: {}, send=lambda *a: sent.append(a))
        client.reload_nodes()
        assert sent == [("PUT", "http://m/providers/proxies/main", None)]

    @pytest.mark.parametrize(("method", "path"), [
        ("PUT", "/configs"), ("PUT", "/configs?force=true"), ("PATCH", "/configs"),
        ("POST", "/providers/proxies/main"), ("GET", "/providers/proxies/main"),
        ("DELETE", "/providers/proxies/main"), ("PUT", "/providers/proxies/other"),
        ("PUT", "/providers/proxies/main/"), ("PUT", "/providers/proxies/main/healthcheck"),
        ("PUT", "/providers/proxies/"), ("PUT", "/providers/proxies/main/../../configs"),
    ])
    def test_everything_else_is_still_refused(self, method, path):
        client = MihomoClient("http://m", fetch=lambda u: {},
                              send=lambda *a: pytest.fail("a request was made"))
        with pytest.raises(ForbiddenWrite):
            client._write(method, path, {})  # noqa: SLF001

    def test_the_whitelist_has_exactly_the_old_rules_plus_one(self):
        from tgmd.traffic.mihomo import _ALLOWED_WRITES

        assert len(_ALLOWED_WRITES) == 4
        patterns = {(verb, rule.pattern) for verb, rule in _ALLOWED_WRITES}
        assert ("PUT", r"^/providers/rules/direct-auto$") in patterns
        assert ("PUT", r"^/providers/proxies/main$") in patterns


# ------------------------------------------------------------------------ flow


def good_page(count=5, prefix="new") -> Fetched:
    return Fetched(200, yaml_body(count, prefix), {})


class TestService:
    async def test_a_valid_url_is_switched_without_a_gate(self, rig):
        rig.pages[URL] = good_page()
        out = await rig.service.accept_url(URL)
        assert out.kind == "switched" and out.old_real == 4 and out.validated.real_count == 5
        assert rig.mihomo.names[0].startswith("new")
        assert await rig.service.stored_url() == URL
        assert rig.mihomo.writes == [("PUT", "/providers/proxies/main")]
        case = await rig.db.sub_case_last()
        assert case["state"] == "done"

    async def test_the_old_url_is_kept_for_a_rollback(self, rig):
        rig.pages[URL], rig.pages[URL2] = good_page(5, "a"), good_page(6, "b")
        await rig.service.accept_url(URL)
        await rig.service.accept_url(URL2)
        assert await rig.service.stored_url() == URL2
        result = await rig.service.rollback()
        assert result.ok and await rig.service.stored_url() == URL
        assert rig.mihomo.names[0].startswith("a")

    async def test_a_rejected_url_changes_nothing(self, rig):
        rig.pages[URL] = Fetched(200, yaml_body(2), {})
        before = Path(rig.config.provider_file).read_text()
        out = await rig.service.accept_url(URL)
        assert (out.kind, out.code) == ("rejected", "too_few")
        assert Path(rig.config.provider_file).read_text() == before
        assert rig.mihomo.writes == [] and await rig.service.stored_url() == ""
        case = await rig.db.sub_case_open()
        assert case["state"] == "awaiting_url" and case["last_error"] == "too_few"

    async def test_a_failed_verify_rolls_back_and_keeps_the_old_url(self, rig):
        rig.pages[URL] = good_page()
        rig.mihomo.reload_works = False
        out = await rig.service.accept_url(URL)
        assert out.kind == "failed" and out.code == "not_reloaded"
        assert await rig.service.stored_url() == ""
        assert "new0" not in Path(rig.config.provider_file).read_text()
        assert (await rig.db.sub_case_last())["state"] == "failed"

    async def test_the_same_content_is_not_switched_again(self, rig):
        rig.pages[URL] = good_page()
        await rig.service.accept_url(URL)
        out = await rig.service.accept_url(URL)
        assert (out.kind, out.code) == ("rejected", "same")

    async def test_without_a_provider_file_it_stops_at_staged(self, tmp_path):
        r = Rig(tmp_path, provider_file="")
        service = await r.start()
        try:
            r.pages[URL] = good_page()
            out = await service.accept_url(URL)
            assert out.kind == "staged" and r.mihomo.writes == []
            assert (await service.switch_staged()).code == "no_file"
        finally:
            await r.stop()

    async def test_switch_staged_after_a_failure_runs_again(self, rig):
        rig.pages[URL] = good_page()
        rig.mihomo.reload_works = False
        assert (await rig.service.accept_url(URL)).kind == "failed"
        rig.mihomo.reload_works = True
        out = await rig.service.switch_staged()
        assert out.kind == "switched" and await rig.service.stored_url() == URL

    async def test_nothing_staged(self, rig):
        assert (await rig.service.switch_staged()).kind == "nothing"

    async def test_a_restart_in_the_middle_of_a_switch_rolls_back(self, tmp_path):
        r = Rig(tmp_path)
        service = await r.start()
        try:
            r.pages[URL] = good_page()
            r.mihomo.on_put = lambda _m: (_ for _ in ()).throw(KeyboardInterrupt())
            with pytest.raises(KeyboardInterrupt):
                await service.accept_url(URL)
            assert (await r.db.sub_case_open())["state"] == "switching"
            assert "new0" in Path(r.config.provider_file).read_text()
            r.mihomo.on_put = None
            await service.resume()
            assert "old0" in Path(r.config.provider_file).read_text()
            assert (await r.db.sub_case_last())["state"] == "failed"
            assert r.said[-1][0] == "switch_failed"
            assert not service.switcher.new_path.exists()
        finally:
            await r.stop()

    async def test_the_direct_check_uses_live_connections(self, tmp_path):
        r = Rig(tmp_path)
        service = await r.start()
        service._sub_hosts = ()  # noqa: SLF001
        try:
            host = "sub.example.invalid"
            assert service._is_direct(host) is None  # noqa: SLF001
            r.mihomo.connections = [{"metadata": {"host": host}, "chains": ["DIRECT"]}]
            assert service._is_direct(host) is True  # noqa: SLF001
            r.mihomo.connections = [{"metadata": {"host": host}, "chains": ["n1", "PROXY"]}]
            assert service._is_direct(host) is False  # noqa: SLF001
        finally:
            await r.stop()


class TestRefresh:
    async def test_a_changed_subscription_is_switched_silently(self, rig):
        rig.pages[URL] = good_page(4, "a")
        await rig.service.accept_url(URL)
        rig.pages[URL] = good_page(5, "b")
        out = await rig.service.refresh()
        assert out.kind == "switched" and rig.said == []
        assert rig.mihomo.names[0].startswith("b")

    async def test_a_big_change_is_reported_and_an_unchanged_one_is_not(self, rig):
        rig.pages[URL] = good_page(4, "a")
        await rig.service.accept_url(URL)
        assert (await rig.service.refresh()).kind == "nothing"
        rig.pages[URL] = good_page(8, "b")
        await rig.service.refresh()
        assert rig.said == [("refreshed", {"old": 4, "new": 8})]

    async def test_a_failed_refresh_never_writes_the_file_and_says_so_once(self, rig):
        rig.pages[URL] = good_page(4, "a")
        await rig.service.accept_url(URL)
        text = Path(rig.config.provider_file).read_text()
        writes = len(rig.mihomo.writes)
        rig.pages[URL] = FetchError("timeout")
        for _ in range(3):
            assert (await rig.service.refresh()).kind == "rejected"
        assert Path(rig.config.provider_file).read_text() == text
        assert len(rig.mihomo.writes) == writes
        assert [k for k, _ in rig.said] == ["refresh_failed"]

    async def test_no_stored_url_means_nothing_to_refresh(self, rig):
        assert await rig.service.refresh() is None and rig.fetches == []

    async def test_a_dead_link_shows_up_as_a_signal(self, rig):
        rig.pages[URL] = good_page(4, "a")
        await rig.service.accept_url(URL)
        rig.pages[URL] = Fetched(404)
        await rig.service.check_fetch()
        assert rig.service.state["fetch"]["status"] == 404
        await rig.service.on_health(sick=False, alive=4, total=4)
        case = await rig.db.sub_case_open()
        assert case["signals"] == ["fetch_dead"]
        rig.pages[URL] = Fetched(503)
        await rig.service.check_fetch()
        assert rig.service.state["fetch"]["status"] == 503


class TestCases:
    async def test_a_dead_subscription_opens_one_case_and_pushes_the_checklist(self, rig):
        await rig.service.on_health(sick=True, alive=0, total=4)
        await rig.service.on_health(sick=True, alive=0, total=4)
        [(kind, data)] = rig.said
        assert kind == "case" and data["level"] == "dead" and data["signals"] == ["nodes_dead"]
        assert (await rig.db.sub_case_open())["state"] == "open"

    async def test_a_warning_also_opens_a_case(self, rig):
        rig.service._sentinel = detect.compile_sentinel("old0")  # noqa: SLF001
        await rig.service.on_health(sick=False, alive=4, total=4)
        assert rig.said[0][1]["level"] == "warning"

    async def test_healthy_records_the_baseline_and_shrinking_warns(self, rig):
        await rig.service.on_health(sick=False, alive=4, total=4)
        assert rig.said == [] and rig.service.state["healthy_real"] == 4
        rig.mihomo.names = rig.mihomo.names[:2]
        await rig.service.on_health(sick=False, alive=2, total=2)
        assert rig.said[0][1]["signals"] == ["nodes_shrunk"]

    async def test_reminders_are_capped_at_three_twelve_hours_apart(self, rig):
        await rig.service.on_health(sick=True, alive=0, total=4)
        opened = rig.now
        await rig.service.tick(opened + 11 * 3600)
        assert [k for k, _ in rig.said] == ["case"]
        for n in range(1, 6):
            await rig.service.tick(opened + n * 12 * 3600 + 1)
        assert [k for k, _ in rig.said] == ["case", "reminder", "reminder", "reminder"]
        assert [d["number"] for k, d in rig.said if k == "reminder"] == [1, 2, 3]

    async def test_the_later_button_and_the_skip_button(self, rig):
        await rig.service.on_health(sick=True, alive=0, total=4)
        await rig.service.later()
        await rig.service.tick(rig.now + 5 * 3600)
        assert len(rig.said) == 1
        await rig.service.tick(rig.now + 6 * 3600 + 1)
        assert rig.said[-1][0] == "reminder"
        await rig.service.skip()
        await rig.service.tick(rig.now + 100 * 3600)
        assert [k for k, _ in rig.said].count("reminder") == 1

    async def test_recovery_message_when_the_signal_clears_by_itself(self, rig):
        await rig.service.on_health(sick=True, alive=0, total=4)
        await rig.service.on_health(sick=False, alive=4, total=4)
        assert [k for k, _ in rig.said] == ["case", "recovered"]
        assert (await rig.db.sub_case_open()) is None
        await rig.service.on_health(sick=False, alive=4, total=4)
        assert len(rig.said) == 2
        await rig.service.on_health(sick=True, alive=0, total=4)  # it returns: a new case
        assert [k for k, _ in rig.said][-1] == "case"

    async def test_the_button_arms_a_ten_minute_window(self, rig):
        await rig.service.on_health(sick=True, alive=0, total=4)
        assert not await rig.service.armed()
        await rig.service.arm()
        assert await rig.service.armed()
        rig.now += 599
        assert await rig.service.armed()
        rig.now += 2
        assert not await rig.service.armed()

    async def test_a_failed_case_does_not_reopen_while_the_signal_stays(self, rig):
        await rig.service.on_health(sick=True, alive=0, total=4)
        rig.pages[URL] = Fetched(200, yaml_body(2), {})
        await rig.service.accept_url(URL)
        rig.mihomo.reload_works = False
        rig.pages[URL] = good_page()
        await rig.service.accept_url(URL)  # fails, case -> failed, closed
        count = len(rig.said)
        await rig.service.on_health(sick=True, alive=0, total=4)
        assert len(rig.said) == count


# -------------------------------------------------------------------------- ui


class Client:
    def __init__(self, fail=False):
        self.deleted, self.fail = [], fail

    async def delete_messages(self, chat, ids):
        if self.fail:
            raise RuntimeError(f"cannot delete in {chat}")
        self.deleted.append((chat, list(ids)))


class Event:
    _next_id: int = 100

    def __init__(self, text="", *, sender=ADMIN, data=b"", client=None):
        self.raw_text = text
        self.sender_id = self.chat_id = sender
        self.is_private = True
        self.message = object()
        Event._next_id += 1
        self.id = Event._next_id
        self.data = data
        self.client = client or Client()
        self.replies, self.answers = [], []

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return self

    async def answer(self, message=None, **kwargs):
        self.answers.append((message, kwargs))


def labels(kwargs):
    markup = kwargs.get("buttons")
    return [] if markup is None else [(b.text, b.type.data) for r in markup.rows for b in r.buttons]


def make_handlers(service):
    config = Config()
    config.access.admin_user_ids = [ADMIN]
    handlers = BotHandlers(bot=object(), config=config, db=object(), queue=object(),
                           pikpak=object(), portal=object())
    handlers.attach_subscription(service)
    return handlers


class TestFlow:
    async def test_a_non_admin_is_ignored_silently(self, rig):
        handlers = make_handlers(rig.service)
        rig.pages[URL] = good_page()
        event = Event(f"/sub {URL}", sender=999)
        await handlers.handle_sub_command(event)
        await handlers.handle_sub_button(Event(data=b"sub:got", sender=999))
        assert event.replies == [] and event.client.deleted == []
        assert rig.fetches == [] and rig.mihomo.writes == []

    async def test_the_url_message_is_deleted_and_the_switch_reported_in_counts(self, rig):
        handlers = make_handlers(rig.service)
        rig.pages[URL] = good_page()
        event = Event(f"/sub {URL}")
        await handlers.handle_sub_command(event)
        assert event.client.deleted == [(ADMIN, [event.id])]
        text = event.replies[-1][0]
        assert "已切换" in text and "原 4 个" in text and "现 5 个" in text
        assert all(s not in str(event.replies) for s in SECRETS)

    async def test_a_failed_delete_does_not_stop_the_flow(self, rig, caplog):
        handlers = make_handlers(rig.service)
        rig.pages[URL] = good_page()
        event = Event(f"/sub {URL}", client=Client(fail=True))
        with caplog.at_level(logging.DEBUG):
            await handlers.handle_sub_command(event)
        assert "已切换" in event.replies[-1][0]
        assert "could not delete" in caplog.text and SECRETS[0] not in caplog.text

    async def test_the_button_arms_the_next_plain_message(self, rig):
        handlers = make_handlers(rig.service)
        rig.pages[URL] = good_page()
        # not armed: a pasted URL is left to the normal link handling
        assert not await ui.url_message(Event(URL), rig.service, True)
        press = Event(data=b"sub:got")
        await handlers.handle_sub_button(press)
        assert "10 分钟" in press.replies[0][0]
        other = Event("just some text")
        assert not await ui.url_message(other, rig.service, True)
        assert not await ui.url_message(Event(URL, sender=999), rig.service, False)
        event = Event(URL)
        assert await ui.url_message(event, rig.service, True)
        assert event.client.deleted and "已切换" in event.replies[-1][0]
        # the window is used up
        assert not await ui.url_message(Event(URL2), rig.service, True)

    async def test_the_url_is_taken_by_on_message_before_the_link_handling(self, rig):
        handlers = make_handlers(rig.service)
        rig.pages[URL] = good_page()
        await rig.service.arm()
        event = Event(URL)
        await handlers.on_message(event)
        assert "已切换" in event.replies[-1][0] and event.client.deleted

    async def test_rejections_are_explained(self, rig):
        handlers = make_handlers(rig.service)
        event = Event("/sub ftp://sub.example.invalid/x")
        await handlers.handle_sub_command(event)
        assert "只接受 http" in event.replies[-1][0]
        rig.pages[URL] = Fetched(200, b"hello")
        event = Event(f"/sub {URL}")
        await handlers.handle_sub_command(event)
        assert "无法识别" in event.replies[-1][0]

    async def test_status_and_usage(self, rig):
        handlers = make_handlers(rig.service)
        event = Event("/sub")
        await handlers.handle_sub_command(event)
        assert "真实 4 个" in event.replies[0][0]
        event = Event("/sub banana")
        await handlers.handle_sub_command(event)
        assert "/sub" in event.replies[0][0]

    async def test_rollback_is_immediate(self, rig):
        handlers = make_handlers(rig.service)
        rig.pages[URL] = good_page()
        await rig.service.accept_url(URL)
        event = Event("/sub rollback")
        await handlers.handle_sub_command(event)
        assert "已回滚" in event.replies[-1][0] and event.replies[-1][1].get("buttons") is None
        assert rig.mihomo.names[0].startswith("old")

    async def test_the_checklist_has_the_steps_the_buttons_and_no_mailbox(self, rig):
        text, buttons = ui.render("case", {"level": "dead", "signals": ["nodes_dead"],
                                           "alive": 0, "total": 4},
                                  login_hint="the hint <b>")
        assert text.count("\n") > 8 and "the hint &lt;b&gt;" in text
        assert "@" not in text and "http" not in text
        assert [d for _, d in labels({"buttons": buttons})] == [b"sub:got", b"sub:later",
                                                                 b"sub:skip"]
        assert next(lbl for lbl, _ in labels({"buttons": buttons})) == "我已拿到新链接"

    async def test_english_keys_exist_for_every_message(self):
        keys = [k for k in i18n.CATALOG["en"] if k.startswith("sub.")]
        assert keys and set(keys) == {k for k in i18n.CATALOG["zh"] if k.startswith("sub.")}


# ------------------------------------------------------------------- redaction


class TestRedaction:
    def test_redact_url(self):
        assert redact_url(URL) == "https://sub.example.invalid/…"
        assert redact_url("http://u:p@example.invalid:8080/a?b=c#d") \
            == "http://example.invalid/…"
        assert redact_url("nonsense") == "<invalid url>"
        assert redact_url("http://[bad") == "<invalid url>"
        assert scrub(f"failed for {URL}!", URL) == "failed for https://sub.example.invalid/…!"

    async def test_the_url_never_appears_anywhere_over_the_whole_flow(self, rig, caplog):
        handlers = make_handlers(rig.service)
        seen: list[str] = []
        with caplog.at_level(logging.DEBUG):
            await rig.service.on_health(sick=True, alive=0, total=4)
            # a rejection, an unreachable host, a bad body, a good one, a failed verify
            for page in (Fetched(200, yaml_body(2), {}), FetchError("network"), Fetched(404),
                         Fetched(200, b"nothing"), good_page(5, "a")):
                rig.pages[URL] = page
                event = Event(f"/sub {URL}")
                await handlers.handle_sub_command(event)
                seen += [t for t, _ in event.replies]
            rig.mihomo.reload_works = False
            rig.pages[URL2] = good_page(6, "b")
            event = Event(f"/sub {URL2}")
            await handlers.handle_sub_command(event)
            seen += [t for t, _ in event.replies]
            rig.mihomo.reload_works = True
            # refresh, check, failure paths, resume, rollback, status
            rig.pages[URL] = FetchError("timeout")
            await rig.service.refresh()
            await rig.service.check_fetch()
            await rig.service.tick(rig.now + 100 * 3600)
            await rig.service.resume()
            await rig.service.rollback()
            await handlers.handle_sub_command(Event("/sub"))
            await handlers.handle_sub_command(Event("/sub switch"))
            seen += [text for _, d in rig.said for text in map(str, d.values())]
            seen += [render_text for kind, d in rig.said
                     for render_text in [ui.render(kind, d)[0]]]
        audit = [f"{r['action']} {r['detail']}" for r in await rig.db.sub_audit_rows(500)]
        cases = []
        case = await rig.db.sub_case_last()
        cases.append(json.dumps(case))
        every = "\n".join([caplog.text, *seen, *audit, *cases, *rig.fetches,
                           json.dumps(rig.service.state)])
        for secret in SECRETS:
            assert secret not in every
        assert "sub.example.invalid/…" in "\n".join(audit)  # the redacted form is what is kept

    def test_a_staged_repr_hides_the_url(self):
        from tgmd.subscription.service import _Staged

        staged = _Staged(URL, validate_content(yaml_body(4)), 1)
        assert SECRETS[0] not in repr(staged) and SECRETS[0] not in str(staged)

    def test_fetch_errors_do_not_chain_the_url(self):
        from tgmd.subscription.fetch import make_fetcher

        with pytest.raises(FetchError) as err:
            make_fetcher("ua", timeout=0.2)("http://127.0.0.1:9/" + SECRETS[0])
        assert err.value.__cause__ is None and SECRETS[0] not in repr(err.value)


# --------------------------------------------------------------------- disabled


class Spy:
    def __init__(self):
        self.calls = []

    async def on_health(self, **kw):
        self.calls.append(kw)


class TestDisabled:
    def test_the_defaults_leave_everything_off(self):
        config = SubscriptionConfig()
        assert config.enabled is False and config.provider_file == ""
        assert config.sentinel_regex == "" and config.login_hint == ""

    def test_the_loader_reads_the_environment(self, monkeypatch):
        from tgmd.config import load_config

        assert load_config().subscription.enabled is False
        assert load_config().traffic.sub_hosts == ()
        monkeypatch.setenv("SUB_REVIVAL_ENABLED", "1")
        monkeypatch.setenv("SUB_HOSTS", "Sub.Example.Invalid, .other.example.invalid")
        monkeypatch.setenv("SUB_MIN_NODES", "5")
        config = load_config()
        assert config.subscription.enabled and config.subscription.min_nodes == 5
        assert config.traffic.sub_hosts == ("sub.example.invalid", "other.example.invalid")

    async def test_the_alert_tail_changes_only_when_revival_is_attached(self, tmp_path):
        from test_traffic_nodes import make

        from tgmd.traffic.store import TrafficStore

        store = TrafficStore(tmp_path / "t.sqlite3")
        store.open()
        try:
            plain, revived = make(store), make(store)
            spy = Spy()
            revived.manager.revival = spy
            for w in (plain, revived):
                for node in w.controller.nodes[:4]:
                    node["alive"] = False
                await w.manager.health(1000.0)
                await w.manager.health(1700.0)
            assert "Cowork" in plain.sent[0]
            assert "Cowork" not in revived.sent[0] and "/sub" in revived.sent[0]
            assert [c["sick"] for c in spy.calls] == [False, True]
        finally:
            store.close()

    async def test_a_sub_command_with_no_service_only_answers_admins(self):
        handlers = make_handlers(None)
        event = Event("/sub")
        await handlers.handle_sub_command(event)
        assert "未启用" in event.replies[0][0]
        other = Event("/sub", sender=1)
        await handlers.handle_sub_command(other)
        assert other.replies == []

    async def test_no_provider_is_ever_read_or_written_when_nothing_calls_it(self, tmp_path):
        mihomo = FakeMihomo(tmp_path / "x.yaml")
        mihomo.client()
        assert mihomo.writes == []
