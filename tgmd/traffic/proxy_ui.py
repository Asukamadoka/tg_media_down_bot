"""``/proxy``: node groups, speed tests and direct-first routing.

Callback data is ``proxy:<verb>:...`` under Telegram's 64 bytes. Node picks
carry an index into the list the picker last showed (node names do not fit).
"""

from __future__ import annotations

import contextlib
import html
import logging
import time

from telethon.errors import MessageNotModifiedError

from ..buttons import callback_buttons
from ..i18n import t
from .direct import DirectRouting
from .nodes import GROUPS, Busy, GroupStatus, NodeManager
from .pricing import short_node

log = logging.getLogger(__name__)

PAGE = 8
GROUP_LABEL = {"PROXY": "proxy.group.proxy", "TG-PICK": "proxy.group.tg"}
GROUP_SHORT = {"PROXY": "proxy.short.proxy", "TG-PICK": "proxy.short.tg"}
MODE_CODE = {"lat": "auto-latency", "spd": "auto-speed"}


def _when(stamp: float) -> str:
    if not stamp:
        return t("proxy.never")
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


def describe_group(status: GroupStatus) -> str:
    node = html.escape(short_node(status.node)) if status.node else "?"
    parts = [t("proxy.group.line", group=t(GROUP_LABEL[status.group]),
               mode=t(f"proxy.mode.{status.mode}"), node=node)]
    if status.latency_ms is not None:
        parts.append(t("proxy.latency", ms=status.latency_ms))
    if status.down_mbps:
        parts.append(t("proxy.speed", down=f"{status.down_mbps:.0f}",
                       up=f"{status.up_mbps or 0:.0f}"))
    return " · ".join(parts)


async def render_status(manager: NodeManager) -> str:
    lines = [f"<b>{t('proxy.title')}</b>"]
    try:
        for status in await manager.status():
            lines.append(describe_group(status))
    except Exception as exc:  # noqa: BLE001 - say what went wrong instead of nothing
        lines.append(t("proxy.unreachable", error=html.escape(str(exc)[:120])))
    if manager.alive_count is not None:
        alive, total = manager.alive_count
        lines.append(t("proxy.alive", alive=alive, total=total))
    last = await _last_probe(manager)
    lines.append(t("proxy.last_probe", when=_when(last)))
    if manager.last_error:
        lines.append(t("proxy.probe_error", error=html.escape(manager.last_error)))
    if manager.probing:
        lines.append(t("proxy.probing"))
    return "\n".join(lines)


async def _last_probe(manager: NodeManager) -> float:
    import asyncio

    return await asyncio.to_thread(manager._store.last_probe)  # noqa: SLF001


def main_buttons():
    rows = []
    for group in GROUPS:
        short = t(GROUP_SHORT[group])
        rows.append([
            (f"{short}·{t('proxy.btn.lat')}", f"proxy:mode:{group}:lat"),
            (f"{short}·{t('proxy.btn.spd')}", f"proxy:mode:{group}:spd"),
            (f"{short}·{t('proxy.btn.manual')}", f"proxy:pick:{group}:0"),
        ])
    rows.append([(t("proxy.btn.probe"), "proxy:probe"), (t("proxy.btn.direct"), "proxy:direct")])
    return callback_buttons(rows)


def _node_line(index: int, row: dict) -> str:
    bits = [f"{index}. {html.escape(short_node(row['name']))}"]
    if row.get("latency_ms") is not None:
        bits.append(f"{row['latency_ms']} ms")
    if row.get("down_mbps"):
        bits.append(f"{row['down_mbps']:.0f} Mbps")
    return " · ".join(bits)


async def render_picker(manager: NodeManager, group: str, page: int):
    nodes = await manager.alive_nodes()
    manager.pick_lists[group] = [n["name"] for n in nodes]
    pages = max(1, -(-len(nodes) // PAGE))
    page = max(0, min(page, pages - 1))
    chunk = nodes[page * PAGE:(page + 1) * PAGE]
    lines = [f"<b>{t('proxy.pick.title', group=t(GROUP_LABEL[group]))}</b> "
             f"({page + 1}/{pages})"]
    lines += [_node_line(page * PAGE + i + 1, row) for i, row in enumerate(chunk)]
    if not chunk:
        lines.append(t("proxy.pick.none"))
    numbers = [(str(page * PAGE + i + 1), f"proxy:set:{group}:{page * PAGE + i}")
               for i in range(len(chunk))]
    rows = [numbers[i:i + 4] for i in range(0, len(numbers), 4)]
    nav = []
    if page > 0:
        nav.append(("◀", f"proxy:pick:{group}:{page - 1}"))
    if page < pages - 1:
        nav.append(("▶", f"proxy:pick:{group}:{page + 1}"))
    nav.append((t("traffic.btn.back"), "proxy:show"))
    rows.append(nav)
    return "\n".join(lines), callback_buttons(rows)


_ICON = {"candidate": "✅", "failed": "❌", "applied": "🔀", "broken": "⚠️", "kept": "🔒"}


def render_direct(rows: list[dict]):
    lines = [f"<b>{t('direct.title')}</b>"]
    buttons: list[list[tuple[str, str]]] = []
    if not rows:
        lines.append(t("direct.none"))
    for row in rows[:20]:
        host = row["host"]
        detail = []
        if row.get("direct_ms") is not None and row.get("proxy_ms") is not None:
            detail.append(t("direct.latency", direct=row["direct_ms"], proxy=row["proxy_ms"]))
        if row.get("reason"):
            detail.append(html.escape(row["reason"]))
        state = t("direct.state." + row["state"])
        note = " (" + "; ".join(detail) + ")" if detail else ""
        lines.append(f"{_ICON.get(row['state'], '-')} <code>{html.escape(host)}</code> "
                     f"{state}{note}")
        wanted = []
        if row["state"] == "candidate":
            wanted = [(t("proxy.btn.set_direct"), f"proxy:dset:{host}"),
                      (t("proxy.btn.keep_proxy"), f"proxy:dkeep:{host}")]
        elif row["state"] in ("applied", "broken"):
            wanted = [(t("proxy.btn.restore"), f"proxy:drestore:{host}")]
        for label, data in wanted:
            if len(data.encode()) <= 64:
                buttons.append([(f"{label} {host}"[:40], data)])
    buttons.append([(t("proxy.btn.direct"), "proxy:direct"), (t("traffic.btn.back"), "proxy:show")])
    return "\n".join(lines), callback_buttons(buttons)


async def show(manager: NodeManager):
    return await render_status(manager), main_buttons()


async def handle_button(event, manager: NodeManager, direct: DirectRouting | None) -> None:
    """A press on a ``/proxy`` button. The caller has checked the sender is an admin."""
    import asyncio

    parts = event.data.decode().split(":", 3)
    verb = parts[1] if len(parts) > 1 else "show"
    answer = ""

    async def edit(text: str, buttons) -> None:
        with contextlib.suppress(MessageNotModifiedError):
            await event.edit(text, parse_mode="html", buttons=buttons, link_preview=False)

    try:
        if verb == "mode" and len(parts) == 4 and parts[2] in GROUPS and parts[3] in MODE_CODE:
            await manager.set_mode(parts[2], MODE_CODE[parts[3]])
            answer = t("proxy.ack.mode")
            await edit(*await show(manager))
        elif verb == "pick" and len(parts) == 4 and parts[2] in GROUPS:
            await edit(*await render_picker(manager, parts[2], int(parts[3])))
        elif verb == "set" and len(parts) == 4 and parts[2] in GROUPS:
            names = manager.pick_lists.get(parts[2]) or []
            index = int(parts[3])
            if index >= len(names):
                answer = t("proxy.pick.stale")
                await edit(*await render_picker(manager, parts[2], 0))
            else:
                await manager.set_mode(parts[2], "manual", names[index])
                answer = t("proxy.ack.manual")
                await edit(*await show(manager))
        elif verb == "probe":
            if manager.probing:
                answer = t("proxy.probing_short")
            else:
                await edit(t("proxy.probing"), None)
                run = await manager.probe_now()
                answer = t("proxy.ack.probe")
                text, buttons = await show(manager)
                if run is not None:
                    text += "\n" + t("proxy.probe_done", nodes=len(run.results),
                                     mb=f"{run.spent_bytes / (1024 * 1024):.0f}")
                await edit(text, buttons)
        elif verb == "direct" and direct is not None:
            await edit(t("direct.testing"), None)
            await direct.run_checks()
            await edit(*render_direct(await asyncio.to_thread(direct._store.direct_hosts)))  # noqa: SLF001
        elif verb in ("dset", "dkeep", "drestore") and direct is not None and len(parts) >= 3:
            host = ":".join(parts[2:])
            if verb == "dset":
                await direct.apply(host)
                answer = t("direct.ack.set")
            else:
                await direct.restore(host)
                answer = t("direct.ack.keep")
            await edit(*render_direct(await asyncio.to_thread(direct._store.direct_hosts)))  # noqa: SLF001
        else:
            await edit(*await show(manager))
    except Busy:
        await event.answer(t("nodes.busy"), alert=True)
        return
    except ValueError as exc:
        await event.answer(html.unescape(str(exc))[:150], alert=True)
        return
    except Exception as exc:
        log.exception("proxy button failed")
        await event.answer(t("proxy.failed", error=str(exc)[:100]), alert=True)
        return
    await event.answer(answer)
