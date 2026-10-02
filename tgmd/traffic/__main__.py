"""``python -m tgmd.traffic report --period today|7d|month [--json]``

Run inside the bot container. Reads the meter's database directly, so it works
whether or not the bot is running, and prints the same numbers as ``/traffic``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .. import i18n
from ..config import ConfigError, load_config
from .gate import STATE_KEY
from .mihomo import TG_GROUP, MihomoClient
from .report import Budgets, build_report, render
from .store import TrafficStore


def _persisted_state(path) -> dict:
    """The gate state the bot saved, read without opening the bot's database for writing."""
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (STATE_KEY,)).fetchone()
        return json.loads(row[0]) if row else {}
    except (sqlite3.Error, ValueError):
        return {}


def _live(args, config) -> int:
    """``probe`` and ``direct-test``: real measurements through mihomo. They write only
    what the bot's own buttons write (PROBE, and FAST with ``--apply``), never the
    rules file."""
    import asyncio

    from .direct import DirectRouting
    from .gate import TrafficControl
    from .nodes import NodeManager
    from .probe import Prober, UrllibNet

    settings = config.traffic
    client = MihomoClient(settings.mihomo_api)
    net = UrllibNet(settings.probe_listener)
    store = TrafficStore(config.download.traffic_db_path)
    store.open()
    try:
        if args.command == "direct-test":
            routing = DirectRouting(settings, client, net, store, None)
            verdicts = [routing.check(host) for host in args.hosts]
            rows = [{"host": v.host, "ok": v.ok, "reason": v.reason, "direct_ms": v.direct_ms,
                     "proxy_ms": v.proxy_ms, "direct_mbps": v.direct_mbps,
                     "proxy_mbps": v.proxy_mbps} for v in verdicts]
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for r in rows:
                    print(f"{'ok  ' if r['ok'] else 'FAIL'} {r['host']}  direct "
                          f"{r['direct_ms']} ms, proxy {r['proxy_ms']} ms"
                          + (f"  ({r['reason']})" if r["reason"] else ""))
            return 0
        prober = Prober(settings, client, net, store)
        run = prober.run()
        switched = False
        if args.apply:
            manager = NodeManager(settings, client, store, None, TrafficControl(settings),
                                  prober)
            switched = asyncio.run(manager._update_fast(force=True))  # noqa: SLF001
        rows = [{"node": r.name, "latency_ms": r.latency_ms, "down_mbps": round(r.down_mbps, 1),
                 "up_mbps": round(r.up_mbps, 1), "price": r.price, "alive": r.alive}
                for r in run.results]
        rows += [{"node": name, "skipped": reason} for name, reason in run.skipped]
        if args.json:
            print(json.dumps({"nodes": rows, "spent_bytes": run.spent_bytes,
                              "fast_switched": switched}, ensure_ascii=False, indent=2))
        else:
            print(f"{'node':40} {'ms':>6} {'down':>8} {'up':>8} {'price':>6} alive")
            for r in rows:
                if "skipped" in r:
                    print(f"{r['node'][:40]:40} skipped ({r['skipped']})")
                else:
                    print(f"{r['node'][:40]:40} {r['latency_ms'] or '-':>6} "
                          f"{r['down_mbps']:>8} {r['up_mbps']:>8} {r['price'] or '-':>6} "
                          f"{'yes' if r['alive'] else 'no'}")
            print(f"used {run.spent_bytes / (1024 * 1024):.0f} MB"
                  + ("; FAST switched" if switched else ""))
        return 0
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tgmd.traffic")
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="print the traffic report")
    report.add_argument("--period", choices=("today", "7d", "month"), default="today")
    report.add_argument("--json", action="store_true", help="machine-readable output")
    probe = sub.add_parser("probe", help="measure every node, like /proxy 立即测速")
    probe.add_argument("--apply", action="store_true", help="also point FAST at the best node")
    probe.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser("ask-probe", help="ask the running bot to send the admins the probe question")
    direct = sub.add_parser("direct-test", help="test hosts direct vs. through the proxy")
    direct.add_argument("hosts", nargs="+", metavar="HOST")
    direct.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    i18n.set_language(config.language)
    if args.command in ("probe", "direct-test"):
        return _live(args, config)
    if args.command == "ask-probe":
        # No network and no Telegram: a row the running bot picks up within 30 seconds.
        store = TrafficStore(config.download.traffic_db_path)
        store.open()
        try:
            store.request_probe_ask(time.time())
        finally:
            store.close()
        print("asked: the bot sends the probe question within about 30 seconds")
        return 0

    path = config.download.traffic_db_path
    if not path.exists():
        print(f"no traffic data yet ({path} does not exist)", file=sys.stderr)
        return 1

    settings = config.traffic
    state = _persisted_state(config.download.db_path)
    paused, over = bool(state.get("paused")), state.get("over_budget") or ""
    chain: list[str] = []
    if settings.enabled:
        try:
            chain = MihomoClient(settings.mihomo_api).current_exit(TG_GROUP)
        except Exception:  # noqa: BLE001 - the exit line is optional
            chain = []

    def rate(kind: str) -> float:
        override = state.get(f"rate_{kind}")
        if override is not None:
            return float(override)
        return settings.media_rate_mbps if kind == "media" else settings.upload_rate_mbps

    store = TrafficStore(path)
    store.open()
    try:
        data = build_report(
            store, args.period, datetime.now(UTC), ZoneInfo(settings.timezone),
            budgets=Budgets(settings.budget_daily_cny, settings.budget_monthly_cny,
                            settings.budget_daily_proxy_gb),
            gate="paused" if paused else "over_budget" if over else "open",
            gate_reason=over, media_rate=rate("media"), upload_rate=rate("upload"),
            exit_chain=chain,
        )
    finally:
        store.close()
    print(json.dumps(data.as_dict(), ensure_ascii=False, indent=2) if args.json
          else render(data, html=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
