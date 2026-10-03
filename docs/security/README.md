# Keeping a public repository clean

`Asukamadoka/tg_media_down_bot` and its GHCR image are **public**. The source may be open. Secrets,
addresses, personal information and device parameters must not be in it, in the image, or in a
commit. Real values live in the NAS's `.env` (not in git) and in Saki's private notes (the private
`Asukamadoka/skill-builder` hub or the vault), never here.

## Placeholders

Where a document needs a value that identifies the deployment, write the placeholder and, once,
say where the real one lives.

| Placeholder | Stands for | Real value lives in |
|---|---|---|
| `<NAS_IP>` | the NAS's address on the LAN | the NAS's `.env` / Saki's private notes |
| `<MODEL_HOST>` | the LAN machine that serves the models | `TRAFFIC_MODEL_HOST`, `NL_OPENAI_BASE_URL` in the NAS's `.env` |
| `<LAN_IP>` | another LAN address | the NAS's `.env` |
| `<OWNER_ID>` | the owner's Telegram user id | the NAS's `.env` (`ADMIN_USER_IDS`), or claimed with `/claim` |
| `<CACHE_CHAT_ID>` | the cache channel's id | the bot's database (`/cache`), `CACHE_CHAT_ID` in the NAS's `.env` |
| `<NAS_VOLUME>` | the NAS's volume path of the library | the NAS's compose file |
| `<SHARE>` | the share name in `smb://` links | `LOCAL_URL_PREFIX` in the NAS's `.env` |
| `<NAS_MODEL>`, `<FUNNEL_HOST>` | the NAS's make and the public Funnel host | Saki's private notes |
| `<USER>` | a user name in a home directory | — |

In **tests** and examples use values that are plainly invented, so the code can still parse them:
`10.0.0.x`, `192.168.0.x`, `172.16.0.x` for addresses, `-1001234567890` for a chat id,
`/volume9/x` for a volume path, `nas.local` for a host.

**Code defaults never contain a real address or id.** A setting that needs one is empty by default
and read from the environment (`TRAFFIC_MODEL_HOST` is the example: empty means "no model host").

## The private store

Real deployment values (LAN and model-host addresses, the Funnel host, the cache channel id, owner
ids, the library's volume path, the share URL prefix) live **outside this repository**, in two places:

1. **The NAS's `.env`** next to its compose file: the runtime source of truth. The bot reads every
   one of these from the environment, with an empty or neutral default in code; compose files refer
   to host paths as `${VAR}` and never hold a literal.
2. **A private, Saki-owned copy**, so a lost NAS or a rebuilt one does not lose them: Saki's
   private values repo (a PRIVATE GitHub repository) holding **sops/age-encrypted files**:
   `private.env.enc` (dotenv) and `gitleaks.private.toml.enc`. The age key stays on the Mac only
   (never in a repository, never on the NAS); the encrypted files are safe to sync and diff.

`deploy/private.env.example` lists every deployment-identifying variable with placeholders and says
where the real value lives. Copy it to `deploy/private.env` (gitignored, and excluded from the
Docker build context by `.gitignore` and `.dockerignore`; `tests/test_image_hygiene.py` fails if
either stops doing so) and fill it in. Credentials are not in that list: they stay in the NAS's
`.env` or the macOS Keychain only.

If you add a setting that identifies the deployment: read it from the environment with an empty
default, add it to `deploy/private.env.example`, and use `${VAR}` in compose files.

## What stops a leak

1. **CI** (`secrets` job in `.github/workflows/ci.yml`). It runs `gitleaks dir .` over the files of
   the commit with `.gitleaks.toml` and fails the build on a finding. The image is only built when
   every job passes, so a finding also stops the image. The scanner is a pinned release, checksum
   verified.
2. **`.gitleaks.toml`.** The default rules (tokens, keys) plus the rules of this project: private
   addresses, `.local` and Tailscale host names, `smb://` URLs, chat and user ids, volume paths,
   home directories, the NAS's make, e-mail addresses. The rules hold patterns, never values. An
   allowlist entry must say why it is not a leak.
   The **value-based** rules (the exact real host, chat id, model name) cannot be in a public file.
   They live in a private config that CI and the pre-commit hook read when it is there; see
   "Private value rules" below.
3. **A pre-commit hook** (optional): `pip install pre-commit && pre-commit install` runs the same
   rules before a commit is made (`.pre-commit-config.yaml`).
4. **The image.** The `Dockerfile` copies only `tgmd/`, `pikpak_wms/`, `config/` (example files
   only) and `tests/nl/`; `.dockerignore` keeps documents, briefs, tests, `.env*`, the real WMS
   config and token out of the build context as well. `tests/test_image_hygiene.py` fails if either
   changes.

## Decrypt at deploy

1. **The NAS holds no key.** Decrypt on the Mac, from a checkout of Saki's private values repo:
   ```
   SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt sops -d --input-type dotenv --output-type dotenv private.env.enc
   ```
2. **macOS caveat**: sops looks for the age key in `~/Library/Application Support/sops/age/keys.txt`
   by default, so `SOPS_AGE_KEY_FILE` is required when the key lives in `~/.config/sops/age`.
3. **sops dotenv does not preserve comments or blank lines**; the `KEY=value` pairs are exact. The
   commented checklist stays in `deploy/private.env.example`.
4. **Copy only the variables the NAS needs** into the NAS's `.env`, after backing it up as
   `.env.bakN-<date>` (N counts up). Restart `bot` only, never `proxy`. Do not leave the decrypted
   output in a file, a shell history or a chat.
5. **Gitleaks private rules**: decrypt `gitleaks.private.toml.enc` the same way (use
   `--input-type`/`--output-type` matching how it was encrypted) to
   `~/.config/tg_media_down_bot/gitleaks.private.toml`, which the pre-commit hook reads. The CI
   secret `GITLEAKS_PRIVATE_RULES` is **not created** (Saki has not decided); until then CI skips
   the private scan.

## Private value rules

`.gitleaks.toml` can only hold patterns. To catch the *exact* real values, Saki keeps a second
config, `.gitleaks.private.toml`, which is never committed (`.gitignore`, `.dockerignore`). Saki
creates it; nothing here creates it or any secret.

1. **Write the file**, one rule per value, with the real text only in the file:
   ```toml
   title = "tg_media_down_bot private values"

   [[rules]]
   id = "private-value-1"
   description = "a real deployment value (the file is private, say which one here)"
   regex = '''<the exact value, regex-escaped>'''
   keywords = ["<a lowercase fragment of it>"]
   ```
   Keep it in the private store (above). Check it on this repository's files:
   `gitleaks dir . --config /path/to/gitleaks.private.toml --redact`.
2. **Pre-commit**: copy it to `~/.config/tg_media_down_bot/gitleaks.private.toml` (or point
   `GITLEAKS_PRIVATE_CONFIG` at it). The `gitleaks-private` hook of `.pre-commit-config.yaml`
   (`scripts/gitleaks_private_hook.sh`) then scans the staged changes with it. Without the file the
   hook does nothing.
3. **CI**: store the file's base64 as the repository secret `GITLEAKS_PRIVATE_RULES`
   (GitHub, repository, Settings, Secrets and variables, Actions; or
   `base64 < gitleaks.private.toml | gh secret set GITLEAKS_PRIVATE_RULES`). The `secrets` job then
   runs `gitleaks dir . --config <that file>` after the public scan. The file is written to a temp
   path and never echoed; where the secret is not set (forks, other contributors) the step is
   skipped silently. Update the secret when the file changes.

Pattern rules for categories (catalogue-code-shaped file names, addresses, ids) stay in the public
`.gitleaks.toml`. The invented stand-ins it allows in tests and examples are `abcd00123`,
`wxyz04567`, `example.com@`, a studio called `示例影像` and `08号模特`.

Run it yourself:

```
brew install gitleaks            # or any release from github.com/gitleaks/gitleaks
gitleaks dir . --config .gitleaks.toml --redact          # the files
gitleaks git . --config .gitleaks.toml --redact          # every commit (finds what is old)
python scripts/public_audit.py --json audit.json         # files and history, values masked
```

`scripts/public_audit.py` writes at most four characters of any value, so its report can be
published. `docs/security/2026-10-03-public-audit.md` is the first one.

## When something got in

* In a file not yet pushed: remove it, amend the commit.
* **Pushed**: treat it as leaked. A pushed value stays readable in history, in forks and in cached
  views until history is rewritten, and even then it must be considered seen. Rotate it first
  (`history-rewrite-plan.md`, "What to rotate"), then decide about the rewrite.
* Never put a value in a commit message, an issue or a pull request either: gitleaks reads files.
