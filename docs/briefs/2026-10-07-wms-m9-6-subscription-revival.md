# Brief: M9.6 — subscription revival (bot side)

Repo `~/Developer/tg_media_down_bot`, branch `claude/telegram-media-downloader-bot-samm1v`. Baseline: the commit that adds this brief (parent `395e806`). Spec: `docs/wms/M9.6-subscription-revival.md` — read all of it first; it is the source of truth.

## Goal

Detect a dying proxy subscription, push Saki a Telegram checklist, accept a new subscription URL from Saki, validate it, and switch mihomo to it with backup and rollback. Saki does the login, account deletion and re-registration by hand; the bot never touches a mailbox or the provider site.

## Steps (commit per stage, each green before the next)

1. **Plumbing.** `tgmd/subscription/` package: config keys (all defaulted, `SUB_REVIVAL_ENABLED=0` means nothing changes), `redact_url()`, DB table `sub_case` (additive migration only), kv key for the stored URL. Move `SUBSCRIPTION_DOMAINS` out of `tgmd/traffic/classify.py` into `SUB_HOSTS` (default empty); the literal domain must be gone from HEAD.
2. **Detect** (`detect.py`) per spec §A, reusing `nodes.py` real-node counting; hook into `NodeManager.health` without duplicating its state machine.
3. **Validate** (`validate.py`) per spec §D, with injected fetch; YAML and base64 formats; SSRF guard.
4. **Switch** (`switch.py`) per spec §E: file provider write + backup + atomic rename + `PUT /providers/proxies/main` (add that one path to `_ALLOWED_WRITES`) + verify + rollback + resume after a crash.
5. **Flow + UI**: `/sub`, `/sub switch`, `/sub rollback`, checklist message with buttons, reminder cap, URL message deletion, admin-only, i18n zh/en.
6. Docs: `deploy/private.env.example`, `docs/security/README.md` private-value list, `deploy/restricted-network/README.md` section "subscription as a file provider" (placeholders only), `docs/wms/ROADMAP.md` row.
7. HANDOFF section `## Stage 4 · M9.6: subscription revival` with: what changed, env vars, **what the NAS needs** (spec "NAS needs" 2–5, verbatim steps), Saki-only actions, verification evidence with counts, deviations and open questions.
8. `ruff check .`, `pytest -q` (state N → M), `gitleaks dir . --config .gitleaks.toml --redact`, `python scripts/public_audit.py --fail-on-head` = 0. Commit `feat(wms): M9.6 - subscription revival (bot side)`, push (never force), print the SHA.

## Acceptance

- [ ] Every test class in spec "Tests" exists and passes; suite count stated; ruff, gitleaks, public audit clean.
- [ ] A subscription URL never appears in any log record, message, exception, audit row or test output (there is a test that asserts it over the whole flow, including failures).
- [ ] The mihomo write whitelist gained exactly one path and still refuses `PUT /configs`.
- [ ] With `SUB_REVIVAL_ENABLED` unset, behaviour and existing tests are unchanged (M9.1's alert tail is the only text allowed to differ, and only when enabled).
- [ ] No mailbox, Spark, Gmail or IMAP code, no provider-site automation, no live network in tests.
- [ ] HANDOFF M9.6 section complete.

## Red lines

`CC_BRIEF.md` §1 and the spec's "Hard limits". The repo and image are public: no provider name, domain, URL, mailbox, address or node name from the real provider anywhere, in code, docs, tests, fixtures or commit messages; invented values only (`example.invalid`). Do not touch the NAS, Telegram sessions or live PikPak. Do not upgrade dependencies. If the spec and the code disagree, stop and write it in HANDOFF under open questions.

## Out of scope

Everything in the spec's "Out of scope" and "NAS needs" (Cowork does those).
