"""``/traffic`` inline keyboards and button handling.

Callback data is ``traffic:<verb>:<period>`` with a few extra fields, always
under Telegram's 64 bytes.
"""

from __future__ import annotations

import logging

from telethon.errors import MessageNotModifiedError

from ..buttons import callback_buttons
from ..i18n import t

log = logging.getLogger(__name__)

PERIODS = ("today", "7d", "month")
RATES = (0, 2, 5, 10, 20)
"""Megabytes per second; 0 is "no limit"."""


def main_buttons(period: str, gate_open: bool):
    return callback_buttons([
        [(t("traffic.btn.today"), "traffic:show:today"),
         (t("traffic.btn.week"), "traffic:show:7d"),
         (t("traffic.btn.month"), "traffic:show:month")],
        [(t("traffic.btn.pause") if gate_open else t("traffic.btn.resume"),
          f"traffic:{'pause' if gate_open else 'resume'}:{period}"),
         (t("traffic.btn.rate"), f"traffic:rate:{period}")],
    ])


def rate_buttons(period: str):
    def label(mbps: int) -> str:
        return t("traffic.rate.none") if mbps == 0 else str(mbps)

    return callback_buttons([
        [(f"⬇ {label(m)}", f"traffic:set:media:{m}:{period}") for m in RATES],
        [(f"⬆ {label(m)}", f"traffic:set:up:{m}:{period}") for m in RATES],
        [(t("traffic.btn.back"), f"traffic:show:{period}")],
    ])


def rate_text(control) -> str:
    return (
        f"<b>{t('traffic.rate.head')}</b>\n"
        f"⬇ {t('traffic.rate.down')}: {_describe(control.rate_mbps('media'))}\n"
        f"⬆ {t('traffic.rate.up')}: {_describe(control.rate_mbps('upload'))}"
    )


def _describe(mbps: float) -> str:
    return t("traffic.rate.none") if not mbps else f"{mbps:g} MB/s"


async def show(service, period: str):
    """``(text, buttons)`` for the report of ``period``."""
    text = await service.render(period)
    return text, main_buttons(period, service.control.is_open)


async def handle_button(event, service) -> None:
    """A press on a ``/traffic`` button. The caller has checked the sender is an admin."""
    control = service.control
    parts = event.data.decode().split(":")
    verb = parts[1] if len(parts) > 1 else ""
    period = parts[-1] if parts[-1] in PERIODS else "today"
    answer = ""
    try:
        if verb == "pause":
            await control.pause()
            answer = t("traffic.ack.paused")
        elif verb == "resume":
            await control.resume()
            answer = t("traffic.ack.resumed")
        elif verb == "set" and len(parts) >= 5:
            kind = "media" if parts[2] == "media" else "upload"
            await control.set_rate(kind, float(int(parts[3])))
            answer = t("traffic.ack.rate")
        if verb == "rate" or (verb == "set" and answer):
            await event.edit(rate_text(control), parse_mode="html",
                             buttons=rate_buttons(period), link_preview=False)
        else:
            text, buttons = await show(service, period)
            await event.edit(text, parse_mode="html", buttons=buttons, link_preview=False)
    except MessageNotModifiedError:
        pass
    except Exception as exc:
        log.exception("traffic button failed")
        await event.answer(t("traffic.failed", error=str(exc)[:100]), alert=True)
        return
    await event.answer(answer)
