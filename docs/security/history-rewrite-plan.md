# Plan: rewriting the public history (for Saki to decide; nothing here has been run)

Status: **prepared, not executed.** The audit (`2026-10-03-public-audit.md`) found no credential in
history: gitleaks' default rules report four false positives (variable names and an invented hash in
tests), and the project's own rules find addresses, a chat id, share paths and the NAS's make, in
the commits listed there. HEAD has been scrubbed; history has not been touched. Rewriting is only
worth it for what is still harmful once read, and it has a cost (below). The decision is yours.

## What is in history that a rewrite would remove

By category (counts and files are in the audit; values are not written anywhere in this repository):

* LAN addresses of the NAS and the model host, in docs, briefs, tests and one code default
  (`tgmd/traffic/classify.py`, until M9.3);
* `smb://` share URLs, NAS volume paths with the share name, the NAS's make and model;
* the Tailscale Funnel host name, in `CC_BRIEF.md` and `HANDOFF.md`;
* the cache channel's Telegram chat id, in a doc and two tests.

None of these opens anything by itself. The Funnel host is a public endpoint by design, so it is the
only one that matters beyond privacy.

## The replace-text file

Patterns only. Save as `expressions.txt` (git-filter-repo's `regex:` lines, Python syntax):

```
regex:(?<![\d.])10\.(?!0\.0\.)\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])==><LAN_IP>
regex:(?<![\d.])192\.168\.(?!0\.)\d{1,3}\.\d{1,3}(?![\d.])==><LAN_IP>
regex:(?<![\d.])172\.(?:1[7-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}(?![\d.])==><LAN_IP>
regex:\b[a-z0-9-]+\.tail[0-9a-f]+\.ts\.net\b==><FUNNEL_HOST>
regex:smb://(?!<)[^\s"'<>`/]+/==>smb://<NAS_IP>/
regex:/[Vv]olume[0-9]+/[^\s"'<>`]*==><NAS_VOLUME>
regex:(?<!\d)-100(?!1234567890|9876543210|9999999999)\d{9,}(?!\d)==><CACHE_CHAT_ID>
regex:\b<NAS_MODEL>(?: DXP[0-9A-Za-z]+)?\b==><NAS_MODEL>
regex:\bDXP[0-9]{3,4}[0-9A-Za-z]*\b==><NAS_MODEL>
regex:\b<NAS_HOSTNAME>\b==><NAS_HOSTNAME>
```

It leaves the invented test values (`10.0.0.x`, `192.168.0.x`, `-1001234567890`) alone. Commit
messages are separate: `--replace-message` takes the same file (the audit does not read them; run
`git log --all --format=%B | gitleaks stdin` first to see whether there is anything to replace).

## Steps

1. **Freeze.** Tell anyone with a clone (Cowork, other machines) to stop pushing.
2. **Back up**: `git clone --mirror git@github.com:Asukamadoka/tg_media_down_bot.git backup.git`.
3. **Rewrite a fresh clone** (filter-repo refuses a used one):
   ```
   git clone git@github.com:Asukamadoka/tg_media_down_bot.git clean && cd clean
   git filter-repo --replace-text ../expressions.txt --replace-message ../expressions.txt
   ```
   Refs: it rewrites every branch and tag. `origin` is removed; add it back.
4. **Verify before pushing**:
   ```
   gitleaks git . --config .gitleaks.toml --redact           # expect: no leaks
   python scripts/public_audit.py --fail-on-head             # expect: nothing in history
   python -m pytest -q && ruff check .
   ```
   (the audit script's history half reports findings by file and commit, so a remaining one is
   easy to chase.)
5. **Force-push**: `git remote add origin …; git push --force --all; git push --force --tags`.
   The production branch is `claude/telegram-media-downloader-bot-samm1v` and has no protection
   that forbids it today; if you turned protection on, lift it for the push and restore it.
6. **Ask GitHub to drop what the rewrite cannot reach.** Old commits stay fetchable by hash, and
   pull requests keep their own refs (`refs/pull/1/*`): close and delete what you can, then open a
   support request ("remove cached views and unreachable commits") naming the repository. Forks
   and clones that already exist keep the old history; nothing here can change that.
7. **The image** (below), then **the clones**: `git fetch && git reset --hard origin/<branch>` on
   every machine that has one (Cowork's checkout, the Mac).
8. **Re-run the audit** (`scripts/public_audit.py --md …`) and replace the audit file.

## What a rewrite breaks

* **Commit hashes written in documents**: `HANDOFF.md`, the briefs and `CC_BRIEF.md` quote hashes
  (`dd947d2`, `c19d658`, …). After a rewrite they point at nothing. Either leave them (they are
  history, the dates still say when) or add a table old → new from `git filter-repo`'s
  `commit-map` file; a scripted pass over `docs/` can rewrite them.
* **Open clones and unpushed branches** diverge completely: they must be re-cloned or reset, and
  work on them rebased by hand.
* **GitHub Actions run links** in `HANDOFF.md` stay valid but show the old SHAs as unreachable.
* **GHCR image labels.** Every image carries its commit as a label; after the rewrite the old
  images name commits that no longer exist. The new build gets new labels, so this only matters for
  the old ones.
* **Pull requests and issues** keep the text they were written with; the rewrite does not touch
  them. Anything sensitive typed there is not removed by this plan.

## The image

The public GHCR image holds the code as it was at each build: only `tgmd/`, `pikpak_wms/`,
`config/*.example.yaml` and `tests/nl/` (the documents and briefs were never in it), but the old
code, including the address that used to be the traffic classifier's default (until M9.3), is in
every old version. Rewriting the repository does **not** change them. To be consistent:

1. Build and push the scrubbed image (every push to the production branch does).
2. Delete the old package versions in GitHub → Packages → `tg_media_down_bot` → versions (keep the
   one the NAS runs until it has pulled the new one), or `gh api -X DELETE
   /user/packages/container/tg_media_down_bot/versions/<id>` for each.
3. Then pull on the NAS and restart.

## What to rotate, whether or not the history is rewritten

Anything found in public history must be treated as seen. The audit found **no token, key, session
string, password or subscription link** in history, so nothing here is *known* to have leaked. Rotate
these only if, to your knowledge, they were ever written into a commit, an issue, a pull request, a
log pasted into one, or a screenshot:

| Credential | How |
|---|---|
| Telegram bot token | BotFather → `/revoke`; put the new one in the NAS's `.env`, restart |
| Telegram `api_hash` | cannot be rotated; create a new application at my.telegram.org, change `API_ID` / `API_HASH` and log in again with `/setup` |
| Telegram reader-account session | terminate the session in Telegram → Settings → Devices, log in again |
| PikPak password / tokens | change the password in PikPak; reconnect with `/pikpak` |
| Proxy subscription link | regenerate it at the provider; update the mihomo config on the NAS |
| GitHub tokens (PATs, Actions secrets) | revoke and reissue |
| Anthropic / other model API keys | revoke at the provider, set the new one in `.env` |
| The signing secret for PikPak fetch links | generated by the bot and kept in its database (`url_signing_secret`); it was never in git. Delete that row and restart to get a new one |

What the audit did find, and what to do about it regardless of a rewrite:

* **The Tailscale Funnel host name is public.** Funnel is reachable from the internet by design; the
  host name only tells a stranger where to look. Check what it serves (`tailscale funnel status`),
  keep it off unless a transfer needs it, and if the exposure matters, rename the node in the
  Tailscale admin console (which changes the host name).
* **The LAN addresses and the NAS's make** are not reachable from outside and are not credentials.
  Nothing to rotate; they make a targeted attack a little easier, which is the reason to scrub HEAD.
* **The cache channel's chat id** is not a credential. If its invite link was ever shared publicly,
  revoke the link in Telegram.
