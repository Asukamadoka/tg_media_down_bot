"""Direct-first routing: find hosts that work without the proxy, and route them
DIRECT through a rule-provider file (docs/wms/M9.1 §B).

The bot never edits mihomo's config. It writes one text file, a ``domain``
rule-set mounted into its container, and asks mihomo to re-read it
(``PUT /providers/rules/direct-auto``). Telegram, the subscription host and
the LAN can never be put in it: :func:`validate_host` refuses them.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from ..i18n import t
from .classify import TELEGRAM_DOMAINS, TELEGRAM_NETWORKS
from .mihomo import MihomoClient
from .probe import DOWNLOAD_CAP, DOWNLOAD_SECONDS, HeadResult, Net
from .store import Period, TrafficStore

if TYPE_CHECKING:
    from ..config import TrafficConfig
    from ..db import Database

log = logging.getLogger(__name__)

STATE_KEY = "traffic:direct"
LATENCY_FACTOR = 3.0
SPEED_SHARE = 0.7
MIN_DIRECT_MIB_S = 2.0
DISCOVERY_EVERY = 24 * 3600.0
RECHECK_EVERY = 7 * 24 * 3600.0
MB = 1024 * 1024

Notify = Callable[..., Awaitable[None]]


def validate_host(host: str, sub_hosts: tuple[str, ...] = ()) -> str:
    """Why ``host`` may not be routed direct, or "" when it may. ``sub_hosts`` are the
    subscription's domains (``SUB_HOSTS``): its own rule routes them, never this list."""
    host = host.strip().lower().rstrip(".")
    if not host or "." not in host or " " in host or "/" in host:
        return "not a domain name"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if any(address in net for net in TELEGRAM_NETWORKS if net.version == address.version):
            return "Telegram"
        return "an IP address, not a domain"
    if any(host == d or host.endswith("." + d) for d in TELEGRAM_DOMAINS):
        return "Telegram"
    if any(host == d or host.endswith("." + d) for d in sub_hosts):
        return "the subscription host"
    if host.endswith((".local", ".lan", ".internal", ".home.arpa", ".localhost")):
        return "LAN"
    return ""


@dataclass
class Verdict:
    host: str
    ok: bool
    reason: str = ""
    direct_ms: int | None = None
    proxy_ms: int | None = None
    direct_mbps: float | None = None
    proxy_mbps: float | None = None


def judge(host: str, direct: HeadResult, proxy: HeadResult, *,
          direct_mbps: float | None = None, proxy_mbps: float | None = None) -> Verdict:
    """Direct "works" when TLS is valid, nothing reset or timed out, and latency is no
    more than 3x the proxy's; when throughput was measured, direct must also reach 70%
    of the proxy's or 2 MiB/s."""
    base = {"host": host, "direct_ms": direct.latency_ms, "proxy_ms": proxy.latency_ms,
            "direct_mbps": direct_mbps, "proxy_mbps": proxy_mbps}
    if not direct.tls_ok:
        return Verdict(ok=False, reason=direct.error or "TLS failed", **base)
    if not direct.reachable:
        return Verdict(ok=False, reason=direct.error or "unreachable", **base)
    if (proxy.reachable and proxy.latency_ms and direct.latency_ms is not None
            and direct.latency_ms > LATENCY_FACTOR * proxy.latency_ms):
        return Verdict(ok=False, reason="slow: latency", **base)
    if direct_mbps is not None and proxy_mbps is not None:
        floor = min(SPEED_SHARE * proxy_mbps, MIN_DIRECT_MIB_S * 8 * MB / 1e6)
        # "at least 70% of the proxy's speed or 2 MiB/s": either is enough.
        if direct_mbps < floor:
            return Verdict(ok=False, reason="slow: throughput", **base)
    return Verdict(ok=True, **base)


class DirectRouting:
    def __init__(self, config: TrafficConfig, client: MihomoClient, net: Net,
                 store: TrafficStore, db: Database | None, *, notify: Notify | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._config = config
        self._client = client
        self._net = net
        self._store = store
        self._db = db
        self._notify = notify
        self._clock = clock
        self._schedule = {"last_discovery": 0.0, "last_recheck": 0.0}
        self._busy = asyncio.Lock()
        self.last_error = ""

    # ----------------------------------------------------------- persistence

    async def load(self) -> None:
        if self._db is not None:
            data = await self._db.kv_get_json(STATE_KEY)
            if isinstance(data, dict):
                self._schedule.update({k: float(v) for k, v in data.items() if k in self._schedule})

    async def _save(self) -> None:
        if self._db is not None:
            await self._db.kv_set_json(STATE_KEY, self._schedule)

    # ------------------------------------------------------------ the rules file

    def hosts_in_file(self) -> list[str]:
        rows = self._store.direct_hosts()
        return sorted(r["host"] for r in rows if r["state"] in ("applied", "broken"))

    def write_file(self, hosts: list[str]) -> bool:
        """Write the rule-set atomically (temp file, then rename). True if it changed."""
        for host in hosts:
            if (why := validate_host(host, self._config.sub_hosts)):
                raise ValueError(f"{host}: {why}")
        path = Path(self._config.direct_rules_file)
        body = "".join(f"+.{host}\n" for host in sorted(set(hosts)))
        try:
            if path.read_text(encoding="utf-8") == body:
                return False
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".direct-auto.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            Path(tmp).chmod(0o644)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return True

    def publish(self) -> None:
        """Write the file from what is stored, and make mihomo re-read it."""
        self.write_file(self.hosts_in_file())
        self._client.reload_rules()

    async def sync_at_start(self) -> None:
        try:
            changed = await asyncio.to_thread(self.write_file, self.hosts_in_file())
            if changed:
                await asyncio.to_thread(self._client.reload_rules)
        except Exception as exc:  # noqa: BLE001 - mihomo or the mount may not be ready
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("could not sync the direct-auto rules at start: %s", exc)

    # -------------------------------------------------------------- candidates

    def candidates(self, now: datetime | None = None) -> list[str]:
        """Hosts worth testing: heavy proxied hosts of the last 24 h, plus the configured list."""
        from zoneinfo import ZoneInfo

        moment = now or datetime.now(UTC)
        period = Period("24h", moment - timedelta(hours=24), moment,
                        ZoneInfo(self._config.timezone))
        heavy = [h for h, _ in self._store.proxied_hosts(
            period, minimum=int(self._config.direct_candidate_mb * MB))]
        skip = {r["host"] for r in self._store.direct_hosts()
                if r["state"] in ("applied", "broken", "kept")}
        out: list[str] = []
        for host in [*heavy, *self._config.direct_probe_hosts]:
            host = host.strip().lower()
            if host in out or host in skip or validate_host(host, self._config.sub_hosts):
                continue
            out.append(host)
        return out

    # -------------------------------------------------------------------- tests

    def check(self, host: str) -> Verdict:
        """Blocking. Test ``host`` direct and through the proxy's current node."""
        if (why := validate_host(host, self._config.sub_hosts)):
            return Verdict(host, False, reason=why)
        url = self._config.direct_test_urls.get(host)
        proxy_node = self._client.now("AUTO-LATENCY")
        try:
            results = []
            for target in ("DIRECT", proxy_node or "DIRECT"):
                self._client.select("PROBE", target)
                head = self._net.head(host, url=url, seconds=DOWNLOAD_SECONDS)
                mbps = None
                if url and head.reachable:
                    try:
                        got, seconds = self._net.download(url, cap=DOWNLOAD_CAP,
                                                          seconds=DOWNLOAD_SECONDS)
                        mbps = got * 8 / 1e6 / max(seconds, 1e-6)
                    except Exception as exc:  # noqa: BLE001 - treated as "not measured"
                        log.info("throughput test of %s failed: %s", host, exc)
                results.append((head, mbps))
        finally:
            self._client.select("PROBE", "DIRECT")
        (direct, d_mbps), (proxy, p_mbps) = results
        return judge(host, direct, proxy, direct_mbps=d_mbps, proxy_mbps=p_mbps)

    async def run_checks(self, hosts: list[str] | None = None) -> list[Verdict]:
        """Test the candidates (or ``hosts``), one after another, and remember the verdicts."""
        async with self._busy:
            wanted = hosts if hosts is not None else self.candidates()
            verdicts = []
            for host in wanted:
                try:
                    verdict = await asyncio.to_thread(self.check, host)
                except Exception as exc:  # noqa: BLE001 - e.g. no PROBE group yet
                    self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                    log.warning("direct test of %s failed to run: %s", host, exc)
                    break
                verdicts.append(verdict)
                self._remember(verdict)
            return verdicts

    def _remember(self, v: Verdict) -> None:
        current = {r["host"]: r["state"] for r in self._store.direct_hosts()}.get(v.host)
        if current in ("applied", "broken"):
            state = "applied" if v.ok else "broken"
        elif current == "kept":
            state = "kept"
        else:
            state = "candidate" if v.ok else "failed"
        self._store.save_direct(
            v.host, state=state, reason=v.reason, direct_ms=v.direct_ms, proxy_ms=v.proxy_ms,
            direct_mbps=v.direct_mbps, proxy_mbps=v.proxy_mbps, now=self._clock())

    # ------------------------------------------------------------------ choices

    async def _set_state(self, host: str, state: str) -> None:
        row = next((r for r in self._store.direct_hosts() if r["host"] == host), None) or {}
        self._store.save_direct(
            host, state=state, reason=row.get("reason", ""), direct_ms=row.get("direct_ms"),
            proxy_ms=row.get("proxy_ms"), direct_mbps=row.get("direct_mbps"),
            proxy_mbps=row.get("proxy_mbps"), now=self._clock())

    async def apply(self, host: str) -> None:
        """Route ``host`` direct (``设为直连``)."""
        if (why := validate_host(host, self._config.sub_hosts)):
            raise ValueError(f"{host}: {why}")
        previous = next((r["state"] for r in self._store.direct_hosts() if r["host"] == host),
                        "candidate")
        await self._set_state(host, "applied")
        try:
            await asyncio.to_thread(self.publish)
        except Exception:
            await self._set_state(host, previous)
            raise

    async def restore(self, host: str) -> None:
        """Back to the proxy (``保持代理`` / ``恢复代理``); it is not suggested again."""
        previous = next((r["state"] for r in self._store.direct_hosts() if r["host"] == host),
                        "kept")
        await self._set_state(host, "kept")
        try:
            await asyncio.to_thread(self.publish)
        except Exception:
            await self._set_state(host, previous)
            raise

    # --------------------------------------------------------------------- tick

    async def tick(self, now: float | None = None) -> None:
        """A daily look for new candidates and a weekly re-test of what is applied."""
        now = now if now is not None else self._clock()
        if self._busy.locked():
            return
        if now - self._schedule["last_recheck"] >= RECHECK_EVERY:
            self._schedule["last_recheck"] = now
            await self._save()
            await self._recheck()
        if now - self._schedule["last_discovery"] >= DISCOVERY_EVERY:
            self._schedule["last_discovery"] = now
            await self._save()
            await self._discover()

    async def _discover(self) -> None:
        if not self._config.direct_auto_apply:
            return  # the owner asks for the list with 直连检测
        passed = [v for v in await self.run_checks() if v.ok]
        for verdict in passed:
            try:
                await self.apply(verdict.host)
            except Exception as exc:  # noqa: BLE001 - say so and go on with the others
                log.warning("could not route %s direct: %s", verdict.host, exc)
                continue
            await self._say(t("direct.auto_applied", host=verdict.host))

    async def _recheck(self) -> None:
        applied = [r["host"] for r in self._store.direct_hosts()
                   if r["state"] in ("applied", "broken")]
        if not applied:
            return
        before = {r["host"]: r["state"] for r in self._store.direct_hosts()}
        for verdict in await self.run_checks(applied):
            if not verdict.ok and before.get(verdict.host) == "applied":
                from ..buttons import callback_buttons

                data = f"proxy:drestore:{verdict.host}"
                buttons = (callback_buttons([[(t("proxy.btn.restore"), data)]])
                           if len(data.encode()) <= 64 else None)
                await self._say(t("direct.broken", host=verdict.host, reason=verdict.reason),
                                buttons)

    async def _say(self, text: str, buttons=None) -> None:
        log.info("direct: %s", text.replace("\n", " "))
        if self._notify is None:
            return
        try:
            await self._notify(text, buttons) if buttons else await self._notify(text)
        except Exception:
            log.warning("could not deliver a direct-routing message", exc_info=True)
