"""``python -m tgmd.traffic report --period today|7d|month [--json]``

Run inside the bot container. Reads the meter's database directly, so it works
whether or not the bot is running, and prints the same numbers as ``/traffic``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .. import i18n
from ..config import ConfigError, load_config
from .gate import STATE_KEY
from .mihomo import MihomoClient
from .report import Budgets, build_report, render
from .service import TG_GROUP
from .store import TrafficStore


def _persisted_state(path) -> dict:
    """The gate state the bot saved, read without opening the bot's database for writing."""
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (STATE_KEY,)).fetchone()
        return json.loads(row[0]) if row else {}
    except (sqlite3.Error, ValueError):
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tgmd.traffic")
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="print the traffic report")
    report.add_argument("--period", choices=("today", "7d", "month"), default="today")
    report.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    i18n.set_language(config.language)
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
