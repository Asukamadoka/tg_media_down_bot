# Brief: personal values out of the public repo for good (repo option A, non-destructive part)

Repo `~/Developer/tg_media_down_bot`, branch `claude/telegram-media-downloader-bot-samm1v`. Start from the HEAD that contains M9.5 and the agents-md adoption commit. One commit.

## Goal

Saki approved keeping the repository public on one condition: LAN/intranet addresses, the NAS make/model, the Tailscale Funnel host, the cache channel id and any other personal data must live only in a private store, so none of it appears in anything forkable or clonable (HEAD, docs, tests, CI, images). M9.3 already scrubbed HEAD (`docs/security/2026-10-03-public-audit.md`: 0 findings in HEAD). This brief closes what is left **without touching history, repo visibility or GHCR**.

## Context

- Read `docs/security/README.md`, `docs/security/2026-10-03-public-audit.md`, `docs/security/history-rewrite-plan.md`, `.gitleaks.toml`, `scripts/public_audit.py`, the `secrets` job in `.github/workflows/ci.yml`.
- `CC_BRIEF.md` §1 red lines apply. No NAS, no Telegram, no GitHub settings, no history rewrite, no force-push, no package deletion.
- Never write a real value anywhere — not in code, tests, docs, commit messages, or this repo's gitleaks rules. If you need to know whether a value is real, you do not need its text: use the audit script's masked output.

## Steps

1. **Private store contract (docs + loader, no values).**
   - Document in `docs/security/README.md` that real deployment values live in a private store outside this repo: the NAS's `.env` (runtime source of truth) plus a private, Saki-owned copy (private GitHub repo or a sops/age-encrypted file kept in that private repo — Saki chooses; write both options and mark the choice `PENDING (Saki)`).
   - Add `deploy/private.env.example`: every deployment-identifying variable (`TRAFFIC_MODEL_HOST`, `NL_OPENAI_BASE_URL`, `CACHE_CHAT_ID`, `LOCAL_URL_PREFIX`, `ADMIN_USER_IDS`, library volume path for compose, …; grep the code and compose files for the full list) with placeholder values and one comment each saying where the real value lives.
   - Make sure every such value is read from the environment with an empty/neutral default; list any that are not and fix them (as `TRAFFIC_MODEL_HOST` was in M9.3). The compose files must reference `${VAR}` for the library volume path instead of a literal.
2. **Remaining personal data in HEAD** (audit "Left as it is"): replace the real downloaded file names / catalogue codes / studio name quoted in docs and tests with plainly invented ones (one constant in tests). Keep test behaviour identical. Do not rename `LIBRARY_NAME` (behaviour); leave "Saki" and the public GitHub handle.
3. **Rules that cannot return.**
   - Extend `.gitleaks.toml` with pattern rules for any category step 2 shows is not yet covered (e.g. catalogue-code-shaped file names in docs/tests, with an allowlist for the invented constant). Patterns only.
   - Value-based rules (the exact real host, chat id, model) must not live in this repo. Add support for an optional second config: CI runs `gitleaks dir . --config .gitleaks.private.toml` **only if** a secret `GITLEAKS_PRIVATE_RULES` is set (base64 of that file), written to a temp path, never echoed, and skipped silently on forks. Same for the pre-commit hook: use `~/.config/tg_media_down_bot/gitleaks.private.toml` when present. Add `.gitleaks.private.toml` to `.gitignore` and `.dockerignore`. Document how Saki creates the file and the secret (Saki does it; do not create secrets).
   - Extend `tests/test_image_hygiene.py` so the image build context also excludes `deploy/private.env*` except the example.
4. `python scripts/public_audit.py --fail-on-head` (or its equivalent flag) clean; `gitleaks dir . --config .gitleaks.toml --redact` clean; `ruff check .` clean; `pytest -q` green.
5. **History and images: plan only.** Update `docs/security/history-rewrite-plan.md` with a short "Option A completion" section listing exactly what still holds old values (history commits by count, PR refs, old GHCR versions) and the steps that would remove them — marked **not executed, needs Saki's explicit yes**.
6. HANDOFF section `## Security · private values vault (option A)` with what changed, env changes for the NAS (none expected beyond names already there; say so), and the list of Saki-only actions.
7. Commit `chore(security): private values vault contract, private gitleaks rules hook, scrub remaining personal data`, push (never force).

## Acceptance

- [ ] `deploy/private.env.example` lists every deployment-identifying variable with placeholders only.
- [ ] No literal deployment value in compose files; all from `${VAR}`.
- [ ] Optional private gitleaks config wired in CI (secret-gated, skipped when absent) and pre-commit; `.gitleaks.private.toml` ignored by git and Docker.
- [ ] Audit script and gitleaks clean on HEAD; tests green with count; ruff clean.
- [ ] history-rewrite-plan has the option-A completion section, nothing executed.

## Out of scope / ambiguity

No history rewrite, force-push, visibility change, GHCR deletion, GitHub secret creation, NAS edits. If a value's status is unclear, treat it as personal and replace it. Record decisions in HANDOFF "Deviations and open questions".
