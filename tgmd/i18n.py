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
                         '/mode — where files should go: telegram, local or pikpak\n'
                         '/status — what I am working on\n'
                         '/cancel [id] — stop one job, or everything\n'
                         '/stats — your recent jobs\n'
                         '/pikpak — PikPak account, quota and target folder\n'
                         '/pikpak login — connect your own PikPak account\n'
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
                            '<code>/mode pikpak</code> — transfer it into PikPak'
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
                         '/mode — 文件送到哪里：telegram、local 或 pikpak\n'
                         '/status — 我正在处理什么\n'
                         '/cancel [id] — 取消某个任务，或全部取消\n'
                         '/stats — 你最近的任务\n'
                         '/pikpak — PikPak 账号、容量和目标文件夹\n'
                         '/pikpak login — 连接你自己的 PikPak 账号\n'
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
                            '<code>/mode pikpak</code> — 转存进 PikPak'
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
    },
}
