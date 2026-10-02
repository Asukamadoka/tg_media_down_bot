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

## What stops a leak

1. **CI** (`secrets` job in `.github/workflows/ci.yml`). It runs `gitleaks dir .` over the files of
   the commit with `.gitleaks.toml` and fails the build on a finding. The image is only built when
   every job passes, so a finding also stops the image. The scanner is a pinned release, checksum
   verified.
2. **`.gitleaks.toml`.** The default rules (tokens, keys) plus the rules of this project: private
   addresses, `.local` and Tailscale host names, `smb://` URLs, chat and user ids, volume paths,
   home directories, the NAS's make, e-mail addresses. The rules hold patterns, never values. An
   allowlist entry must say why it is not a leak.
3. **A pre-commit hook** (optional): `pip install pre-commit && pre-commit install` runs the same
   rules before a commit is made (`.pre-commit-config.yaml`).
4. **The image.** The `Dockerfile` copies only `tgmd/`, `pikpak_wms/`, `config/` (example files
   only) and `tests/nl/`; `.dockerignore` keeps documents, briefs, tests, `.env*`, the real WMS
   config and token out of the build context as well. `tests/test_image_hygiene.py` fails if either
   changes.

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
