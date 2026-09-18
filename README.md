# tg_media_down_bot

A Telegram bot that turns a message link into the file behind it. Send it any
Telegram link and it fetches the media, then sends it back to you, saves it on
the server, or transfers it into PikPak.

It also takes magnet links, direct URLs and PikPak share links, which go
straight into PikPak without passing through this machine.

**Deploying it?** [DEPLOY.md](DEPLOY.md) is the guided path: the four values
you need and where each comes from, one-click deploy for a few hosts, and then
`/setup` inside Telegram for everything else. No link can create a running
bot, but that is the only part that happens outside the Telegram app.

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

### 1. Create the bot

Open [@BotFather](https://t.me/BotFather) and send `/newbot`. It asks for a
display name, then a username ending in `bot`. It replies with a token that
looks like `123456789:AAHfiqks…`. Put that in `TG_BOT_TOKEN`.

The digits before the colon are the bot's own Telegram user id. The setup
check below uses that: it compares them against the account that actually
answers, so a token pasted from the wrong bot is caught rather than quietly
producing a bot that talks to the wrong chats.

While you are in BotFather, `/setprivacy` → Disable is worth doing if you
plan to use the bot inside a group, otherwise it only sees commands.

### 2. Fill in the credentials

1. `TG_API_ID` and `TG_API_HASH` from <https://my.telegram.org/apps>.
2. `TG_BOT_TOKEN` from the step above.

That is everything the process needs to start. Who the admin is, the reading
account, PikPak and the upload cache are all established afterwards from
inside Telegram, so none of them is an environment variable unless you want
it to be.

On first start with no admin the bot writes a claim code to its log; sending
`/claim <code>` makes you the admin, stored in the database. Setting
`ADMIN_USER_IDS` skips that step if you prefer.

If you would rather pin the reading account in the environment, generate a
session string once:

```bash
python -m tgmd.login
```

Put it in `TG_USER_SESSION`. It is a credential equivalent to the account
password, so keep it out of version control; revoke it under Telegram →
Settings → Devices if it leaks. Setting it also disables the in-chat login,
and `/setup` says so rather than appearing to work.

### 3. Verify before starting

```bash
python -m tgmd.verify
```

This is the confirmation step. It does not guess from the configuration file;
it connects and checks:

```
✓ configuration        loaded and internally consistent
✓ access control       1 admin(s), 0 additional user(s)
✓ bot token            well-formed, names bot id 123456789
✓ directories          downloads=downloads, data=data, sessions=sessions
✓ database             opened data/tgmd.sqlite3
✓ bot created          @my_media_bot (id 123456789) — https://t.me/my_media_bot
✓ bot identity         id 123456789 matches the token, and is a bot
✓ user session         @myaccount (id 987654321) from TG_USER_SESSION, authorised
✓ account separation   bot 123456789 reads through account 987654321
✓ history access       the account can list its chats
✓ cache chat           the bot administrates chat -1001234567890
✓ pikpak account       you@example.com signed in, 4.7 GiB of 10.0 TiB used
✓ pikpak login links   /pikpak login will issue https://media.example.com/…
✓ http server          bound 0.0.0.0:8080
✓ public reachability  https://media.example.com/healthz answers

everything passed
```

It exits non-zero if anything failed, so it drops straight into a deployment
script. Warnings do not fail the run. Run it while the bot is stopped, so the
two processes do not contend for a session file.

Admins can run the same identity checks in chat at any time with `/verify`.

Note that MTProto is not plain HTTPS. The host needs to open a direct TCP
connection to Telegram; an HTTPS-only egress proxy will block the bot even
though the token is fine, and the check says so when that happens.

### 4. Start it

```bash
python -m tgmd
```

### 5. Finish inside Telegram

Open the bot and send `/setup`. It shows what is done and what is left:

```
✅ Bot account      — connected, you are talking to it
⬜ Reading account  — /setup telegram to sign in here
⬜ PikPak           — /setup pikpak to sign in here
⬜ Upload cache     — optional
```

`/setup telegram` signs a normal account in to the bot, which is what lets it
read private channels and channels that block saving. It asks for the phone
number, then the login code Telegram sends, then the two-step password if the
account has one. Each message is deleted as it is read, and the session is
brought into service immediately with no restart.

It is admin-only, and the prompt says why it is safe here and nowhere else: a
bot asking for a Telegram login code is the shape of the commonest
account-theft scam on the platform, and this is legitimate only because you
own both the bot and the account.

`/setup pikpak` does the same for PikPak, and needs no web server at all.

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
handed to PikPak, which downloads them on its own infrastructure.

**Telegram media needs the built-in HTTP server.** The bot downloads the file,
serves it at a signed, expiring URL, and asks PikPak to pull it from there. So
PikPak has to be able to reach this host:

```
HTTP_ENABLED=true
HTTP_PORT=8080
PUBLIC_BASE_URL=https://media.example.com
```

Put a TLS reverse proxy in front of it. File URLs carry an HMAC over the file
id and an expiry, so only the exact link the bot generated works, and only
until it expires (`http.url_ttl`, one hour by default).

Without this, `/mode pikpak` still works for magnets and URLs and says clearly
why a Telegram file cannot be transferred.

### Connecting an account

Three ways, and they can coexist. The bot offers whichever its deployment
supports, best first.

**A Mini App, inside Telegram.** With the HTTP server on HTTPS,
`/pikpak login` shows a button that opens the form inside the Telegram app.
There is no link at all: Telegram signs who is opening the page, so identity
comes from Telegram rather than from a secret in a URL. This is the nicest
path and the one a one-click deploy gets automatically, because the public
address is read from the platform.

**In chat.** `/setup pikpak` asks for the email and password as ordinary
messages, deletes each one as it reads it, and keeps only the token. It needs
no web server, no public address and no TLS, so it works on any deployment
including a worker with no inbound networking.

**A one-time link.** Where the HTTP server is running but Telegram will not
open it as a Mini App, `/pikpak login` sends a link to a page the bot serves
at `/pikpak/login/<token>`.

However it is done, only the access token is stored, never the password, and
`/pikpak logout` disconnects the account and deletes the token.

The page is deliberately plain and says on its face that it belongs to your
bot and not to PikPak, because a page that asks for someone's credentials
should never look like it came from the service it is asking about.

Four things keep the one-time link from being a liability:

- it is signed, so the user id inside it cannot be swapped for another;
- it works once, and issuing a new one invalidates the previous;
- it expires (`pikpak.login_link_ttl`, 15 minutes by default);
- three wrong passwords burn it, so a leaked link is not a password oracle.

The bot refuses to issue a link at all unless `PUBLIC_BASE_URL` is HTTPS
(loopback is allowed for local development), since the point is to keep the
password off the wire as well as out of the chat transcript. Set
`PIKPAK_ALLOW_USER_LOGIN=false` to turn the feature off.

**Or configure one shared account** for everyone who has not connected their
own:

```
PIKPAK_USERNAME=you@example.com
PIKPAK_PASSWORD=...
PIKPAK_FOLDER=/TelegramMedia
```

Stored sessions never contain a password in either case. The token is kept and
the credentials are discarded as soon as they have been exchanged for one.

`/pikpak` shows which account is in use, the quota and the target folder;
`/pikpak dir /Movies/Anime` changes where your transfers land.

## Commands

| Command | Purpose |
| --- | --- |
| `/help` | what the bot understands |
| `/mode [telegram\|local\|pikpak]` | show or set your destination |
| `/status` | jobs currently queued or running |
| `/cancel [id]` | cancel one job, or all of yours |
| `/stats` | your recent jobs and total transferred |
| `/pikpak` | which account is in use, quota, target folder |
| `/pikpak login` | get a one-time link to connect your own account |
| `/pikpak logout` | disconnect your account and delete the stored token |
| `/pikpak dir <path>` | change where your transfers land |
| `/id` | your user id and the current chat id |
| `/claim <code>` | become the admin of a freshly deployed bot |
| `/setup` | admins only: the setup checklist, and finish it here |
| `/cache` | admins only: use a channel as the upload cache |
| `/verify` | admins only: identity and configuration report |

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
| `pikpak.allow_user_login` | true | users may connect their own account |
| `pikpak.login_link_ttl` | 900 | login link lifetime, seconds |

Template fields: `chat`, `chat_id`, `message_id`, `topic_id`, `name`, `stem`,
`ext`, `date`. An unknown field is rejected at startup rather than at the
moment a download finishes.

### Upload cache

Create a private channel, add the bot as an administrator, then post `/cache`
in that channel. The bot verifies it can post there, takes the id from the
message, and remembers it. Each file it uploads is also stored there, keyed by
its source message, so the same link requested twice is re-sent from
Telegram's own servers instead of being downloaded and uploaded again.

`CACHE_CHAT_ID` does the same thing from the environment and outranks the
runtime choice, for anyone who prefers declaring it.

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

The suite covers link parsing, bot-token parsing, filename and path building,
URL signing, configuration precedence and validation, platform detection for
one-click deploys, the database layer, media inspection, per-user PikPak
session selection, the setup conversations, Mini App signature validation, and
both HTTP surfaces over a real socket: the file server, the one-time login
page and the Mini App endpoint, including expiry, single use, forged tokens,
forged user ids and attempt limits.

Three of those exist to catch dependency drift rather than our own bugs:
the `pikpakapi` methods this project calls are asserted to still take the
arguments it passes; the inline keyboards are serialised, which fails if
Telegram's schema layer moves them again; and a test that accidentally reaches
PikPak over the network fails immediately instead of hanging.

No credentials are needed.

### Layout

| Module | Responsibility |
| --- | --- |
| `links.py` | parsing every accepted link form (pure) |
| `resolver.py` | link → Telegram messages, invites, albums, comments |
| `downloader.py` | media inspection and download with progress |
| `delivery.py` | sending to Telegram, disk or PikPak |
| `tasks.py` | the job queue and per-job orchestration |
| `handlers.py` | bot commands and dispatch |
| `identity.py` | bot-token parsing and the check-report model |
| `verify.py` | the preflight and in-chat verification checks |
| `setup.py` | the in-Telegram setup conversations |
| `miniapp.py` | Telegram Mini App initData validation |
| `buttons.py` | inline keyboards, isolated because they are layer-specific |
| `pikpak.py` | per-user PikPak sessions, transfers, share restore |
| `portal.py` | one-time PikPak login links and their page |
| `webserver.py` | signed URLs so PikPak can fetch local files |
| `config.py` | YAML + environment configuration |
| `db.py` | SQLite: preferences, upload cache, job history, tokens |

## Prior art

[REFERENCES.md](REFERENCES.md) covers this properly: what is reused as code,
the closest prior art and what to take from each, and what PikPak's own
[@PikPak_Bot](https://t.me/PikPak_Bot) already does so you can decide whether
you need this at all.

The short version: an independent implementation on Telethon, not a fork.
The feature set follows the two `telegram_media_downloader` projects, the
cache-channel trick comes from `File-Sharing-Bot`, and the queue and progress
behaviour from `mirror-leech-telegram-bot`. Browse the fields at
[topics/pikpak](https://github.com/topics/pikpak) and
[topics/telegram](https://github.com/topics/telegram).
