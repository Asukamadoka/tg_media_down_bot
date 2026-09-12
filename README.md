# tg_media_down_bot

A Telegram bot that turns a message link into the file behind it. Send it any
Telegram link and it fetches the media, then sends it back to you, saves it on
the server, or transfers it into PikPak.

It also takes magnet links, direct URLs and PikPak share links, which go
straight into PikPak without passing through this machine.

## What it does

| You send | It does |
| --- | --- |
| `https://t.me/channel/123` | downloads the media from that post |
| `https://t.me/c/1234567890/123` | same, for a private chat you are in |
| `https://t.me/channel/12/123` | a message inside a forum topic |
| `https://t.me/channel/100-120` | every message in that range |
| `https://t.me/channel/123?single` | one album item instead of the whole album |
| `https://t.me/channel/123?comment=45` | a comment under that post |
| `magnet:?xt=urn:btih:...` | queues an offline download in PikPak |
| `https://example.com/video.mp4` | PikPak fetches the URL itself |
| `https://mypikpak.com/s/...` | saves that share into your PikPak drive |
| media sent or forwarded to the bot | saves it to disk or to PikPak |

Several links in one message are all queued. Albums are expanded
automatically, so a link to one photo of a set returns the set.

## Why a user account is required

A bot account cannot read the history of a chat it is not in, and cannot read
chats that restrict saving content at all. So downloads run through a real
Telegram account, and the bot account is only used to talk to you.

That means two credentials: an API ID and hash for the account, and a bot
token from [@BotFather](https://t.me/BotFather). The account has to be a member
of any private chat you want to pull from.

Because the bot reads everything your account can see, **access is closed by
default**. Only ids in `ADMIN_USER_IDS` and `ALLOWED_USER_IDS` may use it.

## Setup

```bash
git clone https://github.com/Asukamadoka/tg_media_down_bot
cd tg_media_down_bot
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

Fill in `.env`:

1. `TG_API_ID` and `TG_API_HASH` from <https://my.telegram.org/apps>.
2. `TG_BOT_TOKEN` from [@BotFather](https://t.me/BotFather).
3. `ADMIN_USER_IDS` — your numeric id. Send `/id` to the bot if you do not
   know it; it answers that command to anyone.
4. `TG_USER_SESSION` — generate it once:

```bash
python -m tgmd.login
```

That signs in the reading account and prints a session string. It is a
credential equivalent to the account password, so keep it out of version
control; revoke it under Telegram → Settings → Devices if it leaks.

Then start the bot:

```bash
python -m tgmd
```

### Docker

```bash
cp .env.example .env    # fill it in first, including TG_USER_SESSION
docker compose up -d
```

State lives in `./data` (sessions, the SQLite database, downloads), so
rebuilding the image loses nothing.

## Destinations

`/mode` picks where files go, per user:

- **`telegram`** (default) — the bot uploads the file back to you. It uses
  MTProto rather than the HTTP Bot API, so the ceiling is 2 GiB rather than
  50 MiB. Anything larger stays on disk instead, and the bot says so.
- **`local`** — the file stays on the server under `DOWNLOAD_DIR` and the bot
  replies with the path. Useful when the server is also your media host.
- **`pikpak`** — the file is transferred into PikPak. See below.

## PikPak

PikPak's API has **no upload endpoint**. The only way to put a file into it is
to give it a URL to fetch. That splits PikPak support in two:

**Magnet links, direct URLs and share links need nothing extra.** They are
handed to PikPak, which downloads them on its own infrastructure. Set the
credentials and it works:

```
PIKPAK_USERNAME=you@example.com
PIKPAK_PASSWORD=...
PIKPAK_FOLDER=/TelegramMedia
```

**Telegram media needs the built-in HTTP server.** The bot downloads the file,
serves it at a signed, expiring URL, and asks PikPak to pull it from there. So
PikPak has to be able to reach this host:

```
HTTP_ENABLED=true
HTTP_PORT=8080
PUBLIC_BASE_URL=https://media.example.com
```

Put a TLS reverse proxy in front of it. URLs carry an HMAC over the file id
and an expiry, so only the exact link the bot generated works, and only until
it expires (`http.url_ttl`, one hour by default).

Without this, `/mode pikpak` still works for magnets and URLs and says clearly
why a Telegram file cannot be transferred.

`/pikpak` shows quota and the target folder; `/pikpak dir /Movies/Anime`
changes where your transfers land.

## Commands

| Command | Purpose |
| --- | --- |
| `/help` | what the bot understands |
| `/mode [telegram\|local\|pikpak]` | show or set your destination |
| `/status` | jobs currently queued or running |
| `/cancel [id]` | cancel one job, or all of yours |
| `/stats` | your recent jobs and total transferred |
| `/pikpak [dir <path>]` | PikPak quota and target folder |
| `/id` | your user id and the current chat id |

Progress is reported in a single message that is edited as the transfer runs,
throttled to one edit every `progress_interval` seconds so Telegram does not
rate-limit the bot.

## Configuration

Every setting can come from `config.yaml` (copy `config.example.yaml`) or from
the environment, and **the environment wins**. Secrets belong in `.env`.

The settings worth knowing about:

| Setting | Default | Meaning |
| --- | --- | --- |
| `download.concurrent` | 2 | parallel downloads across all users |
| `download.max_batch` | 50 | cap on messages expanded from one range link |
| `download.max_queue_per_user` | 20 | per-user queue limit |
| `download.filename_template` | `{chat}/{message_id}_{name}` | layout under `DOWNLOAD_DIR` |
| `download.delete_after_delivery` | true | remove the local copy once delivered |
| `download.auto_join_invites` | false | join `t.me/+hash` links automatically |
| `delivery.max_upload_size_mb` | 2000 | above this, keep the file locally |
| `delivery.cache_chat_id` | none | channel used to avoid re-uploading |
| `access.allow_all_users` | false | open the bot to everyone |

Template fields: `chat`, `chat_id`, `message_id`, `topic_id`, `name`, `stem`,
`ext`, `date`. An unknown field is rejected at startup rather than at the
moment a download finishes.

### Upload cache

Set `CACHE_CHAT_ID` to a channel the bot is an administrator of. Each file the
bot uploads is also stored there, keyed by its source message, so the same
link requested twice is re-sent from Telegram's own servers instead of being
downloaded and uploaded again.

## Limitations

- The reading account must be a member of any private chat you link to. The
  bot cannot join on your behalf unless `auto_join_invites` is on, and even
  then only for invite links.
- Files above 2 GiB cannot be re-uploaded by a bot. They are kept on disk.
- Telegram rate-limits aggressively. The bot honours flood waits rather than
  pushing through them, so a large batch is deliberately not fast.
- `download.max_batch` caps range links. A wider range is truncated and the
  bot reports it.
- Bot messages are in English.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite covers link parsing, filename and path building, URL signing,
configuration precedence and validation, the database layer, media
inspection, and the HTTP file server over a real socket, including its
rejection paths. It also asserts that the `pikpakapi` methods this project
calls still take the arguments it passes, so a dependency upgrade fails in
tests rather than in production. No Telegram credentials are needed.

### Layout

| Module | Responsibility |
| --- | --- |
| `links.py` | parsing every accepted link form (pure) |
| `resolver.py` | link → Telegram messages, invites, albums, comments |
| `downloader.py` | media inspection and download with progress |
| `delivery.py` | sending to Telegram, disk or PikPak |
| `tasks.py` | the job queue and per-job orchestration |
| `handlers.py` | bot commands and dispatch |
| `pikpak.py` | PikPak session, offline downloads, share restore |
| `webserver.py` | signed URLs so PikPak can fetch local files |
| `config.py` | YAML + environment configuration |
| `db.py` | SQLite: preferences, upload cache, job history |

## Prior art

The feature set follows
[tangyoha/telegram_media_downloader](https://github.com/tangyoha/telegram_media_downloader)
and [Dineshkarthik/telegram_media_downloader](https://github.com/Dineshkarthik/telegram_media_downloader),
with the bot-first interface of
[CodeXBotz/File-Sharing-Bot](https://github.com/CodeXBotz/File-Sharing-Bot)
and the cloud-transfer idea from
[anasty17/mirror-leech-telegram-bot](https://github.com/anasty17/mirror-leech-telegram-bot).
This is an independent implementation on Telethon rather than a fork.
