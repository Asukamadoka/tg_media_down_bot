"""A small message catalogue, so the bot can speak more than one language.

This is deliberately not gettext. There is no build step, no ``.po`` files and
no extraction tooling: the catalogue is a dict in this module, and a missing
key falls back to English rather than raising. That keeps the cost of adding a
language proportional to the translation itself.

Three rules the rest of the codebase relies on:

* **Command names and their arguments are never translated.** ``/mode local``
  is parsed by matching the literal word ``local``, so a translated help text
  that told people to send ``/mode 本地`` would document a command that does
  not exist. Translations keep the English keyword and explain it alongside.
* **Stored values are never translated.** Job states and delivery modes are
  written to SQLite; translating them would make old rows and new rows
  disagree. :func:`display_mode` and :func:`display_state` translate them at
  the moment of display instead.
* **Log lines are not translated.** Only text a user reads goes through
  :func:`t`. An operator grepping the deployment log should not have to know
  which language the bot was started in.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "en"
LANGUAGES: tuple[str, ...] = ("en", "zh")

_language = DEFAULT_LANGUAGE


def normalize(value: str | None) -> str:
    """Map anything a person might write to one of :data:`LANGUAGES`.

    Unknown values fall back to the default rather than failing: a typo in an
    environment variable should not stop the bot from starting.
    """
    raw = (value or "").strip().lower().replace("_", "-")
    if not raw:
        return DEFAULT_LANGUAGE
    if raw.startswith("zh") or raw in ("chinese", "cn", "中文"):
        return "zh"
    if raw.startswith("en") or raw in ("english", "c", "posix"):
        return "en"
    return DEFAULT_LANGUAGE


def set_language(value: str | None) -> str:
    """Set the language for every later :func:`t` call. Returns what was set."""
    global _language
    _language = normalize(value)
    return _language


def language() -> str:
    """The language currently in force."""
    return _language


def language_from_environment() -> str | None:
    """Read the language out of the environment, if it says anything.

    ``TGMD_LANG`` is checked before ``BOT_LANG``. POSIX ``LANG`` is
    deliberately *not* consulted: container images routinely set it to
    ``C.UTF-8`` for unrelated reasons, and inheriting that as a UI decision
    would be surprising.
    """
    for name in ("TGMD_LANG", "BOT_LANG"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def t(key: str, /, lang: str | None = None, **kwargs: object) -> str:
    """Look up ``key``, falling back to English and then to the key itself.

    ``kwargs`` are applied with :meth:`str.format` only when there are any, so
    a catalogue entry containing a stray brace is returned untouched rather
    than raising at the worst possible moment.
    """
    chosen = normalize(lang) if lang is not None else _language
    table = CATALOG.get(chosen) or {}
    text = table.get(key)
    if text is None and chosen != DEFAULT_LANGUAGE:
        text = CATALOG[DEFAULT_LANGUAGE].get(key)
    if text is None:
        log.warning("no catalogue entry for %r", key)
        return key
    if not kwargs:
        return text
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        log.warning("could not format catalogue entry %r", key)
        return text


class Explained(Exception):
    """An error a person will read.

    ``str(exc)`` stays English, for the log (log lines are never translated).
    Raised with a catalogue ``key`` and its arguments, :meth:`display` gives
    the reader's language. An argument that is itself an :class:`Explained`
    error is displayed too, so a wrapped PikPak error reads naturally inside
    a delivery error. Without an explicit message the English catalogue
    entry is the message, so the text lives in one place.
    """

    def __init__(self, message: str = "", *, key: str | None = None, **kwargs: object) -> None:
        if not message and key is not None:
            message = t(key, lang=DEFAULT_LANGUAGE, **kwargs)
        super().__init__(message)
        self.key = key
        self.kwargs = kwargs

    def display(self, lang: str | None = None) -> str:
        if self.key is None:
            return str(self)
        args = {
            name: value.display(lang) if isinstance(value, Explained) else value
            for name, value in self.kwargs.items()
        }
        return t(self.key, lang=lang, **args)


def describe(exc: BaseException, lang: str | None = None) -> str:
    """What to show a person for ``exc``: translated when it can be."""
    return exc.display(lang) if isinstance(exc, Explained) else str(exc)


def display_mode(mode: str, /, lang: str | None = None) -> str:
    """A delivery mode as a person should read it. The stored value is unchanged."""
    key = f"mode.name.{mode}"
    return t(key, lang=lang) if key in CATALOG[DEFAULT_LANGUAGE] else mode


def display_state(state: str, /, lang: str | None = None) -> str:
    """A job state as a person should read it. The stored value is unchanged."""
    key = f"state.{state}"
    return t(key, lang=lang) if key in CATALOG[DEFAULT_LANGUAGE] else state


CATALOG: dict[str, dict[str, str]] = {
    "en": {
        'help.body': (
                         '<b>Telegram media downloader</b>\n'
                         '\n'
                         'Send me a Telegram message link and I will fetch the media behind'
                         ' it — even\n'
                         'from channels that block saving, as long as the reading account i'
                         's a member.\n'
                         '\n'
                         '<b>Links I understand</b>\n'
                         '• <code>https://t.me/channel/123</code> — public channel or '
                         'group\n'
                         '• <code>https://t.me/c/1234567890/123</code> — private chat\n'
                         '• <code>https://t.me/channel/12/123</code> — a forum topic\n'
                         '• <code>https://t.me/channel/100-120</code> — a range of '
                         'messages\n'
                         '• <code>?single</code> to take one album item, <code>?comment=45<'
                         '/code> for a comment\n'
                         '• a magnet link or direct URL — handed straight to PikPak\n'
                         '• a PikPak share link — saved into your drive\n'
                         '• media sent or forwarded to me directly\n'
                         '\n'
                         '<b>Commands</b>\n'
                         '/mode — where files should go: telegram, local, pikpak or auto\n'
                         '/status — what I am working on\n'
                         '/cancel [id] — stop one job, or everything\n'
                         '/stats — your recent jobs\n'
                         '/pikpak — PikPak account, quota and target folder\n'
                         '/pikpak login — connect your own PikPak account\n'
                         '/wms — PikPak warehouse: plans, audit, undo (admins; /wms help)\n'
                         '/do — say what to do with the drive, e.g. 下载今天转存的视频 (admins)\n'
                         '/id — your Telegram user id\n'
                         '/help — this message'
                     ),
        'help.admin': (
                          '\n'
                          '/setup — finish setup here: sign in a reading account or '
                          'PikPak\n'
                          '/cache — use a channel as the upload cache\n'
                          "/verify — check the bot's identity and configuration"
                      ),
        'help.unclaimed': (
                              '<b>This bot has no admin yet</b>\n'
                              '\n'
                              'Whoever deployed me left a claim code in my startup log. Sen'
                              'd it here:\n'
                              '\n'
                              '<code>/claim &lt;code&gt;</code>\n'
                              '\n'
                              'That makes you the admin, with no redeploy. Until then I ref'
                              'use every\n'
                              'request, including yours.'
                          ),
        'help.no_reading_account': (
                                       '\n'
                                       '\n'
                                       '⚠️ No reading account is connected yet, so private '
                                       'and save-restricted chats will not work.'
                                   ),
        'help.no_reading_account_admin': (
                                             ' Send <code>/setup</code> to finish that '
                                             'here.'
                                         ),
        'access.denied': (
                             'You are not allowed to use this bot.\n'
                             '\n'
                             'Your user id is <code>{user_id}</code>. An admin can add it t'
                             'o <code>ALLOWED_USER_IDS</code>.'
                         ),
        'error.generic': '❌ {error}',
        'id.reply': (
                        'Your user id: <code>{user_id}</code>\n'
                        'This chat id: <code>{chat_id}</code>'
                    ),
        'mode.current': (
                            'Current mode: <b>{current}</b>\n'
                            '\n'
                            '<code>/mode telegram</code> — send the file back to you\n'
                            "<code>/mode local</code> — keep it on the server's disk\n"
                            '<code>/mode pikpak</code> — transfer it into PikPak\n'
                            '<code>/mode auto</code> — send it back if it can be forwarded, '
                            'otherwise keep it on the NAS'
                        ),
        'mode.unknown': 'Unknown mode {choice}. Pick one of: {modes}',
        'mode.pikpak_none': (
            'No PikPak account is connected yet. Send '
            '<code>/pikpak login</code> to connect yours.'
        ),
        'mode.pikpak_unavailable': (
            'PikPak is not available on this server. Ask the operator to set '
            'PIKPAK_USERNAME and PIKPAK_PASSWORD, or to allow per-user logins.'
        ),
        'mode.set': 'Mode set to <b>{choice}</b>.',
        'mode.name.telegram': 'telegram',
        'mode.name.local': 'local',
        'mode.name.pikpak': 'pikpak',
        'mode.name.auto': 'auto',
        'status.empty': 'Nothing in your queue.',
        'status.header': '<b>{count} item(s) in your queue</b>',
        'state.queued': 'queued',
        'state.running': 'running',
        'state.done': 'done',
        'state.failed': 'failed',
        'state.cancelled': 'cancelled',
        'state.partial': 'partial',
        'cancel.no_match': 'No matching job found.',
        'cancel.nothing': 'Nothing to cancel.',
        'cancel.cancelling': 'Cancelling {ids}.',
        'stats.empty': 'You have not downloaded anything yet.',
        'stats.none': 'none',
        'stats.totals': (
                            '<b>Totals</b>\n'
                            '{totals}\n'
                            'Transferred: {transferred}'
                        ),
        'stats.recent_header': (
                                   '\n'
                                   '<b>Recent</b>'
                               ),
        'pikpak.login.disabled': 'The operator has disabled per-user PikPak logins.',
        'pikpak.login.replacing': (
                                      '\n'
                                      '\n'
                                      'This replaces the account you have connected now.'
                                  ),
        'pikpak.login.miniapp': (
                                    '<b>Connect your PikPak account</b>\n'
                                    '\n'
                                    'Tap below to open the form inside Telegram. There is n'
                                    'o link to leak: Telegram tells me who you are.\n'
                                    '\n'
                                    'Your password goes to PikPak once, in exchange for an '
                                    'access token. Only the token is stored.{replacing}'
                                ),
        'pikpak.login.button_miniapp': '🔐 Connect PikPak',
        'pikpak.login.private_only': (
            'Message me directly to connect PikPak. Telegram only opens the '
            'login form in a private chat, and a password does not belong in a group.'
        ),
        'pikpak.login.chat_fallback': (
                                          '<b>Connect your PikPak account</b>\n'
                                          '\n'
                                          'Send <code>/setup pikpak</code> and I will ask f'
                                          'or your email and password here, deleting each m'
                                          'essage as I read it.\n'
                                          '\n'
                                          '<i>The in-Telegram form is not available: '
                                          '{reason}</i>{replacing}'
                                      ),
        'pikpak.logout.only_admin_shared': 'Only an admin can clear the shared session.',
        'pikpak.logout.shared_cleared': 'Shared PikPak session cleared.',
        'pikpak.logout.none': 'You have no PikPak account connected.',
        'pikpak.logout.done': (
                                  'Your PikPak account is disconnected and the stored token'
                                  ' is gone.'
                              ),
        'pikpak.dir.current': (
                                  'Your PikPak folder: <code>{folder}</code>\n'
                                  'Change it with <code>/pikpak dir /Movies/Anime</code>.'
                              ),
        'pikpak.dir.set': 'PikPak folder set to <code>{folder}</code>.',
        'pikpak.status.none': (
            'No PikPak account is connected. Send '
            '<code>/pikpak login</code> to connect yours.'
        ),
        'pikpak.status.account_own': 'your own account',
        'pikpak.status.account_shared': 'the shared account ({username})',
        'pikpak.status.transfers_full': (
                                            'magnet links, URLs, share links and Telegram '
                                            'media'
                                        ),
        'pikpak.status.transfers_limited': 'magnet links, URLs and share links',
        'pikpak.status.footer_own': (
                                        '\n'
                                        '\n'
                                        '<code>/pikpak logout</code> disconnects your '
                                        'account.'
                                    ),
        'pikpak.status.footer_shared': (
                                           '\n'
                                           '\n'
                                           '<code>/pikpak login</code> connects your own ac'
                                           'count instead.'
                                       ),
        'pikpak.status.body': (
                                  '<b>PikPak</b>\n'
                                  'Account: {account}\n'
                                  'Storage: {used} of {limit} used ({percent}%)\n'
                                  'Folder: <code>{folder}</code>\n'
                                  'Supported transfers: {transfers}{footer}'
                              ),
        'claim.already': 'This bot already has an admin.',
        'claim.success': (
                             '✅ <b>You are now the admin.</b>\n'
                             '\n'
                             'Nothing else needs deploying. Send <code>/setup</code> to sig'
                             'n in a reading account and connect PikPak, both from here.'
                         ),
        'cache.only_admin': 'Only an admin can change the upload cache.',
        'cache.channel_not_admin': (
            'None of this bot\'s admins runs this channel, so it cannot become the upload cache.'
            ' Make the admin\'s own account an admin of the channel, then send /cache again.'
        ),
        'cache.disabled': 'Upload cache disabled. Every request downloads again.',
        'cache.bad_id': (
                            'That does not look like a chat id. Ids look like '
                            '<code>-1001234567890</code>.'
                        ),
        'cache.state_current': 'Currently using <code>{chat_id}</code>.',
        'cache.state_none': 'No upload cache is set, so every request downloads again.',
        'cache.help': (
                          '<b>Upload cache</b>\n'
                          '\n'
                          '{state}\n'
                          '\n'
                          'To set one: create a private channel, add me as an <b>administra'
                          'tor</b>, then post <code>/cache</code> <b>in that channel</b>. I'
                          ' will pick up its id myself.\n'
                          '\n'
                          '<code>/cache off</code> disables it.'
                      ),
        'cache.cannot_see': (
                                'I cannot see that chat. Add me to it as an administrator '
                                'first.\n'
                                '\n'
                                '<i>{error}</i>'
                            ),
        'cache.not_admin': (
                               'I am in that chat but not an administrator, so I could not '
                               'store uploads there. Promote me and try again.'
                           ),
        'cache.set': (
                         '✅ Upload cache set to <code>{chat_id}</code>.\n'
                         '\n'
                         'A link requested twice is now re-sent from Telegram instead of be'
                         'ing downloaded again. This survives restarts, no redeploy.'
                     ),
        'setup.unavailable': 'Setup is not available in this build.',
        'setup.cancelled': 'Setup cancelled.',
        'setup.nothing_to_cancel': 'Nothing to cancel.',
        'setup.only_admin': 'Only an admin can sign in a reading account.',
        'setup.footer': (
                            '\n'
                            '\n'
                            '<code>/setup telegram</code> · <code>/setup pikpak</code> · <c'
                            'ode>/setup cancel</code>'
                        ),
        'verify.only_admin': 'Only an admin can run /verify.',
        'verify.checking': 'Checking…',
        'verify.failed': '❌ Verification failed: {error}',
        'dispatch.prompt': (
                               'Send me a Telegram message link, a magnet link, or media to'
                               ' download. /help lists everything I understand.'
                           ),
        'dispatch.errors_header': 'I could not use those links:',
        'inbound.note_local': (
                                  ' (mode <b>telegram</b> makes no sense here, saving '
                                  'locally)'
                              ),
        'inbound.queued': 'Queued <code>#{job_id}</code>{note}.',
        'bundle.queued': 'Queued {count} job(s): <code>{ids}</code> → <b>{mode}</b>',
        'bundle.needs_user_session': '{ref} needs a user session; none is configured',
        'menu.help': 'What I take and what I can do',
        'menu.claim': 'Become the admin of a freshly deployed bot',
        'menu.setup': 'Finish setup: sign in an account or PikPak',
        'menu.cache': 'Use a channel as the upload cache',
        'menu.mode': 'Where files go: telegram, local or pikpak',
        'menu.status': 'What I am working on',
        'menu.cancel': 'Stop one job, or all of them',
        'menu.stats': 'Your recent jobs',
        'menu.pikpak': 'PikPak account, quota and folder',
        'menu.wms': 'PikPak warehouse: plans, audit, undo',
        'menu.do': 'Say what to do with the drive, in a sentence',
        'menu.verify': 'Check my identity and configuration',
        'menu.id': 'Your Telegram user id',
        'profile.about': (
                             'Send me a Telegram message link and I fetch the media behind '
                             'it, or transfer it to PikPak.'
                         ),
        'profile.description': (
                                   'Send me any Telegram message link and I fetch the media'
                                   ' behind it, even from channels that block saving. I can'
                                   ' send the file back to you, keep it on the server, or t'
                                   'ransfer it into PikPak. Magnet links, direct URLs and P'
                                   'ikPak share links go straight to PikPak.\n'
                                   '\n'
                                   'Send /setup to finish signing in, or /help to see every'
                                   'thing I take.'
                               ),
        'manual.setprivacy.label': '/setprivacy → Disable',
        'manual.setprivacy.why': (
                                     'lets me see links posted in groups I am in. Skip it i'
                                     'f you will only message me directly. There is no API '
                                     'for this setting, so it has to be done in @BotFather.'
                                 ),
        'job.queue_full': (
                              'you already have {limit} items queued; wait for them or use '
                              '/cancel'
                          ),
        'job.pikpak.sending': '⏳ Sending to PikPak: <code>{label}</code>',
        'job.pikpak.no_account': (
                                     '❌ No PikPak account is connected, and a magnet link '
                                     'or URL has nowhere else to go. Use /pikpak login to c'
                                     'onnect yours.'
                                 ),
        'job.share.saving': '⏳ Saving the PikPak share…',
        'job.share.more': (
                              '\n'
                              '… and {count} more'
                          ),
        'job.share.saved': (
                               '✅ Saved {count} item(s) to PikPak:\n'
                               '{listing}'
                           ),
        'job.message.no_media': '❌ That message has no media to download.',
        'job.cancelled': '🚫 Cancelled.',
        'job.cancelled_after': '🚫 Cancelled after {count} file(s).',
        'job.inbound.downloading': '⬇️ <code>{name}</code> ({size})',
        'job.lookup': '🔍 Looking up <code>{ref}</code>…',
        'job.cached': '♻️ {prefix}<code>{name}</code> served from cache',
        'job.forwarded': (
            '⚡ {prefix}<code>{name}</code> — copied by Telegram, nothing downloaded'
        ),
        'job.hint_cache': (
            '\n💡 An admin can send /cache in a private channel to make files like '
            'this instant.'
        ),
        'job.downloading_progress': (
                                        '⬇️ {prefix}<code>{label}</code>\n'
                                        '{bar} {percent}% ({received} / {total})\n'
                                        '{rate} · ETA {eta}'
                                    ),
        'job.downloading': '⬇️ {prefix}<code>{label}</code> ({size})',
        'job.delivered': '✅ {prefix}<code>{label}</code> — {summary}',
        'job.uploading': (
                             '⬆️ {prefix}<code>{name}</code>\n'
                             '{bar} {percent}% ({sent} / {total})\n'
                             '{rate}'
                         ),
        'job.handing_to_pikpak': '☁️ {prefix}handing <code>{name}</code> to PikPak…',
        'job.note_skipped': '{count} message(s) had no media',
        'job.note_truncated': (
                                  'only the first {cap} message(s) were processed '
                                  '(download.max_batch)'
                              ),
        'job.note_failed': (
                               '{count} failed:\n'
                               '{shown}'
                           ),
        'job.note_more': (
                             '\n'
                             '… and {count} more'
                         ),
        'job.summary': '{icon} {succeeded}/{total} delivered',
        'job.nothing_delivered': '❌ Nothing was delivered.',
        # ---- PikPak warehouse (WMS)
        'wms.off': 'The PikPak warehouse is off. Set WMS_ENABLED=true and restart the bot.',
        'wms.admins_only': 'Only admins can manage the PikPak warehouse.',
        'wms.usage': (
            '<b>/wms</b> — warehouse status and panel\n'
            '/wms stocktake [full] — refresh the index\n'
            '/wms plan — plan what the organize rules would do\n'
            '/wms plan &lt;id&gt; — show a plan\n'
            '/wms apply &lt;id&gt; — carry a plan out\n'
            '/wms undo &lt;audit id&gt; — reverse one change\n'
            '/wms rules — the rules in force'
        ),
        'wms.working': 'Working on it…',
        'wms.failed_see_log': 'That failed; the deployment log has the details.',
        'wms.error': 'Warehouse: {error}',
        'wms.bad_id': '“{value}” is not an id.',
        'wms.button.apply': 'Apply',
        'wms.button.discard': 'Discard',
        'wms.button.undo': 'Undo it',
        'wms.button.edit': 'Edit',
        'wms.button.cancel': 'Cancel',
        'wms.nl.usage': (
            'Say what to do with the PikPak drive, for example:\n'
            '<code>/do 下载今天转存到网盘的所有大于1GB的视频</code>\n'
            'In a private chat, admins can also just send the sentence.'
        ),
        'wms.nl.intro': 'Here is the plan (understood by: {translator}). Nothing has changed yet.',
        'wms.nl.ask': '🤔 {question}\nReply with the missing part; it is added to your sentence.',
        'wms.nl.not_understood': (
            'I did not understand that as a drive command. Links are still downloaded as '
            'before; for the drive, try e.g. <code>下载今天转存的大于1GB的视频</code>.'
        ),
        'wms.nl.failed': 'The translator failed: {error}',
        'wms.nl.edit_prompt': (
            'Send what to change or add; it is joined to “{sentence}” and read again.'
        ),
        'wms.nl.cancelled': 'Cancelled. Nothing was changed.',
        'wms.nl.expired': 'This proposal has expired; send the sentence again.',
        'wms.nl.rule_added': 'Added to {path}. It runs on its schedule and asks before changing.',
        'wms.job.waiting': '⏰ The scheduled job “{name}” made a plan; it waits for you:',
        'wms.discarded': 'Plan {id} discarded.',
        'wms.undo.preview': 'This would be undone:\n<code>{what}</code>',
        'wms.undo.done': 'Undone: <code>{what}</code>',
        'wms.shelved.planned': '📦 New files in PikPak. The rules would do this:',
        'wms.shelved.applied': '📦 New files in PikPak were shelved. {summary}',
        'wms.shelved.uncovered': (
            '📦 New files went to {folders} in PikPak, but no organize rule looks '
            'there, so nothing will shelve them. Point PIKPAK_FOLDER at a rule\'s '
            'scope (for example /Inbox), or widen a rule\'s scope.'
        ),
        'wms.rules.header': '<b>Rules</b> ({path})',
        'wms.rules.line': '{state} <b>{name}</b> [{stage}] {scope}: {actions}',
        'wms.rules.on': '✅',
        'wms.rules.off': '⏸',
        'wms.rules.none': 'The rules file has no rules.',
        'wms.status': (
            '<b>PikPak warehouse</b>\n'
            '{files} entries indexed, last stocktake {when}.\n'
            '{open} plan(s) waiting. Scheduled: {jobs}.'
        ),
        'wms.panel.open': 'Open the warehouse panel',
        'wms.panel.unavailable': 'The panel is not available: {reason}.',
        'wms.panel.title': 'PikPak warehouse',
        'wms.panel.subtitle': 'Plans wait here until you apply them. Every change can be undone.',
        'wms.panel.tab_plans': 'Plans',
        'wms.panel.tab_audit': 'Audit',
        'wms.panel.loading': 'Loading…',
        'wms.panel.no_plans': 'No plans waiting.',
        'wms.panel.no_audit': 'No changes yet.',
        'wms.panel.view': 'View',
        'wms.panel.apply': 'Apply',
        'wms.panel.discard': 'Discard',
        'wms.panel.undo': 'Undo',
        'wms.panel.back': 'Back',
        'wms.panel.confirm_apply': 'Apply this plan to your PikPak drive?',
        'wms.panel.confirm_discard': 'Discard this plan without applying it?',
        'wms.panel.confirm_undo': 'Undo this change?',
        'wms.panel.done': 'Done.',
        'wms.panel.unreachable': 'Could not reach the bot.',
        'wms.panel.status': (
            '{files} entries indexed, last stocktake {when}, {open} plan(s) waiting'
        ),
        'wms.panel.bad_request': 'Malformed request.',
        'wms.panel.who': 'Telegram could not confirm who you are. Reopen this page from the bot.',
        'wms.panel.admins_only': 'Only admins can use the warehouse panel.',
        'wms.panel.no_http': (
            'the bot has no public HTTPS address to serve the panel from '
            '(HTTP_ENABLED and PUBLIC_BASE_URL)'
        ),
        'wms.panel.no_https': 'PUBLIC_BASE_URL is {url}; Telegram opens Mini Apps only over HTTPS',
        'setup.status.title': '<b>Setup</b>',
        'setup.status.bot': '✅ <b>Bot account</b> — connected, you are talking to it',
        'setup.status.source_env': 'from the environment',
        'setup.status.source_chat': 'from an in-chat login',
        'setup.status.source_file': 'from a session file',
        'setup.status.reading_ok': '✅ <b>Reading account</b> — connected {source}',
        'setup.status.reading_missing': (
            '⬜ <b>Reading account</b> — needed for private and save-restricted chats\n'
            '    <code>/setup telegram</code> to sign in here'
        ),
        'setup.status.pikpak_own': 'your own account',
        'setup.status.pikpak_shared': 'the shared account',
        'setup.status.pikpak_ok': '✅ <b>PikPak</b> — {which}',
        'setup.status.pikpak_missing': (
            '⬜ <b>PikPak</b> — optional, for cloud transfers\n'
            '    <code>/setup pikpak</code> to sign in here'
        ),
        'setup.status.cache_ok': '✅ <b>Upload cache</b> — configured',
        'setup.status.cache_missing': (
            '⬜ <b>Upload cache</b> — optional. Add me to a private channel as admin, then send <co'
            'de>/cache</code> in that channel.'
        ),
        'setup.status.ready': 'You can start sending links. /help lists what I take.',
        'setup.status.public_only': (
            'Public channels already work. Private ones need the reading account.'
        ),
        'setup.pikpak.disabled': 'The operator has disabled per-user PikPak logins.',
        'setup.private_only': 'Message me directly to sign in, not in a group.',
        'setup.pikpak.form': (
            '\n'
            '\n'
            'Prefer a form? <code>/pikpak login</code> opens one inside Telegram instead.'
        ),
        'setup.pikpak.start': (
            '<b>Connect PikPak</b>\n'
            '\n'
            'Send me the email or phone number your PikPak account uses.{alternative}\n'
            '\n'
            'I delete each message as soon as I have read it, and only the access token is stored, '
            'never your password.\n'
            'Send any other command to stop.'
        ),
        'setup.telegram.pinned': (
            '<code>TG_USER_SESSION</code> is set in the environment, so a login here would be ignor'
            'ed. Remove it first, or keep using the session you have.'
        ),
        'setup.telegram.start': (
            '<b>Connect a reading account</b>\n'
            '\n'
            'This signs a normal Telegram account in to me, so I can read chats a bot cannot: priva'
            'te channels you are in, and channels that block saving.\n'
            '\n'
            '⚠️ I am about to ask for a login code. That is only safe because <b>you run this bot y'
            'ourself</b>. Never give a Telegram login code to a bot or person you do not operate.\n'
            '\n'
            'Send the phone number of the account to use, with its country code, like <code>+861380'
            '0138000</code>.\n'
            'Send any other command to stop.'
        ),
        'setup.timeout': 'That setup step timed out. Start again when ready.',
        'setup.failed': (
            '❌ That did not work: {error}\n'
            'Start again when ready.'
        ),
        'setup.cannot_delete': 'I could not delete that message; please delete it yourself.',
        'setup.pikpak.bad_account': 'That does not look like an email or phone number.',
        'setup.pikpak.ask_password': (
            'Account: <code>{account}</code>\n'
            '\n'
            'Now send the password. I will delete it immediately.'
        ),
        'setup.pikpak.signing_in': 'Signing in to PikPak…',
        'setup.retry_password': (
            '❌ {error}\n'
            '\n'
            'Send the password again, or any command to stop.'
        ),
        'setup.pikpak.connected': (
            '✅ PikPak connected as <code>{account}</code>.\n'
            '\n'
            'Your transfers now go to your own drive. <code>/pikpak</code> shows quota, <code>/pikp'
            'ak logout</code> disconnects it.'
        ),
        'setup.telegram.bad_phone': (
            'That does not look like a phone number. Include the country code, like <code>+86138001'
            '38000</code>.'
        ),
        'setup.telegram.phone_invalid': 'Telegram says that phone number is not valid.',
        'setup.telegram.flood': 'Telegram is rate-limiting logins for {seconds}s. Try later.',
        'setup.telegram.code_sent': (
            'Telegram is sending a login code to that account, in the Telegram app itself.\n'
            '\n'
            'Send me the code. Put a space or dash between the digits if Telegram refuses to let yo'
            'u copy it, for example <code>1 2 3 4 5</code>. I delete it immediately.'
        ),
        'setup.telegram.no_digits': 'I could not find any digits in that.',
        'setup.telegram.expired': 'That login expired. Start again with /setup telegram.',
        'setup.telegram.signing_in': 'Signing in…',
        'setup.telegram.two_step': (
            'That account has two-step verification. Send its password; I delete it immediately and'
            ' never store it.'
        ),
        'setup.telegram.code_wrong': 'That code is wrong. Send it again.',
        'setup.telegram.code_expired': 'That code expired. Start again with /setup telegram.',
        'setup.telegram.checking': 'Checking the password…',
        'setup.telegram.saved': (
            '✅ Signed in as <code>{label}</code> and saved.\n'
            '\n'
            'Restart the bot to start using it.'
        ),
        'setup.telegram.not_adopted': (
            '✅ Signed in as <code>{label}</code> and saved, but could not bring it into service no'
            'w ({error}). Restart the bot.'
        ),
        'setup.telegram.connected': (
            '✅ Reading account connected: <code>{account}</code>\n'
            '\n'
            'Private and save-restricted chats work now, with no restart. Send me a link to try it.'
            '\n'
            '\n'
            'The session is stored on this server and is as sensitive as the account password. Revo'
            'ke it any time from Telegram → Settings → Devices.'
        ),
        'err.resolve.invite_invalid': 'that invite link is invalid or has expired',
        'err.resolve.not_member_invite': (
            'the account is not a member of “{title}”. Join it first, or enable download.auto_join_'
            'invites.'
        ),
        'err.resolve.joined_no_chat': 'joined “{title}” but Telegram returned no chat',
        'err.resolve.private': 'that chat is private and the account is not a member of it',
        'err.resolve.unreachable': (
            'chat {chat} is not reachable. The account reading messages must be a member of it.'
        ),
        'err.resolve.no_username': 'no chat called @{name} exists',
        'err.resolve.username_private': '@{name} is private and the account is not a member of it',
        'err.resolve.unresolvable': 'could not resolve @{name}',
        'err.resolve.bad_ids': 'Telegram rejected those message ids for this chat',
        'err.resolve.admin_required': 'the account needs admin rights in that chat to read it',
        'err.resolve.flood': 'Telegram asked us to wait {seconds}s before reading that chat again',
        'err.resolve.no_thread_access': 'that post has no comment thread the account can read',
        'err.resolve.no_thread': 'that post has no comment thread',
        'err.resolve.comment_gone': 'comment {id} no longer exists',
        'err.resolve.invite_not_message': (
            'that invite link points at a chat, not at a message. Send a message link such as https'
            '://t.me/c/123456/789.'
        ),
        'err.resolve.no_message': 'no message found at {where} (it may have been deleted)',
        'err.pikpak.login_failed': 'PikPak login failed: {error}',
        'err.pikpak.session_expired': (
            'your PikPak session has expired. Use /pikpak login to connect your account again.'
        ),
        'err.pikpak.no_account': (
            'no PikPak account is connected and none is configured on the server. Use /pikpak login'
            ' to connect yours.'
        ),
        'err.pikpak.rejected': 'PikPak rejected those credentials: {error}',
        'err.pikpak.folder_open': 'could not open PikPak folder {folder}: {error}',
        'err.pikpak.folder_create': 'could not create PikPak folder {folder}',
        'err.pikpak.transfer_refused': 'PikPak refused the transfer: {error}',
        'err.pikpak.not_share': '{url} is not a PikPak share link',
        'err.pikpak.share_unreadable_detail': 'could not read the share link: {error}',
        'err.pikpak.share_unreadable': 'could not read the share link',
        'err.pikpak.share_unexpected': 'PikPak returned an unexpected share response',
        'err.pikpak.share_status': (
            'the share link is not usable (status {status}); it may be expired or need a password'
        ),
        'err.pikpak.share_empty': 'the share link contains no files',
        'err.pikpak.share_save_failed': 'saving the share failed: {error}',
        'err.pikpak.quota': 'could not read PikPak quota: {error}',
        'err.link.chat_id': 'not a numeric chat id: {value!r}',
        'err.link.range': 'range {start}-{end} covers too many messages (limit {limit})',
        'err.link.no_path': 'the link has no path, so it points at no chat',
        'err.link.phone': 'that is a phone-number link, not an invite link',
        'err.link.bad_invite': 'malformed invite hash: {hash!r}',
        'err.link.c_needs_ids': 'a t.me/c link needs both a chat id and a message id',
        'err.link.not_chat': 't.me/{name} is not a chat link',
        'err.link.bad_username': '{name!r} is not a valid Telegram username',
        'err.link.no_message_id': '{chat} has no message id in the link',
        'err.link.bad_id': '{value!r} is not a message id or range',
        'err.passthrough': '{error}',
        'err.delivery.flood': 'Telegram asked us to wait {seconds}s before uploading again',
        'err.delivery.upload_failed': 'upload failed: {error}',
        'err.delivery.no_pikpak': (
            'no PikPak account is connected. Use /pikpak login to connect yours, or ask the operato'
            'r to configure a shared account.'
        ),
        'err.delivery.needs_http': (
            'PikPak cannot fetch Telegram media without the HTTP file server. Set HTTP_ENABLED=true'
            ' and PUBLIC_BASE_URL, or use /mode local. Magnet and URL transfers work without it.'
        ),
        'err.delivery.pikpak_error': 'PikPak reported an error fetching the file',
        'err.delivery.pikpak_failed': 'PikPak could not fetch the file: {reason}',
        'err.delivery.pikpak_no_task': (
            'PikPak accepted the request but created no download task; nothing was saved'
        ),
        'err.delivery.too_large': '{size} is over the {limit} a bot can upload',
        'delivery.sent': 'sent {size}',
        'delivery.saved_local': 'saved to <code>{path}</code> ({size})',
        'delivery.saved_pikpak': 'saved to PikPak <code>{path}</code>',
        'delivery.pikpak_fetching': (
            'PikPak is still fetching <code>{name}</code>; it will appear in your drive shortly'
        ),
        'delivery.pikpak_queued': 'queued in PikPak: <code>{name}</code> → {folder}',
        'err.download.flood': 'Telegram asked us to wait {seconds}s; try again later',
        'err.download.disk': (
            'not enough free disk space: {needed:.0f} MiB needed, {free:.0f} MiB free'
        ),
        'err.download.no_file': 'Telegram returned no file for that message',
        'err.download.attempts': 'download failed after {attempts} attempts: {error}',
        'err.token.empty': 'the bot token is empty; get one from @BotFather',
        'err.token.colon': 'a bot token looks like 123456789:AA... — one colon, id first',
        'err.token.bad_id': 'the part before the colon should be the numeric bot id, got {value!r}',
        'err.token.secret_length': (
            'the secret after the colon is {length} characters; BotFather issues about 35'
        ),
        'err.claim.taken': 'this bot already has an admin, so it cannot be claimed again',
        'err.claim.no_code': (
            'no claim code has been issued. Restart the bot and read the code from its log.'
        ),
        'err.claim.wrong': 'that claim code is wrong',
        'verify.check.configuration': 'configuration',
        'verify.check.configuration_note': 'configuration note',
        'verify.check.access_control': 'access control',
        'verify.check.bot_token': 'bot token',
        'verify.check.directories': 'directories',
        'verify.check.bot_identity': 'bot identity',
        'verify.check.bot_created': 'bot created',
        'verify.check.user_session': 'user session',
        'verify.check.account_separation': 'account separation',
        'verify.check.history_access': 'history access',
        'verify.check.cache_chat': 'cache chat',
        'verify.check.forward_fast_path': 'forward fast path',
        'verify.check.pikpak_account': 'pikpak account',
        'verify.check.pikpak_folder': 'pikpak folder',
        'verify.check.pikpak_login': 'pikpak login',
        'verify.check.http_server': 'http server',
        'verify.check.public_reachability': 'public reachability',
        'verify.check.database': 'database',
        'verify.no_checks': 'no checks ran',
        'verify.verdict.failed': '{count} check(s) failed — the bot will not work as configured',
        'verify.verdict.warnings': 'everything essential passed, with {count} warning(s)',
        'verify.verdict.ok': 'everything passed',
        'verify.config.ok': 'loaded and internally consistent',
        'verify.access.open': (
            'open to every Telegram user; the bot can read anything your account can see'
        ),
        'verify.access.none': (
            'no admin or allowed user ids, so every request will be refused. Send /claim with the c'
            "ode from the bot's log, or set ADMIN_USER_IDS."
        ),
        'verify.access.ok': '{admins} admin(s), {users} additional user(s)',
        'verify.token.ok': 'well-formed, names bot id {id}',
        'verify.bot.no_token': 'no usable token to check',
        'verify.timeout': 'timed out connecting to Telegram',
        'verify.bot.sign_in_failed': (
            'could not sign in as the bot: {error}. Check the token with @BotFather, and check that'
            ' this host can open a direct TCP connection to Telegram — MTProto is not plain HTTPS, '
            'so an HTTPS-only proxy will block it.'
        ),
        'verify.bot.read_failed': 'could not read the bot account: {error}',
        'verify.bot.created': '{account} — {link}',
        'verify.bot.no_username': '{account} — no username',
        'verify.bot.not_bot': 'that token belongs to an account Telegram does not mark as a bot',
        'verify.bot.mismatch': (
            'the token names bot id {expected} but the account that answered is {actual}'
        ),
        'verify.bot.ok': 'id {id} matches the token, and is a bot',
        'verify.user.missing': (
            'not configured. Without one, only chats the bot itself is in can be read. Sign one in '
            'from Telegram with /setup telegram.'
        ),
        'verify.user.connect_failed': 'could not connect: {error}',
        'verify.user.not_authorised': (
            'the session from {source} is not authorised; sign in again with /setup telegram'
        ),
        'verify.user.read_failed': 'could not read the account: {error}',
        'verify.user.is_bot': 'that session belongs to a bot; downloads need a real account',
        'verify.user.ok': '{account} from {source}, authorised{premium}',
        'verify.user.premium': ' Telegram Premium',
        'verify.source.in_chat': 'an in-chat login',
        'verify.accounts.need_both': 'needs both accounts',
        'verify.accounts.same': 'the bot and the reading account are the same account',
        'verify.accounts.ok': 'bot {bot} reads through account {user}',
        'verify.history.failed': 'could not list dialogs: {error}',
        'verify.history.empty': 'the account has no chats, so there is nothing to download from',
        'verify.history.ok': 'the account can list its chats',
        'verify.cache.unset': 'not configured; every request re-downloads (set CACHE_CHAT_ID)',
        'verify.cache.cannot_see': (
            'the bot cannot see chat {chat}: {error}. Add the bot to it as an administrator.'
        ),
        'verify.cache.not_admin': (
            'the bot is in chat {chat} but is not an administrator; it may not be able to post or r'
            'ead back uploads'
        ),
        'verify.cache.ok': 'the bot administrates chat {chat}',
        'verify.cache.no_bot': 'cannot be checked without a working bot',
        'verify.forward.bot_reads': (
            'the bot reads for itself and re-sends forwardable files directly'
        ),
        'verify.forward.no_cache': (
            'no cache channel, so forwardable files are downloaded and re-uploaded. Send /cache in '
            'a private channel to make them instant.'
        ),
        'verify.forward.cannot_see': (
            'the reading account cannot see cache channel {chat} ({error}). Add it to the channel, '
            'with permission to post.'
        ),
        'verify.forward.ok': 'the reading account can forward into {chat}',
        'verify.forward.cannot_post': (
            'the reading account is in {chat} but cannot post there; make it an admin with permissi'
            'on to post'
        ),
        'verify.pikpak.ok': '{username} signed in, {used} of {limit} used',
        'verify.pikpak.folder': 'transfers land in {folder}',
        'verify.pikpak.no_shared': 'no shared account configured; users connect their own instead',
        'verify.login.disabled': 'disabled (pikpak.allow_user_login)',
        'verify.login.needs_https': (
            'the Mini App needs HTTP_ENABLED=true and an HTTPS PUBLIC_BASE_URL; until then users co'
            'nnect with /setup pikpak'
        ),
        'verify.login.ok': '/pikpak login opens {url}/pikpak/app in Telegram',
        'verify.http.disabled': (
            'disabled; magnet and URL transfers still work, Telegram media cannot reach PikPak'
        ),
        'verify.http.bind_failed': (
            'could not bind {host}:{port}: {error}. If the bot is already running, this port is exp'
            'ected to be busy.'
        ),
        'verify.http.bound': 'bound {host}:{port}',
        'verify.reach.ok': '{url} answers, so PikPak can fetch files',
        'verify.reach.status': '{url} answered HTTP {status}; check the reverse proxy',
        'verify.reach.failed': (
            'could not fetch {url} from this host ({error}). Verify from outside; NAT hairpinning o'
            'ften breaks a self-test.'
        ),
        'verify.db.ok': 'opened {path}',
        'verify.db.failed': 'could not open {path}: {error}',
        'verify.live.user_missing': 'not configured; only chats the bot itself is in can be read',
        'verify.live.user_ok': '{account}, authorised',
        'verify.live.source_own': 'your own account',
        'verify.live.source_shared': 'the shared account ({username})',
        'verify.live.no_pikpak': (
            'no account connected for you and none configured on the server; use /pikpak login'
        ),
        'verify.live.pikpak_ok': '{source}, {used} of {limit} used',
        'verify.live.pikpak_failed': '{source}: {error}',
        'verify.live.login_ok': '/pikpak login opens the Mini App',
        'verify.live.login_none': 'no Mini App ({reason}); /setup pikpak works',
        'verify.live.http_ok': 'serving at {url}',
        'verify.live.no_url': 'no public URL set',
        'verify.live.http_disabled': 'disabled; Telegram media cannot be transferred to PikPak',
        'verify.cli.config_error': 'configuration error: {error}',
        'verify.cli.start': 'Verifying tg_media_down_bot setup…',
        'portal.title': 'Connect PikPak',
        'portal.heading': 'Connect your PikPak account',
        'portal.sub': (
            'This page belongs to your own media-downloader bot. It is not operated by PikPak.'
        ),
        'portal.username': 'PikPak email or phone',
        'portal.password': 'PikPak password',
        'portal.connect': 'Connect',
        'portal.note': (
            'Telegram tells me who you are, so there is no login link to leak. Your password is sen'
            't to PikPak once to obtain an access token; only the token is stored. Disconnect any t'
            'ime with {command}.'
        ),
        'portal.js.connecting': 'Connecting…',
        'portal.js.connected': 'PikPak connected',
        'portal.js.connected_sub': 'Transfers now go to your own PikPak account.',
        'portal.js.failed': 'That did not work.',
        'portal.js.unreachable': 'Could not reach the bot: {error}',
        'portal.err.disabled': 'Per-user PikPak logins are disabled.',
        'portal.err.malformed': 'Malformed request.',
        'portal.err.identity': (
            'Telegram could not confirm who you are. Reopen this page from the bot.'
        ),
        'portal.err.not_allowed': 'You are not allowed to use this bot.',
        'portal.err.throttled': 'Too many attempts. Wait a few minutes.',
        'portal.err.fields': 'Enter both fields.',
        'portal.reason.disabled': (
            'the operator has disabled per-user PikPak logins (pikpak.allow_user_login)'
        ),
        'portal.reason.no_http': (
            "the bot's HTTP server is not running with a public address, so there is nowhere to ser"
            've the login form. Set HTTP_ENABLED=true and PUBLIC_BASE_URL.'
        ),
        'portal.reason.not_attached': 'the login form is not attached to the HTTP server',
        'portal.reason.plain_http': (
            'PUBLIC_BASE_URL is {url}, which is plain HTTP. Telegram only opens Mini Apps over HTTP'
            'S; put a TLS reverse proxy in front of the bot.'
        ),
        'err.session.not_a_session': 'that is not a Telegram session string: {error}',
        'err.session.not_authorized': 'the new session is not authorized',
    },
    "zh": {
        'help.body': (
                         '<b>Telegram 媒体下载机器人</b>\n'
                         '\n'
                         '把 Telegram 消息链接发给我，我会把它背后的媒体抓下来 —— '
                         '只要读取账号是该频道的成员，\n'
                         '即使频道禁止保存内容也可以。\n'
                         '\n'
                         '<b>我认得的链接</b>\n'
                         '• <code>https://t.me/channel/123</code> — 公开频道或群组\n'
                         '• <code>https://t.me/c/1234567890/123</code> — 私有会话\n'
                         '• <code>https://t.me/channel/12/123</code> — 论坛话题\n'
                         '• <code>https://t.me/channel/100-120</code> — 一段连续的消息\n'
                         '• 加 <code>?single</code> 只取相册里的这一项，'
                         '加 <code>?comment=45</code> 取某条评论\n'
                         '• 磁力链接或直链 — 直接交给 PikPak\n'
                         '• PikPak 分享链接 — 转存进你的网盘\n'
                         '• 直接发给我或转发给我的媒体\n'
                         '\n'
                         '<b>命令</b>\n'
                         '/mode — 文件送到哪里：telegram、local、pikpak 或 auto\n'
                         '/status — 我正在处理什么\n'
                         '/cancel [id] — 取消某个任务，或全部取消\n'
                         '/stats — 你最近的任务\n'
                         '/pikpak — PikPak 账号、容量和目标文件夹\n'
                         '/pikpak login — 连接你自己的 PikPak 账号\n'
                         '/wms — PikPak 仓储：计划、审计、撤销（管理员；/wms help）\n'
                         '/do — 用一句话管理网盘，比如「下载今天转存的视频」（管理员）\n'
                         '/id — 你的 Telegram 用户 id\n'
                         '/help — 这条消息'
                     ),
        'help.admin': (
                          '\n'
                          '/setup — 在这里完成配置：登录读取账号或 PikPak\n'
                          '/cache — 指定一个频道作为上传缓存\n'
                          '/verify — 检查机器人的身份和配置'
                      ),
        'help.unclaimed': (
                              '<b>这个机器人还没有管理员</b>\n'
                              '\n'
                              '部署我的人会在启动日志里看到一个认领码。把它发到这里：\n'
                              '\n'
                              '<code>/claim &lt;认领码&gt;</code>\n'
                              '\n'
                              '这样你就成为管理员，不需要重新部署。'
                              '在那之前我会拒绝所有请求，包括你的。'
                          ),
        'help.no_reading_account': (
                                       '\n'
                                       '\n'
                                       '⚠️ '
                                       '还没有连接读取账号，'
                                       '所以私有会话和禁止保存的会话暂时用不了。'
                                   ),
        'help.no_reading_account_admin': ' 发送 <code>/setup</code> 就能在这里完成。',
        'access.denied': (
                             '你没有使用这个机器人的权限。\n'
                             '\n'
                             '你的用户 id 是 <code>{user_id}</code>。管理员可以把它加进 '
                             '<code>ALLOWED_USER_IDS</code>。'
                         ),
        'error.generic': '❌ {error}',
        'id.reply': (
                        '你的用户 id：<code>{user_id}</code>\n'
                        '当前会话 id：<code>{chat_id}</code>'
                    ),
        'mode.current': (
                            '当前模式：<b>{current}</b>\n'
                            '\n'
                            '<code>/mode telegram</code> — 把文件传回给你\n'
                            '<code>/mode local</code> — 留在服务器磁盘上\n'
                            '<code>/mode pikpak</code> — 转存进 PikPak\n'
                            '<code>/mode auto</code> — 能转发就秒传给你，受限的存到 NAS'
                        ),
        'mode.unknown': '不认识的模式 {choice}。只能是以下之一：{modes}',
        'mode.pikpak_none': (
            '还没有连接 PikPak 账号。'
            '发送 <code>/pikpak login</code> 连接你自己的账号。'
        ),
        'mode.pikpak_unavailable': (
            '这台服务器上 PikPak 不可用。请运维配置 PIKPAK_USERNAME 和 '
            'PIKPAK_PASSWORD，或者允许用户自行登录。'
        ),
        'mode.set': '模式已设为 <b>{choice}</b>。',
        'mode.name.telegram': 'telegram（传回给你）',
        'mode.name.local': 'local（留在服务器）',
        'mode.name.pikpak': 'pikpak（转存网盘）',
        'mode.name.auto': 'auto（能转发就秒传，受限的存 NAS）',
        'status.empty': '你的队列是空的。',
        'status.header': '<b>你的队列里有 {count} 项</b>',
        'state.queued': '排队中',
        'state.running': '进行中',
        'state.done': '已完成',
        'state.failed': '失败',
        'state.cancelled': '已取消',
        'state.partial': '部分完成',
        'cancel.no_match': '没找到匹配的任务。',
        'cancel.nothing': '没有可取消的任务。',
        'cancel.cancelling': '正在取消 {ids}。',
        'stats.empty': '你还没有下载过任何东西。',
        'stats.none': '无',
        'stats.totals': (
                            '<b>累计</b>\n'
                            '{totals}\n'
                            '已传输：{transferred}'
                        ),
        'stats.recent_header': (
                                   '\n'
                                   '<b>最近</b>'
                               ),
        'pikpak.login.disabled': '运维关闭了「每用户自行登录 PikPak」这个功能。',
        'pikpak.login.replacing': (
                                      '\n'
                                      '\n'
                                      '这会替换掉你现在已连接的账号。'
                                  ),
        'pikpak.login.miniapp': (
                                    '<b>连接你的 PikPak 账号</b>\n'
                                    '\n'
                                    '点下面的按钮，表单会在 Telegram 内部打开。'
                                    '没有链接会外泄：Telegram 会直接告诉我你是谁。\n'
                                    '\n'
                                    '你的密码只会发给 PikPak '
                                    '一次，换回一个访问令牌。我只保存令牌，不保存密码。'
                                    '{replacing}'
                                ),
        'pikpak.login.button_miniapp': '🔐 连接 PikPak',
        'pikpak.login.private_only': (
            '请私聊我来连接 PikPak。'
            'Telegram 只在私聊里打开登录表单，密码也不该发在群里。'
        ),
        'pikpak.login.chat_fallback': (
                                          '<b>连接你的 PikPak 账号</b>\n'
                                          '\n'
                                          '发送 <code>/setup '
                                          'pikpak</code>，我会在这里问你的邮箱和密码，'
                                          '每条消息读完立即删除。\n'
                                          '\n'
                                          '<i>Telegram 内的表单不可用：{reason}</i>{replacing}'
                                      ),
        'pikpak.logout.only_admin_shared': '只有管理员能清除共享会话。',
        'pikpak.logout.shared_cleared': '共享的 PikPak 会话已清除。',
        'pikpak.logout.none': '你没有连接 PikPak 账号。',
        'pikpak.logout.done': '你的 PikPak 账号已断开，保存的令牌也已删除。',
        'pikpak.dir.current': (
                                  '你的 PikPak 文件夹：<code>{folder}</code>\n'
                                  '用 <code>/pikpak dir /Movies/Anime</code> 修改它。'
                              ),
        'pikpak.dir.set': 'PikPak 文件夹已设为 <code>{folder}</code>。',
        'pikpak.status.none': (
            '还没有连接 PikPak 账号。'
            '发送 <code>/pikpak login</code> 连接你自己的账号。'
        ),
        'pikpak.status.account_own': '你自己的账号',
        'pikpak.status.account_shared': '共享账号（{username}）',
        'pikpak.status.transfers_full': '磁力链接、直链、分享链接，以及 Telegram 媒体',
        'pikpak.status.transfers_limited': '磁力链接、直链和分享链接',
        'pikpak.status.footer_own': (
                                        '\n'
                                        '\n'
                                        '<code>/pikpak logout</code> 可以断开你的账号。'
                                    ),
        'pikpak.status.footer_shared': (
                                           '\n'
                                           '\n'
                                           '<code>/pikpak login</code> '
                                           '可以改用你自己的账号。'
                                       ),
        'pikpak.status.body': (
                                  '<b>PikPak</b>\n'
                                  '账号：{account}\n'
                                  '容量：已用 {used} / 共 {limit}（{percent}%）\n'
                                  '文件夹：<code>{folder}</code>\n'
                                  '支持转存：{transfers}{footer}'
                              ),
        'claim.already': '这个机器人已经有管理员了。',
        'claim.success': (
                             '✅ <b>你现在是管理员了。</b>\n'
                             '\n'
                             '不需要再部署任何东西。'
                             '发送 <code>/setup</code> 就能在这里登录读取账号、连接 '
                             'PikPak。'
                         ),
        'cache.only_admin': '只有管理员能修改上传缓存。',
        'cache.channel_not_admin': (
            '这个频道的管理员里没有本机器人的管理员，不能设为上传缓存。请把管理员本人的账号设为频道管理员'
            '，再发一次 /cache。'
        ),
        'cache.disabled': '上传缓存已关闭。之后每次请求都会重新下载。',
        'cache.bad_id': (
                            '这看起来不像一个会话 id。id 的样子是 '
                            '<code>-1001234567890</code>。'
                        ),
        'cache.state_current': '当前使用 <code>{chat_id}</code>。',
        'cache.state_none': '还没有设置上传缓存，所以每次请求都会重新下载。',
        'cache.help': (
                          '<b>上传缓存</b>\n'
                          '\n'
                          '{state}\n'
                          '\n'
                          '设置方法：建一个私有频道，把我加为<b>管理员</b>，'
                          '然后<b>在那个频道里</b>发一条 <code>/cache</code>。'
                          '我会自己记下它的 id。\n'
                          '\n'
                          '<code>/cache off</code> 可以关闭它。'
                      ),
        'cache.cannot_see': (
                                '我看不到那个会话。请先把我加进去并设为管理员。\n'
                                '\n'
                                '<i>{error}</i>'
                            ),
        'cache.not_admin': (
                               '我在那个会话里，但不是管理员，所以没法往里存上传的文件。'
                               '把我提升为管理员再试一次。'
                           ),
        'cache.set': (
                         '✅ 上传缓存已设为 <code>{chat_id}</code>。\n'
                         '\n'
                         '同一条链接再请求一次时，会直接从 Telegram '
                         '转发，不再重新下载。这个设置重启后依然有效，不需要重新部署。'
                     ),
        'setup.unavailable': '这个构建里没有配置向导。',
        'setup.cancelled': '配置已取消。',
        'setup.nothing_to_cancel': '没有可取消的操作。',
        'setup.only_admin': '只有管理员能登录读取账号。',
        'setup.footer': (
                            '\n'
                            '\n'
                            '<code>/setup telegram</code> · <code>/setup pikpak</code> · <c'
                            'ode>/setup cancel</code>'
                        ),
        'verify.only_admin': '只有管理员能运行 /verify。',
        'verify.checking': '检查中…',
        'verify.failed': '❌ 检查失败：{error}',
        'dispatch.prompt': (
                               '给我发一个 Telegram 消息链接、磁力链接，'
                               '或者直接发媒体文件。/help 列出了我能处理的全部类型。'
                           ),
        'dispatch.errors_header': '这些链接我处理不了：',
        'inbound.note_local': (
                                  '（这里用 <b>telegram</b> '
                                  '模式没有意义，已改为保存到本地）'
                              ),
        'inbound.queued': '已加入队列 <code>#{job_id}</code>{note}。',
        'bundle.queued': '已加入队列 {count} 个任务：<code>{ids}</code> → <b>{mode}</b>',
        'bundle.needs_user_session': '{ref} 需要用户会话，但还没有配置',
        'menu.help': '我能接什么、能做什么',
        'menu.claim': '认领一个刚部署好的机器人',
        'menu.setup': '完成配置：登录账号或 PikPak',
        'menu.cache': '指定一个频道作为上传缓存',
        'menu.mode': '文件送到哪：telegram / local / pikpak',
        'menu.status': '我正在处理什么',
        'menu.cancel': '取消一个任务，或全部取消',
        'menu.stats': '你最近的任务',
        'menu.pikpak': 'PikPak 账号、容量和文件夹',
        'menu.wms': 'PikPak 仓储：计划、审计、撤销',
        'menu.do': '用一句话管理网盘',
        'menu.verify': '检查我的身份和配置',
        'menu.id': '你的 Telegram 用户 id',
        'profile.about': (
                             '把 Telegram 消息链接发给我，我把背后的媒体抓下来，或者转存到 '
                             'PikPak。'
                         ),
        'profile.description': (
                                   '把任意 Telegram 消息链接发给我，我会抓取它背后的媒体，'
                                   '即使频道禁止保存内容也可以。我可以把文件传回给你、'
                                   '留在服务器上，或者转存进 PikPak。磁力链接、'
                                   '直链和 PikPak 分享链接会直接交给 PikPak。\n'
                                   '\n'
                                   '发送 /setup 完成登录，或发送 /help '
                                   '查看我能处理的全部类型。'
                               ),
        'manual.setprivacy.label': '/setprivacy → Disable',
        'manual.setprivacy.why': (
                                     '让我能看到我所在群组里发的链接。如果你只打算私聊我，'
                                     '这一步可以跳过。这个开关没有 API，只能在 @BotFather '
                                     '里手动设置。'
                                 ),
        'job.queue_full': (
                              '你已经有 {limit} 个任务在排队了，等它们跑完，或者用 /cancel '
                              '取消'
                          ),
        'job.pikpak.sending': '⏳ 正在发送到 PikPak：<code>{label}</code>',
        'job.pikpak.no_account': (
                                     '❌ 没有连接 PikPak 账号，磁力链接和直链没有别的去处。'
                                     '用 /pikpak login 连接你自己的账号。'
                                 ),
        'job.share.saving': '⏳ 正在转存 PikPak 分享…',
        'job.share.more': (
                              '\n'
                              '… 还有 {count} 项'
                          ),
        'job.share.saved': (
                               '✅ 已转存 {count} 项到 PikPak：\n'
                               '{listing}'
                           ),
        'job.message.no_media': '❌ 那条消息里没有可下载的媒体。',
        'job.cancelled': '🚫 已取消。',
        'job.cancelled_after': '🚫 已取消，此前完成了 {count} 个文件。',
        'job.inbound.downloading': '⬇️ <code>{name}</code>（{size}）',
        'job.lookup': '🔍 正在查找 <code>{ref}</code>…',
        'job.cached': '♻️ {prefix}<code>{name}</code> 命中缓存，直接转发',
        'job.forwarded': '⚡ {prefix}<code>{name}</code> — 由 Telegram 直接复制，未下载',
        'job.hint_cache': '\n💡 管理员在一个私有频道里发送 /cache，这类文件就能秒转。',
        'job.downloading_progress': (
                                        '⬇️ {prefix}<code>{label}</code>\n'
                                        '{bar} {percent}%（{received} / {total}）\n'
                                        '{rate} · 预计还需 {eta}'
                                    ),
        'job.downloading': '⬇️ {prefix}<code>{label}</code>（{size}）',
        'job.delivered': '✅ {prefix}<code>{label}</code> — {summary}',
        'job.uploading': (
                             '⬆️ {prefix}<code>{name}</code>\n'
                             '{bar} {percent}%（{sent} / {total}）\n'
                             '{rate}'
                         ),
        'job.handing_to_pikpak': '☁️ {prefix}正在把 <code>{name}</code> 交给 PikPak…',
        'job.note_skipped': '{count} 条消息里没有媒体',
        'job.note_truncated': '只处理了前 {cap} 条消息（受 download.max_batch 限制）',
        'job.note_failed': (
                               '{count} 个失败：\n'
                               '{shown}'
                           ),
        'job.note_more': (
                             '\n'
                             '… 还有 {count} 个'
                         ),
        'job.summary': '{icon} 已投递 {succeeded}/{total}',
        'job.nothing_delivered': '❌ 没有任何内容被投递。',
        'wms.off': 'PikPak 仓储未开启。请设置 WMS_ENABLED=true 并重启 bot。',
        'wms.admins_only': '只有管理员能管理 PikPak 仓储。',
        'wms.usage': (
            '<b>/wms</b> — 仓储状态与面板\n'
            '/wms stocktake [full] — 刷新本地索引\n'
            '/wms plan — 按整理规则出一份计划\n'
            '/wms plan &lt;编号&gt; — 查看计划\n'
            '/wms apply &lt;编号&gt; — 执行计划\n'
            '/wms undo &lt;审计编号&gt; — 撤销一处改动\n'
            '/wms rules — 当前生效的规则'
        ),
        'wms.working': '处理中……',
        'wms.failed_see_log': '失败了，详情见部署日志。',
        'wms.error': '仓储：{error}',
        'wms.bad_id': '「{value}」不是一个编号。',
        'wms.button.apply': '确认执行',
        'wms.button.discard': '丢弃',
        'wms.button.undo': '确认撤销',
        'wms.button.edit': '修改',
        'wms.button.cancel': '取消',
        'wms.nl.usage': (
            '用一句话说要对网盘做什么，比如：\n'
            '<code>/do 下载今天转存到网盘的所有大于1GB的视频</code>\n'
            '管理员在私聊里也可以直接发这句话。'
        ),
        'wms.nl.intro': '计划如下（由 {translator} 理解）。现在还什么都没改。',
        'wms.nl.ask': '🤔 {question}\n直接回复补充的部分，会接在你刚才那句话后面。',
        'wms.nl.not_understood': (
            '没听懂这是一条网盘指令。发链接照常下载；管理网盘可以这样说：'
            '<code>下载今天转存的大于1GB的视频</code>。'
        ),
        'wms.nl.failed': '翻译出错：{error}',
        'wms.nl.edit_prompt': '请发送要修改或补充的内容，会接在「{sentence}」后面重新理解。',
        'wms.nl.cancelled': '已取消，什么都没改。',
        'wms.nl.expired': '这个计划已过期，请重新发送那句话。',
        'wms.nl.rule_added': '已写入 {path}。以后按时运行，每次改动前都会先问你。',
        'wms.job.waiting': '⏰ 定时任务「{name}」生成了一份计划，等你确认：',
        'wms.discarded': '计划 {id} 已丢弃。',
        'wms.undo.preview': '将撤销：\n<code>{what}</code>',
        'wms.undo.done': '已撤销：<code>{what}</code>',
        'wms.shelved.planned': '📦 PikPak 里有新文件，按规则将会这样整理：',
        'wms.shelved.applied': '📦 PikPak 里的新文件已按规则上架。{summary}',
        'wms.shelved.uncovered': (
            '📦 新文件存到了 PikPak 的 {folders}，但没有任何整理规则覆盖这个目录，'
            '所以不会被上架。请把 PIKPAK_FOLDER 设到某条规则的 scope 下（比如 /Inbox），'
            '或者放宽规则的 scope。'
        ),
        'wms.rules.header': '<b>规则</b>（{path}）',
        'wms.rules.line': '{state} <b>{name}</b> [{stage}] {scope}：{actions}',
        'wms.rules.on': '✅',
        'wms.rules.off': '⏸',
        'wms.rules.none': '规则文件里没有规则。',
        'wms.status': (
            '<b>PikPak 仓储</b>\n'
            '索引 {files} 条，上次盘点 {when}。\n'
            '{open} 个计划待确认。定时任务：{jobs}。'
        ),
        'wms.panel.open': '打开仓储面板',
        'wms.panel.unavailable': '面板不可用：{reason}。',
        'wms.panel.title': 'PikPak 仓储',
        'wms.panel.subtitle': '计划在这里等你确认后才执行。每一处改动都可以撤销。',
        'wms.panel.tab_plans': '计划',
        'wms.panel.tab_audit': '审计',
        'wms.panel.loading': '加载中……',
        'wms.panel.no_plans': '没有等待确认的计划。',
        'wms.panel.no_audit': '还没有任何改动。',
        'wms.panel.view': '查看',
        'wms.panel.apply': '确认执行',
        'wms.panel.discard': '丢弃',
        'wms.panel.undo': '撤销',
        'wms.panel.back': '返回',
        'wms.panel.confirm_apply': '确定把这个计划应用到你的 PikPak 网盘吗？',
        'wms.panel.confirm_discard': '确定丢弃这个计划（不执行）吗？',
        'wms.panel.confirm_undo': '确定撤销这处改动吗？',
        'wms.panel.done': '完成。',
        'wms.panel.unreachable': '连不上 bot。',
        'wms.panel.status': '索引 {files} 条，上次盘点 {when}，{open} 个计划待确认',
        'wms.panel.bad_request': '请求格式不对。',
        'wms.panel.who': 'Telegram 无法确认你的身份。请从 bot 里重新打开此页面。',
        'wms.panel.admins_only': '只有管理员能使用仓储面板。',
        'wms.panel.no_http': (
            'bot 没有可以提供面板的公网 HTTPS 地址（HTTP_ENABLED 与 PUBLIC_BASE_URL）'
        ),
        'wms.panel.no_https': 'PUBLIC_BASE_URL 是 {url}；Telegram 只通过 HTTPS 打开 Mini App',
        'setup.status.title': '<b>配置</b>',
        'setup.status.bot': '✅ <b>机器人账号</b> — 已连接，你正在和它对话',
        'setup.status.source_env': '来自环境变量',
        'setup.status.source_chat': '来自聊天内登录',
        'setup.status.source_file': '来自会话文件',
        'setup.status.reading_ok': '✅ <b>读取账号</b> — 已连接，{source}',
        'setup.status.reading_missing': (
            '⬜ <b>读取账号</b> — 私有频道和禁止保存的频道需要它\n'
            '    发 <code>/setup telegram</code> 在这里登录'
        ),
        'setup.status.pikpak_own': '你自己的账号',
        'setup.status.pikpak_shared': '共享账号',
        'setup.status.pikpak_ok': '✅ <b>PikPak</b> — {which}',
        'setup.status.pikpak_missing': (
            '⬜ <b>PikPak</b> — 可选，用于转存到云盘\n'
            '    发 <code>/setup pikpak</code> 在这里登录'
        ),
        'setup.status.cache_ok': '✅ <b>上传缓存</b> — 已配置',
        'setup.status.cache_missing': (
            '⬜ <b>上传缓存</b> — 可选。把我加进一个私有频道并设为管理员，然后在那个频道里发 <code>'
            '/cache</code>。'
        ),
        'setup.status.ready': '现在可以发链接了。/help 列出我能接收的内容。',
        'setup.status.public_only': '公开频道已经可以用了。私有频道需要读取账号。',
        'setup.pikpak.disabled': '运营者关闭了个人 PikPak 登录。',
        'setup.private_only': '请私聊我登录，不要在群里。',
        'setup.pikpak.form': (
            '\n'
            '\n'
            '想用表单？<code>/pikpak login</code> 会在 Telegram 里打开一个。'
        ),
        'setup.pikpak.start': (
            '<b>连接 PikPak</b>\n'
            '\n'
            '请发送你 PikPak 账号的邮箱或手机号。{alternative}\n'
            '\n'
            '每条消息我读完就删，只保存访问令牌，从不保存密码。\n'
            '发送任意其他命令即可中止。'
        ),
        'setup.telegram.pinned': (
            '环境变量里设了 <code>TG_USER_SESSION</code>，在这里登录会被忽略。请先去掉它，或者继续'
            '用现有的会话。'
        ),
        'setup.telegram.start': (
            '<b>连接读取账号</b>\n'
            '\n'
            '这会把一个普通 Telegram 账号登录到我这里，让我能读取机器人读不到的聊天：你所在的私有频'
            '道，以及禁止保存的频道。\n'
            '\n'
            '⚠️ 接下来我会要登录验证码。这只因为<b>这个机器人是你自己运行的</b>才是安全的。绝不要把'
            ' Telegram 登录验证码交给不是你自己运营的机器人或任何人。\n'
            '\n'
            '请发送要使用的账号的手机号，带国家码，例如 <code>+8613800138000</code>。\n'
            '发送任意其他命令即可中止。'
        ),
        'setup.timeout': '这一步配置超时了。准备好后请重新开始。',
        'setup.failed': (
            '❌ 没有成功：{error}\n'
            '准备好后请重新开始。'
        ),
        'setup.cannot_delete': '我删不掉那条消息，请你自己删除。',
        'setup.pikpak.bad_account': '这看起来不像邮箱或手机号。',
        'setup.pikpak.ask_password': (
            '账号：<code>{account}</code>\n'
            '\n'
            '现在请发送密码。我会立刻删除它。'
        ),
        'setup.pikpak.signing_in': '正在登录 PikPak……',
        'setup.retry_password': (
            '❌ {error}\n'
            '\n'
            '请重新发送密码，或发送任意命令中止。'
        ),
        'setup.pikpak.connected': (
            '✅ PikPak 已连接：<code>{account}</code>。\n'
            '\n'
            '你的转存现在会进你自己的网盘。<code>/pikpak</code> 查看容量，<code>/pikpak logout</cod'
            'e> 断开连接。'
        ),
        'setup.telegram.bad_phone': (
            '这看起来不像手机号。请带上国家码，例如 <code>+8613800138000</code>。'
        ),
        'setup.telegram.phone_invalid': 'Telegram 说这个手机号无效。',
        'setup.telegram.flood': 'Telegram 限制了登录频率，需等待 {seconds} 秒。请稍后再试。',
        'setup.telegram.code_sent': (
            'Telegram 正在把登录验证码发到那个账号的 Telegram 应用里。\n'
            '\n'
            '请把验证码发给我。如果 Telegram 不让你直接复制，在数字之间加空格或短横线，例如 <code>1'
            ' 2 3 4 5</code>。我会立刻删除它。'
        ),
        'setup.telegram.no_digits': '里面没有找到任何数字。',
        'setup.telegram.expired': '这次登录已过期。请用 /setup telegram 重新开始。',
        'setup.telegram.signing_in': '正在登录……',
        'setup.telegram.two_step': (
            '这个账号开启了两步验证。请发送它的密码；我会立刻删除，且从不保存。'
        ),
        'setup.telegram.code_wrong': '验证码不对，请重新发送。',
        'setup.telegram.code_expired': '验证码已过期。请用 /setup telegram 重新开始。',
        'setup.telegram.checking': '正在核对密码……',
        'setup.telegram.saved': (
            '✅ 已登录 <code>{label}</code> 并保存。\n'
            '\n'
            '重启机器人后开始使用。'
        ),
        'setup.telegram.not_adopted': (
            '✅ 已登录 <code>{label}</code> 并保存，但现在没法启用它（{error}）。请重启机器人。'
        ),
        'setup.telegram.connected': (
            '✅ 读取账号已连接：<code>{account}</code>\n'
            '\n'
            '私有频道和禁止保存的频道现在就能用，无需重启。发个链接试试。\n'
            '\n'
            '会话保存在这台服务器上，敏感程度等同于账号密码。随时可以在 Telegram → 设置 → 设备 里撤'
            '销。'
        ),
        'err.resolve.invite_invalid': '邀请链接无效或已过期',
        'err.resolve.not_member_invite': (
            '账号还不是「{title}」的成员。请先加入，或开启 download.auto_join_invites。'
        ),
        'err.resolve.joined_no_chat': '已加入「{title}」，但 Telegram 没有返回这个聊天',
        'err.resolve.private': '这个聊天是私密的，账号不是它的成员',
        'err.resolve.unreachable': '无法访问聊天 {chat}。读取消息的账号必须是它的成员。',
        'err.resolve.no_username': '不存在名为 @{name} 的聊天',
        'err.resolve.username_private': '@{name} 是私密的，账号不是它的成员',
        'err.resolve.unresolvable': '无法解析 @{name}',
        'err.resolve.bad_ids': 'Telegram 拒绝了这个聊天里的这些消息 ID',
        'err.resolve.admin_required': '账号需要该聊天的管理员权限才能读取',
        'err.resolve.flood': 'Telegram 要求等待 {seconds} 秒后再读取这个聊天',
        'err.resolve.no_thread_access': '这条帖子没有账号能读取的评论区',
        'err.resolve.no_thread': '这条帖子没有评论区',
        'err.resolve.comment_gone': '评论 {id} 已不存在',
        'err.resolve.invite_not_message': (
            '这个邀请链接指向的是聊天，不是消息。请发送消息链接，例如 https://t.me/c/123456/789。'
        ),
        'err.resolve.no_message': '在 {where} 没有找到消息（可能已被删除）',
        'err.pikpak.login_failed': 'PikPak 登录失败：{error}',
        'err.pikpak.session_expired': '你的 PikPak 登录已过期。请用 /pikpak login 重新连接账号。',
        'err.pikpak.no_account': (
            '没有连接 PikPak 账号，服务器上也没有配置。请用 /pikpak login 连接你的账号。'
        ),
        'err.pikpak.rejected': 'PikPak 拒绝了这组凭据：{error}',
        'err.pikpak.folder_open': '无法打开 PikPak 文件夹 {folder}：{error}',
        'err.pikpak.folder_create': '无法创建 PikPak 文件夹 {folder}',
        'err.pikpak.transfer_refused': 'PikPak 拒绝了这次转存：{error}',
        'err.pikpak.not_share': '{url} 不是 PikPak 分享链接',
        'err.pikpak.share_unreadable_detail': '无法读取分享链接：{error}',
        'err.pikpak.share_unreadable': '无法读取分享链接',
        'err.pikpak.share_unexpected': 'PikPak 返回了无法识别的分享信息',
        'err.pikpak.share_status': '分享链接不可用（状态 {status}），可能已过期或需要提取码',
        'err.pikpak.share_empty': '分享链接里没有文件',
        'err.pikpak.share_save_failed': '转存分享失败：{error}',
        'err.pikpak.quota': '无法读取 PikPak 容量：{error}',
        'err.link.chat_id': '不是数字形式的聊天 ID：{value!r}',
        'err.link.range': '范围 {start}-{end} 包含的消息太多（上限 {limit}）',
        'err.link.no_path': '链接没有路径，指不到任何聊天',
        'err.link.phone': '这是电话号码链接，不是邀请链接',
        'err.link.bad_invite': '邀请码格式不对：{hash!r}',
        'err.link.c_needs_ids': 't.me/c 链接需要同时带有聊天 ID 和消息 ID',
        'err.link.not_chat': 't.me/{name} 不是聊天链接',
        'err.link.bad_username': '{name!r} 不是有效的 Telegram 用户名',
        'err.link.no_message_id': '链接里没有 {chat} 的消息 ID',
        'err.link.bad_id': '{value!r} 不是消息 ID 或范围',
        'err.passthrough': '{error}',
        'err.delivery.flood': 'Telegram 要求等待 {seconds} 秒后再上传',
        'err.delivery.upload_failed': '上传失败：{error}',
        'err.delivery.no_pikpak': (
            '没有连接 PikPak 账号。请用 /pikpak login 连接你的账号，或请管理员配置共享账号。'
        ),
        'err.delivery.needs_http': (
            '没有 HTTP 文件服务器，PikPak 无法拉取 Telegram 媒体。请设置 HTTP_ENABLED=true 和 PUBLI'
            'C_BASE_URL，或改用 /mode local。磁力链接和 URL 转存不受影响。'
        ),
        'err.delivery.pikpak_error': 'PikPak 报告拉取文件时出错',
        'err.delivery.pikpak_failed': 'PikPak 拉取文件失败：{reason}',
        'err.delivery.pikpak_no_task': 'PikPak 接受了请求但没有建立下载任务，文件没有存进网盘',
        'err.delivery.too_large': '{size} 超过了机器人可上传的 {limit}',
        'delivery.sent': '已发送 {size}',
        'delivery.saved_local': '已保存到 <code>{path}</code>（{size}）',
        'delivery.saved_pikpak': '已存入 PikPak <code>{path}</code>',
        'delivery.pikpak_fetching': 'PikPak 仍在拉取 <code>{name}</code>，稍后会出现在网盘里',
        'delivery.pikpak_queued': '已加入 PikPak 离线下载：<code>{name}</code> → {folder}',
        'err.download.flood': 'Telegram 要求等待 {seconds} 秒，请稍后再试',
        'err.download.disk': '磁盘空间不足：需要 {needed:.0f} MiB，剩余 {free:.0f} MiB',
        'err.download.no_file': 'Telegram 没有返回这条消息的文件',
        'err.download.attempts': '重试 {attempts} 次后下载仍然失败：{error}',
        'err.token.empty': '机器人令牌为空，请向 @BotFather 申请一个',
        'err.token.colon': '机器人令牌形如 123456789:AA...：只有一个冒号，前面是 ID',
        'err.token.bad_id': '冒号前面应该是数字形式的机器人 ID，实际是 {value!r}',
        'err.token.secret_length': '冒号后面的密钥有 {length} 个字符，BotFather 发的一般约 35 个',
        'err.claim.taken': '这个机器人已经有管理员，不能再被认领',
        'err.claim.no_code': '还没有生成认领码。请重启机器人，从日志里读取认领码。',
        'err.claim.wrong': '认领码不对',
        'verify.check.configuration': '配置',
        'verify.check.configuration_note': '配置提示',
        'verify.check.access_control': '访问控制',
        'verify.check.bot_token': '机器人令牌',
        'verify.check.directories': '目录',
        'verify.check.bot_identity': '机器人身份',
        'verify.check.bot_created': '机器人账号',
        'verify.check.user_session': '用户会话',
        'verify.check.account_separation': '账号分离',
        'verify.check.history_access': '历史读取',
        'verify.check.cache_chat': '缓存聊天',
        'verify.check.forward_fast_path': '转发快路',
        'verify.check.pikpak_account': 'PikPak 账号',
        'verify.check.pikpak_folder': 'PikPak 文件夹',
        'verify.check.pikpak_login': 'PikPak 登录',
        'verify.check.http_server': 'HTTP 服务',
        'verify.check.public_reachability': '公网可达',
        'verify.check.database': '数据库',
        'verify.no_checks': '没有运行任何检查',
        'verify.verdict.failed': '{count} 项检查失败——按当前配置机器人无法工作',
        'verify.verdict.warnings': '关键项全部通过，有 {count} 条警告',
        'verify.verdict.ok': '全部通过',
        'verify.config.ok': '已加载，各项配置互相一致',
        'verify.access.open': '对所有 Telegram 用户开放；机器人能读取你的账号能看到的一切',
        'verify.access.none': (
            '没有管理员或允许的用户 ID，所有请求都会被拒绝。请用机器人日志里的认领码发送 /claim，或'
            '设置 ADMIN_USER_IDS。'
        ),
        'verify.access.ok': '{admins} 个管理员，另有 {users} 个用户',
        'verify.token.ok': '格式正确，对应机器人 ID {id}',
        'verify.bot.no_token': '没有可用的令牌可供检查',
        'verify.timeout': '连接 Telegram 超时',
        'verify.bot.sign_in_failed': (
            '无法以机器人身份登录：{error}。请在 @BotFather 核对令牌，并确认这台主机能直接与 Telegr'
            'am 建立 TCP 连接——MTProto 不是普通的 HTTPS，只放行 HTTPS 的代理会挡住它。'
        ),
        'verify.bot.read_failed': '无法读取机器人账号：{error}',
        'verify.bot.created': '{account} — {link}',
        'verify.bot.no_username': '{account} — 没有用户名',
        'verify.bot.not_bot': '这个令牌属于一个 Telegram 没有标记为机器人的账号',
        'verify.bot.mismatch': '令牌对应机器人 ID {expected}，但应答的账号是 {actual}',
        'verify.bot.ok': 'ID {id} 与令牌一致，并且是机器人',
        'verify.user.missing': (
            '未配置。没有它，只能读取机器人自己所在的聊天。请在 Telegram 里用 /setup telegram 登录'
            '一个。'
        ),
        'verify.user.connect_failed': '无法连接：{error}',
        'verify.user.not_authorised': '来自 {source} 的会话未授权；请用 /setup telegram 重新登录',
        'verify.user.read_failed': '无法读取账号：{error}',
        'verify.user.is_bot': '这个会话属于机器人；下载需要真人账号',
        'verify.user.ok': '{account}，来自 {source}，已授权{premium}',
        'verify.user.premium': '，Telegram Premium',
        'verify.source.in_chat': '聊天内登录（in-chat login）',
        'verify.accounts.need_both': '需要两个账号都可用',
        'verify.accounts.same': '机器人和读取账号是同一个账号',
        'verify.accounts.ok': '机器人 {bot} 通过账号 {user} 读取',
        'verify.history.failed': '无法列出会话：{error}',
        'verify.history.empty': '这个账号没有任何聊天，无处可下载',
        'verify.history.ok': '账号可以列出自己的聊天',
        'verify.cache.unset': '未配置；每次请求都会重新下载（设置 CACHE_CHAT_ID）',
        'verify.cache.cannot_see': (
            '机器人看不到聊天 {chat}：{error}。请把机器人加为该聊天的管理员。'
        ),
        'verify.cache.not_admin': (
            '机器人在聊天 {chat} 里但不是管理员；可能无法发帖或读回上传的文件'
        ),
        'verify.cache.ok': '机器人是聊天 {chat} 的管理员',
        'verify.cache.no_bot': '没有可用的机器人，无法检查',
        'verify.forward.bot_reads': '机器人自己读取，可转发的文件直接重发',
        'verify.forward.no_cache': (
            '没有缓存频道，可转发的文件也要下载再上传。在私有频道里发送 /cache 即可秒传。'
        ),
        'verify.forward.cannot_see': (
            '读取账号看不到缓存频道 {chat}（{error}）。请把它加入频道，并给予发帖权限。'
        ),
        'verify.forward.ok': '读取账号可以转发到 {chat}',
        'verify.forward.cannot_post': (
            '读取账号在 {chat} 里但不能发帖；请把它设为有发帖权限的管理员'
        ),
        'verify.pikpak.ok': '{username} 已登录，已用 {used} / {limit}',
        'verify.pikpak.folder': '转存到 {folder}',
        'verify.pikpak.no_shared': '未配置共享账号；由用户各自连接自己的账号',
        'verify.login.disabled': '已关闭（pikpak.allow_user_login）',
        'verify.login.needs_https': (
            'Mini App 需要 HTTP_ENABLED=true 和 HTTPS 的 PUBLIC_BASE_URL；在此之前用户用 /setup pik'
            'pak 连接'
        ),
        'verify.login.ok': '/pikpak login 会在 Telegram 里打开 {url}/pikpak/app',
        'verify.http.disabled': '已关闭；磁力和 URL 转存照常，Telegram 媒体无法送到 PikPak',
        'verify.http.bind_failed': (
            '无法绑定 {host}:{port}：{error}。如果机器人正在运行，端口被占用是正常的。'
        ),
        'verify.http.bound': '已绑定 {host}:{port}',
        'verify.reach.ok': '{url} 有应答，PikPak 可以拉取文件',
        'verify.reach.status': '{url} 返回 HTTP {status}；请检查反向代理',
        'verify.reach.failed': (
            '无法从本机访问 {url}（{error}）。请从外网核实；NAT 回环常导致自测失败。'
        ),
        'verify.db.ok': '已打开 {path}',
        'verify.db.failed': '无法打开 {path}：{error}',
        'verify.live.user_missing': '未配置；只能读取机器人自己所在的聊天',
        'verify.live.user_ok': '{account}，已授权',
        'verify.live.source_own': '你自己的账号',
        'verify.live.source_shared': '共享账号（{username}）',
        'verify.live.no_pikpak': '你没有连接账号，服务器上也没有配置；请用 /pikpak login',
        'verify.live.pikpak_ok': '{source}，已用 {used} / {limit}',
        'verify.live.pikpak_failed': '{source}：{error}',
        'verify.live.login_ok': '/pikpak login 会打开 Mini App',
        'verify.live.login_none': '没有 Mini App（{reason}）；可以用 /setup pikpak',
        'verify.live.http_ok': '服务地址 {url}',
        'verify.live.no_url': '未设置公网地址',
        'verify.live.http_disabled': '已关闭；Telegram 媒体无法转存到 PikPak',
        'verify.cli.config_error': '配置错误：{error}',
        'verify.cli.start': '正在检查 tg_media_down_bot 的配置…',
        'portal.title': '连接 PikPak',
        'portal.heading': '连接你的 PikPak 账号',
        'portal.sub': '这个页面属于你自己的媒体下载机器人，不是由 PikPak 运营的。',
        'portal.username': 'PikPak 邮箱或手机号',
        'portal.password': 'PikPak 密码',
        'portal.connect': '连接',
        'portal.note': (
            'Telegram 会告诉我你是谁，所以没有可泄露的登录链接。你的密码只发给 PikPak 一次，用来换'
            '取访问令牌；只保存令牌。随时可以用 {command} 断开。'
        ),
        'portal.js.connecting': '连接中…',
        'portal.js.connected': 'PikPak 已连接',
        'portal.js.connected_sub': '之后的转存会进入你自己的 PikPak 账号。',
        'portal.js.failed': '没有成功。',
        'portal.js.unreachable': '连不上机器人：{error}',
        'portal.err.disabled': '已关闭按用户登录 PikPak。',
        'portal.err.malformed': '请求格式不对。',
        'portal.err.identity': 'Telegram 无法确认你的身份。请从机器人里重新打开这个页面。',
        'portal.err.not_allowed': '你无权使用这个机器人。',
        'portal.err.throttled': '尝试次数太多，请等几分钟。',
        'portal.err.fields': '请把两项都填上。',
        'portal.reason.disabled': '运营者关闭了按用户登录 PikPak（pikpak.allow_user_login）',
        'portal.reason.no_http': (
            '机器人的 HTTP 服务没有以公网地址运行，登录表单无处可放。请设置 HTTP_ENABLED=true 和 PU'
            'BLIC_BASE_URL。'
        ),
        'portal.reason.not_attached': '登录表单没有挂到 HTTP 服务上',
        'portal.reason.plain_http': (
            'PUBLIC_BASE_URL 是 {url}，这是明文 HTTP。Telegram 只通过 HTTPS 打开 Mini App；请在机器'
            '人前面加一个 TLS 反向代理。'
        ),
        'err.session.not_a_session': '这不是 Telegram 会话字符串：{error}',
        'err.session.not_authorized': '新会话未授权',
    },
}
