"""``/sub`` and the revival messages (docs/wms/M9.6 §B, §C). Admins only: anyone else is
ignored silently. Callback data is ``sub:<verb>`` and is not translated."""

from __future__ import annotations

import html
import logging
import re
import time

from ..buttons import callback_buttons
from ..i18n import t
from ..traffic.pricing import short_node
from .redact import redact_url
from .service import Outcome, SubscriptionService

log = logging.getLogger(__name__)

_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)
NAMES_SHOWN = 8


def single_url(text: str) -> str | None:
    """The text, when it is one http(s) URL and nothing else."""
    text = (text or "").strip()
    return text if _URL.match(text) else None


def _signals(found: list[str], data: dict) -> str:
    facts = {k: data.get(k, "?") for k in ("alive", "total", "real", "healthy")}
    return "; ".join(t(f"sub.signal.{s}", **facts) for s in found) or t("sub.signal.manual")


def checklist_buttons():
    return callback_buttons([[(t("sub.btn.got"), "sub:got")],
                             [(t("sub.btn.later"), "sub:later"), (t("sub.btn.skip"), "sub:skip")]])


def render(kind: str, data: dict, *, login_hint: str = ""):
    """``(text, buttons)`` for a message the service pushes on its own."""
    if kind == "case":
        hint = f" ({html.escape(login_hint)})" if login_hint else ""
        head = t("sub.case.dead" if data.get("level") == "dead" else "sub.case.warning")
        seen = t("sub.case.seen", signals=_signals(data.get("signals") or [], data))
        return f"{head}\n{seen}\n\n{t('sub.case.steps', hint=hint)}", checklist_buttons()
    if kind == "reminder":
        return (t("sub.reminder", number=data["number"],
                  signals=_signals(data.get("signals") or [], data)), checklist_buttons())
    if kind == "recovered":
        return t("sub.recovered", alive=data.get("alive", "?"), total=data.get("total", "?")), None
    if kind == "refresh_failed":
        reason = data.get("code", "?") + (f" {data['detail']}" if data.get("detail") else "")
        return t("sub.refresh_failed", reason=reason), None
    if kind == "refreshed":
        return t("sub.refreshed", old=data["old"], new=data["new"]), None
    if kind == "switch_failed":
        return failure_text(data.get("reason", "?"), data.get("result")), None
    return "", None


def failure_text(reason: str, result) -> str:
    if reason == "interrupted":
        return t("sub.switch_failed.interrupted")
    if (result is not None and result.reason == "no_backup") or (
            result is not None and not result.rolled_back):
        return t("sub.switch_failed.no_backup", reason=reason)
    if result is not None and result.restored_ok is False:
        return t("sub.switch_failed.bad_restore", reason=reason)
    return t("sub.switch_failed", reason=reason)


def reject_text(code: str, detail: str = "") -> str:
    return t(f"sub.reject.{code}", detail=html.escape(detail))


def counts_text(outcome: Outcome, key: str) -> str:
    new = outcome.validated
    assert new is not None
    mix = ", ".join(f"{k} {v}" for k, v in sorted(new.protocols.items()))
    names = "\n".join(html.escape(short_node(n.name)) for n in new.nodes[:NAMES_SHOWN])
    if len(new.nodes) > NAMES_SHOWN:
        names += f"\n… +{len(new.nodes) - NAMES_SHOWN}"
    return t(key, old=outcome.old_real if outcome.old_real is not None else "?",
             new=new.real_count, mix=mix, names=names)


def _when(stamp) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else t("sub.never")


async def status_text(service: SubscriptionService) -> str:
    s = await service.status()
    return t("sub.status", real=s["real"] if s["real"] is not None else "?",
             total=s["total"] if s["total"] is not None else "?",
             refresh=_when(s["last_refresh"]), updated=html.escape(str(s["updated_at"] or "?")),
             case=(s["case"] or t("sub.none")) + (" · staged" if s["staged"] else ""),
             backups=html.escape(", ".join(b.rsplit(".bak-", 1)[-1] for b in s["backups"])
                                 or t("sub.none")),
             switching=t("sub.yes" if s["can_switch"] else "sub.no"))


async def _delete(event) -> None:
    """The message that held the URL goes; a failure is logged without it."""
    try:
        await event.client.delete_messages(event.chat_id, [event.id])
    except Exception as exc:  # noqa: BLE001
        log.warning("could not delete the subscription message (%s)", type(exc).__name__)


async def receive_url(event, service: SubscriptionService, url: str) -> None:
    await _delete(event)
    await event.reply(t("sub.validating"))
    outcome = await service.accept_url(url)
    log.info("subscription link from chat: %s -> %s", redact_url(url), outcome.kind)
    await _report(event.reply, outcome)


async def _report(reply, outcome: Outcome) -> None:
    if outcome.kind == "nothing":
        await reply(t("sub.nothing"), parse_mode="html")
    elif outcome.kind == "rejected":
        await reply(reject_text(outcome.code, outcome.detail), parse_mode="html")
    elif outcome.kind == "staged":
        await reply(counts_text(outcome, "sub.staged") + t("sub.staged.nofile"),
                    parse_mode="html")
    elif outcome.kind == "failed":
        await reply(failure_text(outcome.code, outcome.result))
    else:
        await reply(counts_text(outcome, "sub.switched"), parse_mode="html")


async def _rollback(reply, service: SubscriptionService) -> None:
    result = await service.rollback()
    await reply(t("sub.rollback.done") if result.ok
                else t("sub.rollback.failed", reason=result.reason or "verify"))


async def handle_command(event, service: SubscriptionService | None, is_admin: bool) -> None:
    if not is_admin:
        return  # silently
    if service is None:
        await event.reply(t("sub.off"))
        return
    parts = (event.raw_text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not arg:
        await event.reply(await status_text(service), parse_mode="html")
    elif arg.lower() == "switch":
        await _report(event.reply, await service.switch_staged())
    elif arg.lower() == "rollback":
        await _rollback(event.reply, service)
    elif "://" in arg and not any(c.isspace() for c in arg):  # check_url explains a refusal
        await receive_url(event, service, arg)
    else:
        await event.reply(t("sub.usage"), parse_mode="html")


async def handle_button(event, service: SubscriptionService | None, is_admin: bool) -> None:
    if not is_admin:
        await event.answer()
        return
    if service is None:
        await event.answer(t("sub.off"), alert=True)
        return
    verb = (event.data or b"").decode().partition(":")[2]
    if verb == "got":
        await service.arm()
        await event.answer()
        await event.reply(t("sub.armed"))
    elif verb == "later":
        await service.later()
        await event.answer(t("sub.later"))
    elif verb == "skip":
        await service.skip()
        await event.answer(t("sub.skipped"), alert=True)
    else:
        await event.answer()


async def url_message(event, service: SubscriptionService | None, is_admin: bool) -> bool:
    """A plain message that is the armed URL. True when it was taken."""
    if service is None or not is_admin or not await service.armed():
        return False
    url = single_url(event.raw_text or "")
    if url is None:
        return False
    await receive_url(event, service, url)
    return True
