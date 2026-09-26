# References and reuse

Where the ideas in this project came from, what is actually reused as code, and
what is worth reading if you want to extend it.

Browse the fields directly:

- <https://github.com/topics/pikpak> — the PikPak ecosystem, a few dozen
  repositories. Small enough to read most of it in an afternoon.
- <https://github.com/topics/telegram> — the Telegram ecosystem, tens of
  thousands of repositories. Useful for libraries and conventions, not for
  browsing end to end.

## Reused as code

Two dependencies, both doing work that would be foolish to rewrite.

| Project | Role here |
| --- | --- |
| [LonamiWebs/Telethon](https://github.com/LonamiWebs/Telethon) (Python) | the MTProto client. Both of this bot's clients are Telethon, which is what allows reading save-restricted chats and uploading up to 2 GiB. |
| [Quan666/PikPakAPI](https://github.com/Quan666/PikPakAPI) (Python) | the PikPak API. `tgmd/pikpak.py` wraps it; `tests/test_pikpak.py` pins the method signatures it depends on. |

Reading `PikPakAPI` is also how the project learned that PikPak has **no upload
endpoint**, which is the single fact that shapes all of `tgmd/webserver.py`.
Its `to_dict()` serialising the password in clear text is why
`tgmd/pikpak.py` strips credentials before persisting.

## Closest prior art

These do overlapping jobs. None is forked; the feature set and the phrasing of
several bot commands follow them.

| Project | What to take from it |
| --- | --- |
| [krau/SaveAny-Bot](https://github.com/krau/SaveAny-Bot) (Go) | the most complete thing in this space: forward a Telegram file, save it to Alist, WebDAV, S3, Rclone or disk. Its **pluggable storage backends** are the obvious next step for this project, where PikPak is currently hard-wired as the one cloud target. |
| [iyear/tdl](https://github.com/iyear/tdl) (Go) | a downloader toolkit rather than a bot. Worth reading for chunked, resumable, parallel downloads and for how it exports chat history. |
| [tangyoha/telegram_media_downloader](https://github.com/tangyoha/telegram_media_downloader) (Python) | the closest Python relative, with a web progress UI and bot commands. Pyrogram-based rather than Telethon. |
| [Dineshkarthik/telegram_media_downloader](https://github.com/Dineshkarthik/telegram_media_downloader) (Python) | the original config-file batch downloader the above forked. Simple and readable. |
| [anasty17/mirror-leech-telegram-bot](https://github.com/anasty17/mirror-leech-telegram-bot) (Python) | mirror-to-cloud with queues, per-user settings and progress messages. The reference for queue and progress-reporting behaviour. |
| [CodeXBotz/File-Sharing-Bot](https://github.com/CodeXBotz/File-Sharing-Bot) (Python) | where the **cache channel** idea comes from: store uploads in a private channel so a file requested twice is re-sent rather than re-fetched. `CACHE_CHAT_ID` is this. |
| [AnonymousV73X/PIKPAK-TO-GOOGLE-DRIVE-TELEGRAM-BOT](https://github.com/AnonymousV73X/PIKPAK-TO-GOOGLE-DRIVE-TELEGRAM-BOT) (Python) | PikPak out to Google Drive, the opposite direction to this bot. |
| [akynazh/tg-search-bot](https://github.com/akynazh/tg-search-bot) (Python) | search Telegram and auto-save results, including to PikPak. Interesting for the auto-save trigger, not the search. |

## PikPak's own bot

PikPak runs [@PikPak_Bot](https://t.me/PikPak_Bot), linked from **My → Connect
Telegram Bot** in the PikPak app. Forward it a magnet link, a direct URL or a
file and it lands in your drive.

**It cannot be cloned or reused.** It is closed, runs on PikPak's servers, and
holds credentials no third party can have. What can be reused is its
interaction design, which is genuinely good: connect once, then forward and
forget, with no commands to learn.

Use it instead of this project if forwarding is all you need. What it cannot do
is why this exists:

- read a channel that blocks saving, since such a message cannot be forwarded
  to it at all;
- fetch a message by link from a private channel you are in;
- hand the file back to you in Telegram, or keep it on your own disk;
- work from a bare link with no forwarding, including ranges and album links.

All of those need a real Telegram account rather than a bot account, which is
what `/setup telegram` connects.

## Worth knowing about, not used here

| Project | Why it is interesting |
| --- | --- |
| [ykxVK8yL5L/pikpak-webdav](https://github.com/ykxVK8yL5L/pikpak-webdav) and [VGEAREN/pikpak-webdav](https://github.com/VGEAREN/pikpak-webdav) | expose PikPak as a WebDAV share, so players and file managers mount it directly. A neat pairing with this bot: transfer here, mount there. |
| [Bengerthelorf/pikpaktui](https://github.com/Bengerthelorf/pikpaktui) (Rust) | a terminal client for PikPak. Useful as a second, independent reading of the API's behaviour. |
| [bharathganji/pikpak-plus](https://github.com/bharathganji/pikpak-plus) | an unofficial PikPak web app. Shows what the API supports beyond offline download. |
| [lyqingye/pikpak-go](https://github.com/lyqingye/pikpak-go), [Muione/PikpakAPI](https://github.com/Muione/PikpakAPI) (TypeScript) | PikPak SDKs in other languages, handy for cross-checking an endpoint when the Python client is ambiguous. |
| [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot), [telegraf](https://github.com/telegraf/telegraf), [pyTelegramBotAPI](https://github.com/eternnoir/pyTelegramBotAPI) | Bot API wrappers. Deliberately **not** used: the HTTP Bot API caps uploads at 50 MiB and cannot read arbitrary history, which rules out this project's whole purpose. |
| [tdlib/td](https://github.com/tdlib/td) (C++) | the official client library. The alternative to Telethon if this were ever rewritten for throughput. |

## If you extend this

The gap worth closing first is the one SaveAny-Bot already covers: make the
cloud destination pluggable instead of PikPak-only. `tgmd/delivery.py` is the
seam. A WebDAV or Rclone backend would not need the HTTP file server at all,
because unlike PikPak those accept an upload.
