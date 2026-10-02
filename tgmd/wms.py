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
import time
from collections.abc import Awaitable, Callable
from typing import Any

from dotenv import load_dotenv
from pikpakapi import PikPakApi

from pikpak_wms.ops.embed import (
    AccountUnavailable,
    Clarification,
    EmbeddedWms,
    Prioritize,
    Remark,
    Run,
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
from .utils import escape_html, human_duration, human_rate, human_size, truncate

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

PROGRESS_EVERY = 10.0
"""Seconds between edits of a running plan's message."""

PROGRESS_MIN_GAP = 5.0
"""Never edit more often than this (Telegram limits how fast a message may change)."""

MESSAGE_LIMIT = 3500
"""Telegram's limit is 4096; the plan is cut well before it."""

FILES_PER_PAGE = 6
"""Tasks shown (each with its own buttons) in one page of a running plan's message."""

PARALLEL_CHOICES = (1, 2, 4, 0)
"""The ``同时下载`` choices; 0 is no limit."""

REMARK_WINDOW = 30 * 60
"""A pending plan this recent is the one 「X 下过了」 refers to (docs/wms/M9.2 §C.2)."""

DOWNLOAD_ROWS = 12

Notify = Callable[[int, str, Any], Awaitable[None]]
"""Send ``text`` (HTML) with optional ``buttons`` to a chat."""

Edit = Callable[[str, Any], Awaitable[None]]
"""Replace a message's text (HTML) and buttons."""


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
        # /do: who is refining a sentence (the proposals themselves are in the
        # database, so their buttons survive a restart).
        self._editing: dict[int, int] = {}
        self._announced: set[int] = set()
        # Background runs: one watcher edits one progress message.
        self._watchers: set[asyncio.Task] = set()
        self._finishing: dict[int, int] = {}
        self.progress_every = PROGRESS_EVERY
        self.min_gap = PROGRESS_MIN_GAP
        self.tick = 1.0

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
            await self.announce_interrupted()
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
        if getattr(result, "alert", ""):
            return escape_html(result.alert), None
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
        embedded = self.embedded
        editing = self._editing.pop(user_id, None)
        if editing is not None:
            earlier = await embedded.load_proposal(editing)
            if earlier is not None:
                await embedded.drop_proposal(editing)
                await self._drop_plan(earlier)
                text = f"{earlier['sentence']}{MERGE}{text}"
        try:
            result = await embedded.understand(text)
        except WmsError as exc:
            return t("wms.nl.failed", error=escape_html(exc.display())), None
        if result is None:
            return t("wms.nl.not_understood"), None
        if isinstance(result, Remark):
            return await self._remark(user_id, result)
        if isinstance(result, Prioritize):
            return await self._prioritize(result)
        record = {"user": user_id, "sentence": text, "at": time.time()}
        pid = await embedded.save_proposal(record)
        if isinstance(result, Clarification):
            # The next message answers the question: merge it with this one.
            self._editing[user_id] = pid
            question = embedded.clarification_text(result)
            return t("wms.nl.ask", question=escape_html(question)), None
        try:
            proposal = await embedded.propose(result, user_id=user_id)
        except WmsError as exc:
            await embedded.drop_proposal(pid)
            return t("wms.error", error=escape_html(exc.display())), None
        await embedded.save_proposal({
            **record, "kind": proposal.kind, "plan_id": proposal.plan_id,
            "rules": embedded.rules_to_json(proposal.rules),
            "translator": proposal.translator,
        }, pid)
        # M7: the report comes with its own buttons, a batch lists its plans.
        if proposal.kind == "report" and proposal.big is not None:
            return report_message(proposal.big)
        if proposal.kind == "batch":
            summaries = await embedded.plan_overview(proposal.plan_ids)
            return batch_message("organize-tree", [_scoped(item) for item in summaries])
        body = "\n".join(embedded.proposal_lines(proposal))
        if len(body) > MESSAGE_LIMIT:
            body = body[:MESSAGE_LIMIT].rsplit("\n", 1)[0] + "\n…"
        intro = t("wms.nl.intro", translator=escape_html(self.translator_label(proposal)))
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

    def translator_label(self, proposal: Any) -> str:
        return proposal.translator

    async def _remark(self, user_id: int, remark: Remark) -> tuple[str, Any]:
        """「juvr00309 下过了」 / 「不要 X」 on its own (docs/wms/M9.2 §C.2): never a plan.

        With a plan of this user's from the last 30 minutes waiting, the files are taken
        out of it and it is shown again; otherwise a 「X 下过了」 is remembered in the
        download log and a bare 「不要 X」 has nothing to act on."""
        embedded = self.embedded
        assert embedded is not None
        names = escape_html("、".join(remark.names))
        if remark.downloaded:
            await embedded.mark_downloaded(remark.names, user_id=user_id)
        found = await self._pending_plan(user_id)
        if found is None:
            key = "wms.remark.marked" if remark.downloaded else "wms.remark.nothing_pending"
            return t(key, names=names), None
        pid, entry = found
        plan_id = entry["plan_id"]
        removed = await embedded.remove_from_plan(plan_id, remark.names)
        if not removed:
            return t("wms.remark.not_in_plan", names=names, id=plan_id), None
        head = t("wms.remark.removed", names=names, id=plan_id, count=len(removed))
        if not await embedded.plan_open(plan_id):
            await embedded.drop_proposal(pid)
            return f"{head}\n{t('wms.remark.emptied', id=plan_id)}", None
        lines = await embedded.plan_lines(plan_id, limit=8)
        body = "\n".join(lines)
        if len(body) > MESSAGE_LIMIT:
            body = body[:MESSAGE_LIMIT].rsplit("\n", 1)[0] + "\n…"
        row = [(t("wms.button.apply"), f"wms:nl:apply:{pid}"),
               (t("wms.button.edit"), f"wms:nl:edit:{pid}"),
               (t("wms.button.cancel"), f"wms:nl:cancel:{pid}")]
        return f"{head}\n<pre>{escape_html(body)}</pre>", callback_buttons([row])

    async def _prioritize(self, wish: Prioritize) -> tuple[str, Any]:
        """「先下 X」「优先下载 X」「X 置顶」 (docs/wms/M9.4 §B.3): moves queued and running
        downloads up the line; never a plan."""
        assert self.embedded is not None
        names = escape_html("、".join(wish.names))
        level = {"high": 1, "top": 2}[wish.level]
        found = await self.embedded.prioritize(wish.names, level)
        if not found:
            return t("wms.priority.none", names=names), None
        shown = escape_html("、".join(found[:3]) + ("…" if len(found) > 3 else ""))
        return t("wms.priority.done", level=t(f"wms.priority.level.{level}"),
                 count=len(found), names=shown), None

    async def _pending_plan(self, user_id: int) -> tuple[int, dict[str, Any]] | None:
        """This user's newest plan from a sentence, waiting and less than 30 minutes old."""
        assert self.embedded is not None
        for pid, entry in await self.embedded.recent_proposals(limit=20):
            if entry.get("user") != user_id or entry.get("kind") != "plan":
                continue
            age = time.time() - float(entry.get("at") or 0)
            if entry.get("plan_id") is None or age > REMARK_WINDOW:
                continue
            if await self.embedded.plan_open(entry["plan_id"]):
                return pid, entry
        return None

    # ---------------------------------------------------------- download log

    async def downloads_message(self, view: str = "today", name: str | None = None
                                ) -> tuple[str, Any]:
        """``/downloads``: today's rows, the last week's, the failed ones, or a search."""
        assert self.embedded is not None
        embedded = self.embedded
        if name:
            rows = await embedded.downloads(name=name, limit=DOWNLOAD_ROWS + 1)
            label = t("wms.downloads.view.search", name=escape_html(name))
        elif view == "week":
            rows = await embedded.downloads(days=7, limit=DOWNLOAD_ROWS + 1)
            label = t("wms.downloads.view.week")
        elif view == "failed":
            rows = await embedded.downloads(status=["failed"], limit=DOWNLOAD_ROWS + 1)
            label = t("wms.downloads.view.failed")
        else:
            rows = await embedded.downloads(today=True, limit=DOWNLOAD_ROWS + 1)
            label = t("wms.downloads.view.today")
        shown = rows[:DOWNLOAD_ROWS]
        lines = [t("wms.downloads.header", view=label, count=len(shown))]
        if not shown:
            lines.append(t("wms.downloads.none"))
        else:
            body = "\n".join(embedded.download_line(row) for row in shown)
            lines.append(f"<pre>{escape_html(body)}</pre>")
            if len(rows) > len(shown):
                lines.append(t("wms.downloads.more", count=len(rows) - len(shown)))
        queued = embedded.queued_tasks()
        if queued:
            lines.append(t("wms.downloads.queue", count=len(queued)))
            body = "\n".join(
                f"{n}. {self.priority_mark(track.priority)}{truncate(track.name, 40)} "
                f"({t('wms.downloads.queue_plan', id=plan_id)}, {track.number})"
                for n, (plan_id, track) in enumerate(queued[:DOWNLOAD_ROWS], 1))
            lines.append(f"<pre>{escape_html(body)}</pre>")
            if len(queued) > DOWNLOAD_ROWS:
                lines.append(t("wms.downloads.more", count=len(queued) - DOWNLOAD_ROWS))
        current = await embedded.parallel_files()
        lines.append(t("wms.downloads.parallel", n=self._parallel_label(current)))
        buttons = callback_buttons([
            [(t("wms.button.dl_today"), "wms:dl:today"), (t("wms.button.dl_week"), "wms:dl:week"),
             (t("wms.button.dl_failed"), "wms:dl:failed")],
            self.parallel_buttons(0, current),
        ])
        return "\n".join(lines), buttons

    async def nl_button(
        self, user_id: int, verb: str, pid: int
    ) -> tuple[str | None, str | None, Run | None]:
        """A press on [confirm] / [edit] / [cancel]: (new message text, alert, run).

        Confirming a plan does not wait for it: the run starts in the background
        and comes back as ``run`` for the caller to watch (docs/wms/M8.3 §G)."""
        if self.embedded is None:
            return None, t("wms.nl.expired"), None
        entry = await self.embedded.load_proposal(pid)
        if entry is None or entry.get("user") != user_id or "kind" not in entry:
            return None, t("wms.nl.expired"), None
        if verb == "edit":
            self._editing[user_id] = pid
            return t("wms.nl.edit_prompt", sentence=escape_html(entry["sentence"])), None, None
        if verb == "cancel":
            await self.embedded.drop_proposal(pid)
            await self._drop_plan(entry)
            return t("wms.nl.cancelled"), None, None
        if verb == "apply":
            try:
                if entry["kind"] == "rule":
                    rules = self.embedded.rules_from_json(entry.get("rules") or [])
                    path = await self.embedded.add_rules(rules, sentence=entry["sentence"])
                    await self.embedded.drop_proposal(pid)
                    return t("wms.nl.rule_added", path=escape_html(path)), None, None
                if entry.get("plan_id") is None:
                    return None, t("wms.nl.expired"), None
                run = await self.embedded.start_apply(entry["plan_id"], user_id=user_id)
            except WmsError as exc:
                return None, exc.display()[:190], None
            self._finishing[run.plan_id] = pid  # forgotten when the run completes
            return self.progress_text(run), None, run
        return None, t("wms.nl.expired"), None

    # ------------------------------------------------------- background runs

    def progress_text(self, run: Run) -> str:
        """⏳ Plan 58: 3/591 done · the current file, its percent, rate and time left.

        With several files: a summary line (done / failed / skipped / total, the speed
        of all of them, time left, how many at once) and one line per task (its number,
        name, state, percent, rate), a page of them at a time (docs/wms/M9.3 §A.4)."""
        control = run.control
        done = run.done
        if len(control.tracks) > 1:
            # The plan's own progress is the first file not done; with files running side by
            # side, what is done is every one that is no longer queued, running or paused.
            counts = control.counts()
            finished = sum(counts[key] for key in ("done", "failed", "skipped", "cancelled"))
            done = max(done, run.total - len(control.tracks) + finished)
        lines = [t("wms.run.progress", id=run.plan_id, done=done, total=run.total)]
        if len(control.tracks) <= 1:
            if run.file:
                fraction = run.fraction
                lines.append(t(
                    "wms.run.file", name=escape_html(truncate(run.file, 48)),
                    percent=f"{fraction * 100:.0f}" if fraction is not None else "?",
                    done=human_size(run.received), size=human_size(run.size),
                    rate=human_rate(run.speed), eta=human_duration(run.eta),
                ))
                if run.conns:
                    lines.append(t("wms.run.fetch", avg=f"{run.average / (1024 * 1024):.1f}",
                                   conns=run.conns, links=run.links or "web"))
            for track in control.tracks.values():
                if track.state in ("paused", "cancelled"):
                    lines.append(self.task_line(track))
            return "\n".join(lines)
        counts = control.counts()
        lines.append(t(
            "wms.run.summary", done=counts["done"], failed=counts["failed"],
            skipped=counts["skipped"], total=len(control.tracks),
            rate=human_rate(control.speed), eta=human_duration(control.eta),
            parallel=self._parallel_label(control.limit))
            + (t("wms.run.summary_cancelled", cancelled=counts["cancelled"])
               if counts["cancelled"] else "")
            + (t("wms.run.summary_paused", paused=counts["paused"]) if counts["paused"] else ""))
        listed = control.rows()
        pages = max(-(-len(listed) // FILES_PER_PAGE), 1)
        page = min(max(control.page, 0), pages - 1)
        lines.extend(self.task_line(track)
                     for track in listed[page * FILES_PER_PAGE:(page + 1) * FILES_PER_PAGE])
        if pages > 1:
            lines.append(t("wms.run.page", page=page + 1, pages=pages))
        return "\n".join(lines)

    def task_line(self, track: Any) -> str:
        """One task: ``3. name · 下载中 45% · 3.2 MB/s`` (docs/wms/M9.3 §A.4)."""
        name = escape_html(truncate(track.name, 40))
        fraction = track.fraction
        percent = f"{fraction * 100:.0f}%" if fraction is not None else "?%"
        label = t(f"wms.task.state.{track.state}")
        if track.state == "active":
            detail = f"{label} {percent} · {human_rate(track.speed)}"
        elif track.state in ("paused", "cancelled") and fraction:
            detail = f"{label} {percent}"
        else:
            detail = label
        mark = self.priority_mark(track.priority)
        return t("wms.task.line", n=track.number, name=mark + name, detail=detail)

    @staticmethod
    def priority_mark(level: int) -> str:
        """⬆ for high, ⬆⬆ for top, on the task's line."""
        return "⬆" * level + (" " if level else "")

    @staticmethod
    def _parallel_label(limit: int) -> str:
        return str(limit) if limit else t("wms.parallel.all")

    def parallel_buttons(self, plan_id: int, current: int) -> list[tuple[str, str]]:
        """``同时下载`` choices for a plan (``plan_id`` 0: the default for every plan)."""
        return [(t("wms.button.parallel_on" if n == current else "wms.button.parallel",
                   n=self._parallel_label(n)), f"wms:par:{plan_id}:{n}")
                for n in PARALLEL_CHOICES]

    def task_buttons(self, plan_id: int, track: Any) -> list[tuple[str, str]]:
        """A task's buttons, on its own row: 开始 or 暂停, then 终止; a stopped task that
        left a partial file offers 删除已下载部分."""
        who = f"{plan_id}:{track.index}"
        short = truncate(track.name, 12)
        first = (t("wms.button.task_pause", n=track.number, name=short), f"wms:tp:{who}")
        if track.state in ("queued", "paused"):
            first = (t("wms.button.task_start", n=track.number, name=short), f"wms:ts:{who}")
        if track.state in ("active", "queued", "paused"):
            return [first, (t("wms.button.task_stop", n=track.number), f"wms:tx:{who}"),
                    (t("wms.button.task_priority", n=track.number), f"wms:tr:{who}")]
        if track.state == "cancelled" and track.partial_files():
            return [(t("wms.button.task_delete", n=track.number, name=short),
                     f"wms:td:{who}")]
        return []

    def run_buttons(self, run: Run) -> Any:
        """A running plan's buttons: a row of 开始/暂停 and 终止 for each task shown, the page
        buttons, 全部暂停 / 全部开始 / 重试失败的, the choice of how many at once, and 停止."""
        rows: list[list[tuple[str, str]]] = []
        control = run.control
        listed = control.rows()
        pages = max(-(-len(listed) // FILES_PER_PAGE), 1)
        page = min(max(control.page, 0), pages - 1)
        for track in listed[page * FILES_PER_PAGE:(page + 1) * FILES_PER_PAGE]:
            row = self.task_buttons(run.plan_id, track)
            if row:
                rows.append(row)
        if pages > 1:
            nav = []
            if page > 0:
                nav.append((t("wms.button.prev"), f"wms:page:{run.plan_id}:{page - 1}"))
            if page < pages - 1:
                nav.append((t("wms.button.next"), f"wms:page:{run.plan_id}:{page + 1}"))
            rows.append(nav)
        if len(control.tracks) > 1:
            plan_row = [(t("wms.button.pause_all"), f"wms:pall:{run.plan_id}"),
                        (t("wms.button.start_all"), f"wms:sall:{run.plan_id}"),
                        (t("wms.button.plan_priority"), f"wms:pr:{run.plan_id}")]
            counts = control.counts()
            if counts["failed"] or counts["cancelled"]:
                plan_row.append((t("wms.button.retry_failed"), f"wms:retry:{run.plan_id}"))
            rows.append(plan_row)
        if run.total > 1:
            rows.append(self.parallel_buttons(run.plan_id, control.limit))
        rows.append([(t("wms.button.stop"), f"wms:stop:{run.plan_id}")])
        return callback_buttons(rows)

    def stop_buttons(self, plan_id: int) -> Any:
        return callback_buttons([[(t("wms.button.stop"), f"wms:stop:{plan_id}")]])

    def _resume_buttons(self, plan_id: int) -> Any:
        return callback_buttons([[(t("wms.button.resume"), f"wms:apply:{plan_id}"),
                                  (t("wms.button.discard"), f"wms:discard:{plan_id}")]])

    def finished_message(self, run: Run) -> tuple[str, Any]:
        """What a run ended as: a summary, a stop, or an error (the plan stays usable)."""
        if run.stopped:
            return (t("wms.run.stopped", id=run.plan_id, done=run.done, total=run.total),
                    self._resume_buttons(run.plan_id))
        if run.error or run.report is None:
            return (t("wms.run.failed", id=run.plan_id, error=escape_html(run.error or "?"),
                      done=run.done, total=run.total), self._resume_buttons(run.plan_id))
        report = run.report
        lines = [escape_html(report.summary())]
        lines += [t("wms.run.failure_line", path=escape_html(str(item.get("path") or "?")),
                    error=escape_html(str(item.get("error") or "")))
                  for item in report.failed[:3]]
        lines += [t("wms.run.fetch_line", name=escape_html(truncate(
            str(item.get("path") or "?").rsplit("/", 1)[-1], 40)),
            avg=f"{float(item.get('avg_mib_s') or 0):.1f}",
            conns=item.get("peak_connections", 0), links=item.get("links", "web"))
            for item in getattr(report, "fetches", [])[:3]]
        if report.stopped:
            lines.append(escape_html(report.stopped))
        if report.remaining:
            lines.append(t("wms.run.remaining", remaining=report.remaining))
            return "\n".join(lines), self._resume_buttons(run.plan_id)
        if report.failed or getattr(report, "cancelled", None):
            rows = [[(t("wms.button.retry_failed"), f"wms:retry:{run.plan_id}")]]
            rows += [row for track in run.control.rows()
                     if (row := self.task_buttons(run.plan_id, track))][:FILES_PER_PAGE]
            return "\n".join(lines), callback_buttons(rows)
        return "\n".join(lines), None

    def watch(self, run: Run, edit: Edit) -> asyncio.Task:
        """Keep one message showing ``run``: edited every :attr:`progress_every`
        seconds (and sooner, never closer than :attr:`min_gap`, when another
        action finished), then replaced by the result. Edits that Telegram
        refuses are skipped, never fatal."""
        task = asyncio.create_task(self._watch(run, edit), name=f"wms-watch-{run.plan_id}")
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)
        return task

    async def _watch(self, run: Run, edit: Edit) -> None:
        loop = asyncio.get_running_loop()
        last, shown_done, shown_files = loop.time(), run.done, 0
        while True:
            try:
                await asyncio.wait_for(run.finished.wait(), timeout=self.tick)
                break
            except TimeoutError:
                pass
            waited = loop.time() - last
            files = len(run.control.tracks)
            if waited >= self.progress_every or (
                (run.done != shown_done or files != shown_files) and waited >= self.min_gap
            ):
                last, shown_done, shown_files = loop.time(), run.done, files
                await self._safe_edit(edit, self.progress_text(run), self.run_buttons(run))
        text, buttons = self.finished_message(run)
        await self._safe_edit(edit, text, buttons)
        pid = self._finishing.pop(run.plan_id, None)
        if pid is not None and run.report is not None and not run.report.remaining \
                and not run.error and not run.stopped and self.embedded is not None:
            with contextlib.suppress(Exception):
                await self.embedded.drop_proposal(pid)

    async def _safe_edit(self, edit: Edit, text: str, buttons: Any) -> None:
        try:
            await edit(text, buttons)
        except Exception as exc:  # noqa: BLE001 - "not modified", flood waits, a deleted message
            log.debug("could not edit the progress message: %s", exc)

    def background(self, work: Any) -> asyncio.Task:
        """Run a coroutine beside the handler that started it (an undo)."""
        task = asyncio.ensure_future(work)
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)
        return task

    async def settle(self) -> None:
        """Wait for every progress watcher to finish (tests, and shutdown)."""
        if self._watchers:
            await asyncio.gather(*list(self._watchers), return_exceptions=True)

    async def announce_interrupted(self) -> None:
        """After a restart: tell the admins which plans were cut off (docs/wms/M8.3 §G5)."""
        if self.embedded is None or self._notify is None:
            return
        for info in self.embedded.interrupted:
            text = t("wms.run.interrupted", id=info["id"], done=info["done"], total=info["total"])
            for admin in self.config.access.admin_user_ids:
                try:
                    await self._notify(admin, text, self._resume_buttons(info["id"]))
                except Exception:
                    log.exception("could not tell %s that plan %s was interrupted",
                                  admin, info["id"])
        self.embedded.interrupted = []

    async def _drop_plan(self, entry: dict[str, Any]) -> None:
        plan_id = entry.get("plan_id")
        if plan_id is not None and self.embedded is not None:
            with contextlib.suppress(WmsError):
                await self.embedded.discard(plan_id)

    def editing(self, user_id: int) -> bool:
        return user_id in self._editing

    async def stop(self) -> None:
        for task in list(self._watchers):
            task.cancel()
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
