"""The traffic report: one set of numbers for ``/traffic``, the daily summary
and ``python -m tgmd.traffic report``."""

from __future__ import annotations

import html as _html
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from ..i18n import t
from .pricing import short_node
from .store import Row, TrafficStore, period_for

MB = 1024 * 1024
GB = 1024 * MB

PERIOD_KEYS = {"today": "traffic.period.today", "yesterday": "traffic.period.yesterday",
               "7d": "traffic.period.week", "month": "traffic.period.month"}


def fmt_bytes(amount: float) -> str:
    """``1.84 GB`` for gigabytes, ``12.3 MB`` and ``480 KB`` below that."""
    if amount >= 0.01 * GB:
        return f"{amount / GB:.2f} GB"
    if amount >= MB:
        return f"{amount / MB:.1f} MB"
    return f"{amount / 1024:.0f} KB"


def fmt_cny(amount: float) -> str:
    return f"¥{amount:.2f}"


@dataclass
class Budgets:
    daily_cny: float = 0.0
    monthly_cny: float = 0.0
    daily_proxy_gb: float = 0.0


@dataclass
class Report:
    period: str
    rows: list[Row]
    top_hosts: list[tuple[str, str, str, int]]
    day_cost: float
    month_cost: float
    day_proxy_bytes: int
    budgets: Budgets
    exit_chain: list[str] = field(default_factory=list)
    gate: str = "open"
    gate_reason: str = ""
    media_rate: float = 0.0
    upload_rate: float = 0.0
    heavy_other: list[tuple[str, int]] = field(default_factory=list)

    @property
    def proxy(self) -> list[Row]:
        return sorted((r for r in self.rows if r.outbound == "proxy"), key=lambda r: -r.bytes)

    @property
    def direct(self) -> list[Row]:
        return [r for r in self.rows if r.outbound == "direct"]

    @property
    def unattributed(self) -> int:
        return sum(r.bytes for r in self.rows if r.category == "unattributed")

    @property
    def proxy_bytes(self) -> int:
        return sum(r.bytes for r in self.proxy)

    @property
    def proxy_cost(self) -> float:
        return sum(r.cost for r in self.proxy)

    @property
    def direct_bytes(self) -> int:
        return sum(r.bytes for r in self.direct)

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "proxy": {
                "bytes": self.proxy_bytes,
                "cost_cny": round(self.proxy_cost, 4),
                "rows": [
                    {"category": r.category, "node": r.node, "up": r.up, "down": r.down,
                     "cost_cny": round(r.cost, 4)}
                    for r in self.proxy
                ],
            },
            "direct": {
                "bytes": self.direct_bytes,
                "rows": [{"category": r.category, "up": r.up, "down": r.down}
                         for r in self.direct],
            },
            "unattributed_bytes": self.unattributed,
            "budgets": {
                "daily_cny": {"used": round(self.day_cost, 4), "limit": self.budgets.daily_cny},
                "monthly_cny": {"used": round(self.month_cost, 4),
                                "limit": self.budgets.monthly_cny},
                "daily_proxy_gb": {"used": round(self.day_proxy_bytes / GB, 4),
                                   "limit": self.budgets.daily_proxy_gb},
            },
            "top_hosts": [{"host": host, "category": category, "node": node, "bytes": total}
                          for host, category, node, total in self.top_hosts],
            "exit": self.exit_chain,
            "gate": {"state": self.gate, "reason": self.gate_reason,
                     "media_mbps": self.media_rate, "upload_mbps": self.upload_rate},
        }


def build_report(store: TrafficStore, period: str, now: datetime, tz: ZoneInfo, *,
                 budgets: Budgets, gate: str = "open", gate_reason: str = "",
                 media_rate: float = 0.0, upload_rate: float = 0.0,
                 exit_chain: list[str] | None = None) -> Report:
    span = period_for(period, now, tz)
    today = period_for("today", now, tz)
    month = period_for("month", now, tz)
    today_rows = store.rows(today)
    return Report(
        period=period,
        rows=store.rows(span),
        top_hosts=store.top_hosts(span),
        day_cost=sum(r.cost for r in today_rows if r.outbound == "proxy"),
        month_cost=sum(r.cost for r in store.rows(month) if r.outbound == "proxy"),
        day_proxy_bytes=sum(r.bytes for r in today_rows if r.outbound == "proxy"),
        budgets=budgets,
        exit_chain=exit_chain or [],
        gate=gate,
        gate_reason=gate_reason,
        media_rate=media_rate,
        upload_rate=upload_rate,
        heavy_other=[(h, n) for h, n in store.heavy_hosts(
            span, category="other", outbound="proxy", minimum=100 * MB)],
    )


def _label(category: str) -> str:
    return t(f"traffic.cat.{category}")


def describe_gate(gate: str, reason: str, media_rate: float, upload_rate: float) -> str:
    if gate == "paused":
        text = t("traffic.gate.paused")
    elif gate == "over_budget":
        text = t("traffic.gate.over_budget", budget=_budget_label(reason))
    else:
        text = t("traffic.gate.open")
    limits = []
    if media_rate:
        limits.append(t("traffic.gate.limit_down", mbps=f"{media_rate:g}"))
    if upload_rate:
        limits.append(t("traffic.gate.limit_up", mbps=f"{upload_rate:g}"))
    suffix = " · ".join(limits) if limits else t("traffic.gate.unlimited")
    return t("traffic.gate.state", state=text, limits=suffix)


def _budget_label(reason: str) -> str:
    name = reason.split(":", 1)[0]
    return t(f"traffic.budget.{name}") if name else ""


def render(report: Report, *, html: bool = True) -> str:
    """The report as bot HTML, or as plain text for the command line."""
    esc = _html.escape if html else (lambda text: text)

    def bold(text: str) -> str:
        return f"<b>{text}</b>" if html else text

    label = t(PERIOD_KEYS[report.period])
    lines = [bold(t("traffic.proxy.head", period=label, size=fmt_bytes(report.proxy_bytes),
                    cost=fmt_cny(report.proxy_cost)))]
    if not report.proxy:
        lines.append("  " + t("traffic.none"))
    for row in report.proxy:
        node = esc(short_node(row.node)) if row.node else ""
        if row.category == "telegram":
            body = t("traffic.line.telegram", down=fmt_bytes(row.down), up=fmt_bytes(row.up))
        else:
            body = f"{_label(row.category)} {fmt_bytes(row.bytes)}"
        lines.append(f"  {body}   {node}".rstrip())

    lines.append(bold(t("traffic.direct.head", period=label,
                        size=fmt_bytes(report.direct_bytes))))
    merged: dict[str, int] = {}
    for row in report.direct:
        key = "lan_model" if row.category in ("lan", "model") else row.category
        merged[key] = merged.get(key, 0) + row.bytes
    if merged:
        lines.append("  " + " · ".join(
            f"{_label(key)} {fmt_bytes(size)}"
            for key, size in sorted(merged.items(), key=lambda kv: -kv[1])))
    if report.unattributed:
        lines.append(t("traffic.unattributed", size=fmt_bytes(report.unattributed)))

    b = report.budgets
    parts = []
    if b.daily_cny:
        parts.append(t("traffic.budget.line_day", used=fmt_cny(report.day_cost),
                       limit=fmt_cny(b.daily_cny)))
    if b.monthly_cny:
        parts.append(t("traffic.budget.line_month", used=fmt_cny(report.month_cost),
                       limit=fmt_cny(b.monthly_cny)))
    if b.daily_proxy_gb:
        parts.append(t("traffic.budget.line_gb", used=fmt_bytes(report.day_proxy_bytes),
                       limit=f"{b.daily_proxy_gb:g} GB"))
    if parts:
        lines.append(t("traffic.budget.head") + " · ".join(parts))
    if report.exit_chain:
        lines.append(t("traffic.exit", chain=esc(" → ".join(
            [report.exit_chain[0], short_node(report.exit_chain[-1])]
            if len(report.exit_chain) > 1 else report.exit_chain))))
    lines.append(t("traffic.gate.head") + esc(describe_gate(
        report.gate, report.gate_reason, report.media_rate, report.upload_rate)))

    if report.top_hosts:
        lines.append("")
        lines.append(bold(t("traffic.top.head")))
        for index, (host, _category, node, total) in enumerate(report.top_hosts, start=1):
            lines.append(f"{index}. {esc(host)} — {fmt_bytes(total)}"
                         + (t("traffic.top.node", node=esc(short_node(node))) if node else ""))
    if report.heavy_other:
        lines.append("")
        lines.append(t("traffic.consider_rule"))
        for host, total in report.heavy_other:
            lines.append(f"  {esc(host)} — {fmt_bytes(total)}")
    return "\n".join(lines)
