"""The PikPak warehouse (pikpak_wms) wired into the bot.

WMS never logs in on its own here: it borrows the PikPak account the bot
already has, so nobody types a password twice. Which account:

1. ``WMS_ACCOUNT``: a Telegram user id, or ``shared`` for the shared account;
2. else the first admin who connected their own account (Mini App or chat);
3. else the shared account from ``PIKPAK_USERNAME`` / ``PIKPAK_PASSWORD``.

Two ways in, both through :mod:`pikpak_wms.ops.embed` only:

* ``WMS_ENABLED=true`` runs WMS's scheduled jobs inside the bot;
* ``python -m tgmd.wms <command>`` (``wms`` in the image) runs one WMS
  command with the same account, e.g. ``docker compose run --rm bot wms plans``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from dotenv import load_dotenv
from pikpakapi import PikPakApi

from pikpak_wms.ops.embed import (
    AccountUnavailable,
    Clarification,
    EmbeddedWms,
    WmsError,
    run_command,
    set_language,
)

from . import bootstrap, i18n
from .buttons import callback_buttons
from .config import Config, load_config
from .db import Database
from .i18n import describe, t
from .pikpak import PikPakError, PikPakService
from .utils import escape_html

log = logging.getLogger(__name__)

__all__ = ["WmsError", "WmsInBot", "account_for", "main", "plan_message", "provider_for"]


async def account_for(config: Config, pikpak: PikPakService) -> int | None:
    """The user whose drive WMS manages; None means the shared account."""
    if config.wms.shared_account:
        return None
    if config.wms.account is not None:
        return config.wms.account
    for admin in config.access.admin_user_ids:
        if await pikpak.has_user_session(admin):
            return admin
    return None


def provider_for(config: Config, pikpak: PikPakService):
    """An async callable giving WMS a logged-in client, decided on every call
    (an admin may connect their account while the bot runs)."""

    async def provider() -> PikPakApi:
        user_id = await account_for(config, pikpak)
        try:
            return await pikpak.client(user_id)
        except PikPakError as exc:
            raise AccountUnavailable(str(exc), shown=describe(exc)) from exc

    return provider


SHELVE_DELAY = 20.0
"""Seconds of quiet after the last transfer before shelving, so a batch of
links becomes one plan rather than one per link."""

MERGE = "\uff0c"
"""The Chinese comma joining a sentence and the answer that refines it."""

PROPOSALS_KEPT = 50
"""Natural-language proposals remembered for their buttons; older ones expire."""

MESSAGE_LIMIT = 3500
"""Telegram's limit is 4096; the plan is cut well before it."""

Notify = Callable[[int, str, Any], Awaitable[None]]
"""Send ``text`` (HTML) with optional ``buttons`` to a chat."""


def plan_message(
    lines: list[str], plan_id: int, *, intro: str = "", details: bool = False
) -> tuple[str, Any]:
    """A plan as a chat message, with [apply] [discard] buttons, and with
    ``details`` a [details] button between them (M7 §4)."""
    body = "\n".join(lines)
    if len(body) > MESSAGE_LIMIT:
        body = body[:MESSAGE_LIMIT].rsplit("\n", 1)[0] + "\n…"
    text = (intro + "\n" if intro else "") + f"<pre>{escape_html(body)}</pre>"
    row = [(t("wms.button.apply"), f"wms:apply:{plan_id}")]
    if details:
        row.append((t("wms.button.details"), f"wms:detail:{plan_id}"))
    row.append((t("wms.button.discard"), f"wms:discard:{plan_id}"))
    return text, callback_buttons([row])


BATCH_SHOWN = 40
"""Plans listed in one batch message (two buttons each; Telegram allows 100)."""


def batch_message(name: str, summaries: list[dict[str, Any]]) -> tuple[str, Any]:
    """Many plans from one job (organize-tree: one per top-level folder), so
    each can be confirmed on its own (M7 §3.2)."""
    shown = summaries[:BATCH_SHOWN]
    lines = [t("wms.batch.intro", name=escape_html(name), count=len(summaries))]
    for item in shown:
        lines.append(t("wms.batch.line", id=item["id"], scope=escape_html(item["scope"]),
                       actions=item["actions"], size=item["size"]))
    if len(summaries) > len(shown):
        lines.append(t("wms.batch.more", count=len(summaries) - len(shown)))
    rows, row = [], []
    for item in shown:
        row += [(t("wms.button.apply_n", id=item["id"]), f"wms:apply:{item['id']}"),
                (t("wms.button.details_n", id=item["id"]), f"wms:detail:{item['id']}")]
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return "\n".join(lines), callback_buttons(rows)


def report_message(big: Any) -> tuple[str, Any]:
    """The big-files report, one [🗑 n] button per listed item (M7 §5)."""
    body = "\n".join(big.lines())
    if len(body) > MESSAGE_LIMIT:
        body = body[:MESSAGE_LIMIT].rsplit("\n", 1)[0] + "\n…"
    text = t("wms.big.intro") + f"\n<pre>{escape_html(body)}</pre>"
    buttons = [
        (t("wms.button.trash_n", n=number), f"wms:bt:{item.file_id}")
        for number, item in enumerate(big.items(), start=1)
        if len(f"wms:bt:{item.file_id}".encode()) <= 64
    ]
    rows = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
    return text, callback_buttons(rows) if rows else None


def covers(scope: str, folder: str) -> bool:
    """True when a rule with this scope sees files put in ``folder``."""
    scope, folder = "/" + scope.strip("/"), "/" + folder.strip("/")
    return scope == "/" or folder == scope or folder.startswith(scope + "/")


class WmsInBot:
    """Starts and stops the embedded scheduler with the bot, and shelves new
    arrivals: after files land in PikPak, the organize rules are run and the
    plan is sent with a confirm button (or applied, WMS_AUTO_SHELVE=apply)."""

    def __init__(self, config: Config, pikpak: PikPakService) -> None:
        self.config = config
        self.pikpak = pikpak
        self.embedded: EmbeddedWms | None = None
        self.shelve_delay = SHELVE_DELAY
        self._notify: Notify | None = None
        self._shelve_chats: set[int] = set()
        self._shelve_deadline = 0.0
        self._shelve_task: asyncio.Task | None = None
        self._shelve_folders: set[str] = set()
        self._warned_uncovered = False
        # /do: proposals waiting for a button, and who is refining a sentence.
        self._proposals: dict[int, dict[str, Any]] = {}
        self._editing: dict[int, int] = {}
        self._next_pid = 0
        self._announced: set[int] = set()

    def attach_notifier(self, notify: Notify) -> None:
        self._notify = notify

    # ------------------------------------------------------------- shelving

    async def pikpak_saved(self, job: Any) -> None:
        """JobQueue hook: a job put files into PikPak."""
        if self.embedded is None or self.config.wms.auto_shelve == "off":
            return
        # Only the drive WMS manages; a member's own drive is theirs.
        own = job.user_id if await self.pikpak.has_user_session(job.user_id) else None
        if own != await account_for(self.config, self.pikpak):
            return
        self._shelve_chats.add(job.chat_id)
        if job.kind != "share":  # a restored share lands where PikPak puts it
            self._shelve_folders.add(job.pikpak_folder or self.config.pikpak.folder)
        loop = asyncio.get_running_loop()
        self._shelve_deadline = loop.time() + self.shelve_delay
        if self._shelve_task is None or self._shelve_task.done():
            self._shelve_task = asyncio.create_task(self._shelve_when_quiet(),
                                                    name="wms-shelve")

    async def _shelve_when_quiet(self) -> None:
        loop = asyncio.get_running_loop()
        while self._shelve_chats:
            while (wait := self._shelve_deadline - loop.time()) > 0:
                await asyncio.sleep(wait)
            chats, self._shelve_chats = self._shelve_chats, set()
            folders, self._shelve_folders = self._shelve_folders, set()
            try:
                await self.shelve(chats, folders)
            except Exception:
                log.exception("shelving after a transfer failed")

    async def shelve(self, chats: set[int], folders: set[str] = frozenset()) -> Any:
        """Run the organize rules now and tell ``chats`` what they would do."""
        if self.embedded is None:
            return None
        apply = self.config.wms.auto_shelve == "apply"
        result = await self.embedded.run_job("organize", apply=apply)
        if result is not None and result.plan_id is None and folders:
            await self._warn_uncovered(chats, folders)
        if result is None or result.plan_id is None or self._notify is None:
            return result
        if result.report is not None:
            text = t("wms.shelved.applied", summary=escape_html(result.report.summary()))
            buttons = None
        else:
            lines = await self.embedded.plan_lines(result.plan_id, limit=12)
            text, buttons = plan_message(lines, result.plan_id, intro=t("wms.shelved.planned"))
        for chat in chats:
            await self._notify(chat, text, buttons)
        return result

    async def start(self) -> None:
        if not self.config.wms.enabled:
            return
        # WMS reads the environment for its language; the bot's may come
        # from config.yaml instead, and both must speak the same one.
        set_language(i18n.language())
        try:
            self.embedded = EmbeddedWms(provider_for(self.config, self.pikpak),
                                        on_result=self.job_finished)
            await self.embedded.start()
        except Exception:
            # A broken WMS config must not keep the downloader from starting.
            log.exception("WMS could not start; the bot carries on without it")
            self.embedded = None

    async def _warn_uncovered(self, chats: set[int], folders: set[str]) -> None:
        """Once per run: files landed where no organize rule looks, so shelving
        will never find them. Saying so beats silently doing nothing."""
        if self._warned_uncovered or self._notify is None or self.embedded is None:
            return
        try:
            scopes = [r["scope"] for r in self.embedded.rules()
                      if r["enabled"] and r["stage"] == "organize"]
        except WmsError:
            return
        uncovered = sorted(
            folder for folder in folders
            if not any(covers(scope, folder) for scope in scopes)
        )
        if not uncovered:
            return
        self._warned_uncovered = True
        log.warning("no organize rule covers %s, where PikPak transfers land", uncovered)
        text = t("wms.shelved.uncovered", folders=escape_html(", ".join(uncovered)))
        for chat in chats:
            await self._notify(chat, text, None)

    async def job_finished(self, result: Any) -> None:
        """A scheduled job left a plan waiting: tell the admins, once per plan.

        M7 jobs also report what they carried out on their own (dedupe), and
        the weekly big-files report goes out with its buttons.
        """
        if self._notify is None or self.embedded is None:
            return
        message = await self._job_message(result)
        if message is None:
            return
        text, buttons = message
        for admin in self.config.access.admin_user_ids:
            await self._notify(admin, text, buttons)

    async def _job_message(self, result: Any) -> tuple[str, Any] | None:
        name = escape_html(result.name)
        if getattr(result, "big", None) is not None:
            return report_message(result.big)
        many = list(getattr(result, "plan_ids", []) or [])
        if many and getattr(result, "reports", None):
            return t("wms.job.applied", name=name, summary=escape_html(result.summary)), None
        if len(many) > 1:
            fresh = [pid for pid in many if pid not in self._announced]
            if not fresh:
                return None
            self._announced.update(many)
            summaries = await self.embedded.plan_overview(many)
            return batch_message(result.name, [_scoped(item) for item in summaries])
        if result.plan_id is None or result.report is not None \
                or result.plan_id in self._announced:
            return None
        self._announced.add(result.plan_id)
        lines = await self.embedded.plan_lines(result.plan_id, limit=12)
        return plan_message(lines, result.plan_id, details=bool(many),
                            intro=t("wms.job.waiting", name=name))

    # ---------------------------------------- natural language (/do, M6)

    async def nl_message(self, user_id: int, text: str) -> tuple[str, Any]:
        """One sentence from an admin → (HTML reply, buttons or None)."""
        if self.embedded is None:
            return t("wms.off"), None
        editing = self._editing.pop(user_id, None)
        if editing is not None and editing in self._proposals:
            earlier = self._proposals.pop(editing)
            await self._drop_plan(earlier)
            text = f"{earlier['sentence']}{MERGE}{text}"
        try:
            result = await self.embedded.understand(text)
        except WmsError as exc:
            return t("wms.nl.failed", error=escape_html(exc.display())), None
        if result is None:
            return t("wms.nl.not_understood"), None
        pid = self._remember(user_id, text, None)
        if isinstance(result, Clarification):
            # The next message answers the question: merge it with this one.
            self._editing[user_id] = pid
            question = self.embedded.clarification_text(result)
            return t("wms.nl.ask", question=escape_html(question)), None
        try:
            proposal = await self.embedded.propose(result)
        except WmsError as exc:
            return t("wms.error", error=escape_html(exc.display())), None
        self._proposals[pid]["proposal"] = proposal
        # M7: the report comes with its own buttons, a batch lists its plans.
        if proposal.kind == "report" and proposal.big is not None:
            return report_message(proposal.big)
        if proposal.kind == "batch":
            summaries = await self.embedded.plan_overview(proposal.plan_ids)
            return batch_message("organize-tree", [_scoped(item) for item in summaries])
        body = "\n".join(self.embedded.proposal_lines(proposal))
        if len(body) > MESSAGE_LIMIT:
            body = body[:MESSAGE_LIMIT].rsplit("\n", 1)[0] + "\n…"
        intro = t("wms.nl.intro", translator=escape_html(proposal.translator or "rules"))
        text_out = f"{intro}\n<pre>{escape_html(body)}</pre>"
        actionable = (proposal.kind == "rule" or
                      (proposal.kind == "plan" and proposal.plan_id is not None))
        row = []
        if actionable:
            row.append((t("wms.button.apply"), f"wms:nl:apply:{pid}"))
        row.append((t("wms.button.edit"), f"wms:nl:edit:{pid}"))
        if actionable:
            row.append((t("wms.button.cancel"), f"wms:nl:cancel:{pid}"))
        return text_out, callback_buttons([row])

    async def nl_button(self, user_id: int, verb: str, pid: int) -> tuple[str | None, str | None]:
        """A press on [confirm] / [edit] / [cancel]: (new message text, alert)."""
        entry = self._proposals.get(pid)
        if entry is None or entry["user"] != user_id or self.embedded is None:
            return None, t("wms.nl.expired")
        proposal = entry["proposal"]
        if verb == "edit":
            self._editing[user_id] = pid
            return t("wms.nl.edit_prompt", sentence=escape_html(entry["sentence"])), None
        if verb == "cancel":
            self._proposals.pop(pid, None)
            await self._drop_plan(entry)
            return t("wms.nl.cancelled"), None
        if verb == "apply" and proposal is not None:
            try:
                if proposal.kind == "rule":
                    path = await self.embedded.add_rules(proposal.rules,
                                                         sentence=entry["sentence"])
                    result = t("wms.nl.rule_added", path=escape_html(path))
                else:
                    report = await self.embedded.apply(proposal.plan_id)
                    result = escape_html(report.summary())
            except WmsError as exc:
                return None, exc.display()[:190]
            self._proposals.pop(pid, None)
            return result, None
        return None, t("wms.nl.expired")

    def _remember(self, user_id: int, sentence: str, proposal: Any) -> int:
        self._next_pid += 1
        self._proposals[self._next_pid] = {"user": user_id, "sentence": sentence,
                                           "proposal": proposal}
        while len(self._proposals) > PROPOSALS_KEPT:
            self._proposals.pop(next(iter(self._proposals)))
        return self._next_pid

    async def _drop_plan(self, entry: dict[str, Any]) -> None:
        proposal = entry.get("proposal")
        if proposal is not None and proposal.plan_id is not None and self.embedded is not None:
            with contextlib.suppress(WmsError):
                await self.embedded.discard(proposal.plan_id)

    def editing(self, user_id: int) -> bool:
        return user_id in self._editing

    async def stop(self) -> None:
        if self._shelve_task is not None:
            self._shelve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._shelve_task
            self._shelve_task = None
        if self.embedded is not None:
            await self.embedded.stop()
            self.embedded = None


def _scoped(summary: dict[str, Any]) -> dict[str, Any]:
    """A batch plan's folder, from its source ("organize-tree:/A")."""
    source = str(summary.get("source") or "")
    return {**summary, "scope": source.partition(":")[2] or source}


# ------------------------------------------------------------ command line


def _factory(config: Config):
    """A fresh bot database and PikPak service per event loop the CLI opens."""

    def make():
        state: dict[str, PikPakService] = {}

        async def provider() -> PikPakApi:
            if "pikpak" not in state:
                db = Database(config.download.db_path)
                await db.connect()
                # Admins claimed in chat live in the database, not in .env.
                await bootstrap.load_runtime_settings(db, config)
                state["pikpak"] = PikPakService(config.pikpak, db)
            return await provider_for(config, state["pikpak"])()

        return provider

    return make


def main(argv: list[str] | None = None) -> int:
    """``wms`` inside the image: the WMS command line on the bot's account."""
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    config = load_config(None)
    return run_command(sys.argv[1:] if argv is None else argv, _factory(config))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
