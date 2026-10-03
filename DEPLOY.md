# Deploy

The honest shape of this: no link can create a running Telegram bot. A bot is
a process that has to live somewhere, Telegram does not host it, and only you
can create its token. So deployment is three steps, and the third one happens
entirely inside Telegram.

| Step | Where | What |
| --- | --- | --- |
| 1 | Telegram | create the bot, get its token |
| 2 | a host | deploy this repo with three values |
| 3 | Telegram | `/claim`, then `/setup` for the account and PikPak |

Step 2 is the only part that cannot be done in the Telegram app, because the
values have to reach the process before it can start. Everything else is
either automatic or a command you send in a chat.

What the bot does for itself, so you do not have to:

| Chore | How it is avoided |
| --- | --- |
| `/setcommands` in BotFather | written over the API at startup |
| bot description and about text | same |
| finding your own user id | `/claim <code>` from the log |
| finding a channel's `-100…` id | post `/cache` in the channel |
| the public URL and port | read from the platform's own environment |
| generating a session string | `/setup telegram` signs in, in chat |

Only `/setprivacy` is left in BotFather, because Telegram exposes no API for
it, and it is optional.

## Step 1 — the three values you need

Collect these first. Paste them into your host's environment-variable form,
never into a chat.

Three, not four: the admin id used to be a variable, which meant deploying,
sending `/id`, reading the number back and deploying again. The bot now prints
a claim code to its own log, and `/claim <code>` makes you the admin with no
second deploy. Same for the upload cache: post `/cache` in the channel and the
bot takes the id from there.

### `TG_API_ID` and `TG_API_HASH`

Open <https://my.telegram.org/apps> and sign in with the phone number of the
account that will read chats. Fill in any app name. The page then shows
**App api_id** and **App api_hash**.

These identify the client software, not the account. They cannot be skipped:
the bot speaks MTProto, which requires them, and that is also what lifts the
upload ceiling from 50 MiB to 2 GiB.

### `TG_BOT_TOKEN`

Open [@BotFather](https://t.me/BotFather) and send `/newbot`. Give it a
display name, then a username ending in `bot`. It replies with a token shaped
like `123456789:AAHfiqks…`.

Keep the whole thing, colon included. The digits before the colon are the
bot's own user id, and the bot checks them against the account that answers,
so a token from the wrong bot is caught at startup rather than later.

While you are there, `/setprivacy` → **Disable** lets the bot see links posted
in groups it is in. Skip it if you only ever message the bot directly.

### `ADMIN_USER_IDS` — optional, skip it

Leave it blank. On startup with no admin the bot writes a block like this to
its log:

```
====================================================================
  NO ADMIN YET. Open @your_bot in Telegram and send:

      /claim 5isJppLOdTc

  That makes you the admin. No redeploy needed, and this code stops
  working straight afterwards.
====================================================================
```

You are already looking at that log, having just deployed. Send the command
and you own the bot, stored in the database so it survives restarts.

The code is required rather than trusting whoever messages first, because bot
usernames are searchable: first-sender-wins would hand the bot to whoever
found it. It is single-use and stops working the moment an admin exists.

This is a real gate, not a formality: the bot reads whatever the connected
account can see, so it refuses everyone who is not an admin or on
`ALLOWED_USER_IDS`.

## Step 2 — deploy

Any host that can run a Docker image works. The repository ships `Dockerfile`,
`render.yaml` and `app.json`.

**Render** builds from `render.yaml`, prompts for the four values above, and
mounts a persistent disk at `/data`:

```
https://render.com/deploy?repo=https://github.com/Asukamadoka/tg_media_down_bot
```

**Koyeb** takes the repository and environment in the URL:

```
https://app.koyeb.com/deploy?type=git&repository=github.com/Asukamadoka/tg_media_down_bot&branch=main&name=tg-media-down-bot&ports=8080;http;/&env[HTTP_ENABLED]=true
```

**Railway** needs a published template, which only the repository owner can
create: open <https://railway.com/new>, point it at this repository, and add
the variables by hand.

I could not reach `render.com` or `koyeb.com` from the sandbox this was built
in, so treat those two URLs as the documented format rather than as verified
links. If either is rejected, use the host's normal "new service from a Git
repository" flow and paste the same variables; nothing about the project
depends on the button.

### Persistent storage matters

Put the state directory on a disk that survives redeploys. The image already
points at `/data`:

```
SESSION_DIR=/data/sessions
DATA_DIR=/data/db
DOWNLOAD_DIR=/data/downloads
```

Without a persistent `/data`, every redeploy wipes the SQLite database, which
loses the reading-account session created by `/setup telegram` and every
connected PikPak account. The bot still works, but everyone has to sign in
again.

### Deploy as a web service, not a worker

Only a web service gets a public HTTPS address, and that address is what
PikPak fetches files from and what the Telegram Mini App login page is served
on. The bot reads the platform's `PORT` and public URL by itself, so you do
not have to set `HTTP_PORT` or `PUBLIC_BASE_URL` on Render, Koyeb, Railway or
Fly. On anything else, set `PUBLIC_BASE_URL` to your HTTPS address.

If you only ever want files sent back through Telegram or kept on disk, a
worker is fine and no HTTP settings are needed at all.

### Plain Docker

```bash
git clone https://github.com/Asukamadoka/tg_media_down_bot
cd tg_media_down_bot
cp .env.example .env      # fill in the four values
docker compose up -d
```

## Step 3 — finish inside Telegram

First, `/claim <code>` with the code from the log, unless you set
`ADMIN_USER_IDS` yourself. Until that happens the bot refuses everyone,
including you, and says so with the instructions.

Then send `/setup`. It shows a checklist and fills it in without you touching
the host again:

```
✅ Bot account      — connected, you are talking to it
⬜ Reading account  — /setup telegram to sign in here
⬜ PikPak           — /setup pikpak to sign in here
⬜ Upload cache     — optional
```

**`/setup telegram`** signs a normal Telegram account in to the bot, which is
what lets it read private channels and channels that block saving. It asks
for the phone number, then the login code Telegram sends to that account, then
the two-step password if there is one. Each of those messages is deleted as
soon as it is read, the session is stored on your server, and it is brought
into service immediately, with no restart.

It is admin-only, and the prompt says plainly why it is safe here and nowhere
else: a bot asking for a Telegram login code is the shape of the commonest
account-theft scam on the platform. It is legitimate in this one case because
you own both the bot and the account. Never give a login code to a bot someone
else runs.

**`/setup pikpak`** asks for the PikPak email and password in chat, deletes
both, and keeps only the access token. If the HTTP server is running on HTTPS,
`/pikpak login` is nicer: it opens the same form as a Mini App inside the
Telegram app, where Telegram signs your identity so there is no link to leak.

**The upload cache** is optional and needs no id hunting. Create a private
channel, add the bot as an administrator, then post `/cache` **in that
channel**. The bot checks it really can post there, takes the id from the
message itself, and stores it. A link requested twice is then re-sent from
Telegram's servers instead of being downloaded again. `/cache off` disables
it.

Then run `/verify` to confirm the whole thing, or `python -m tgmd.verify` on
the host for the same report with an exit code.

## Do you even need this bot?

PikPak runs its own Telegram bot, [@PikPak_Bot](https://t.me/PikPak_Bot),
reachable from **My → Connect Telegram Bot** in the PikPak app. Forward it a
magnet link, a direct URL or a message and it saves the file to your drive.

If that is all you want, use it. It is less work than hosting anything, and it
cannot be cloned in any case: it is closed, server-side, and tied to PikPak's
own infrastructure.

What it cannot do is the reason this project exists:

- read a channel that blocks saving, since such a message cannot be forwarded
  to it at all;
- fetch a message by link from a private channel you are in;
- send the file back to you in Telegram, or keep it on your own disk;
- work from a link alone, with no forwarding.

Those all need a real Telegram account rather than a bot account, which is
what `/setup telegram` connects.

[REFERENCES.md](REFERENCES.md) compares the alternatives in more detail,
including several that may suit you better than either.
