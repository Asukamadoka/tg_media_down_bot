# Brief: M9.5 — events "Verification code is invalid" (captcha scoping) + agents-md v2.0.0 adoption

Repo `~/Developer/tg_media_down_bot`, branch `claude/telegram-media-downloader-bot-samm1v` (HEAD `6b77466`, M9.4). Two independent commits, Part A first.

## Context (read first)

- `CC_BRIEF.md` §1 red lines beat everything here. No NAS, no Telegram, no live PikPak calls, no CI or repo-settings changes. Never force-push.
- `docs/HANDOFF.md` latest section (M9.4) for conventions: what was done / env / migration / deploy steps (Cowork) / verification evidence / deviations.
- Docs, code, comments and commits in English. Match the surrounding code style (plain-prose docstrings, small functions).

## Part A — M9.5: captcha scoping fix and event-sync fallback

### Problem (live report)

Saki's `/do` in Telegram answers `仓储：events: Verification code is invalid`.

Code analysis (not yet confirmed against NAS logs):

1. `/do` → `eventsync.refresh_index` → `sync_events` → `_read_new` → `WmsClient.events` → `pikpakapi.PikPakApi.events` (`GET /drive/v1/events`). `WmsClient._call` wraps the SDK error as `WmsError("events: …")`.
2. pikpakapi 0.1.11 keeps the captcha as **state on the shared api object**. `get_download_url()` does `captcha_init("GET:/drive/v1/files/<id>")`, sets `self.captcha_token`, sends the GET, then sets it back to `None` — with no `try/finally`. `get_headers()` adds `X-Captcha-Token` (and a different User-Agent) to **every** request while it is set.
   - Concurrency: outbound downloads run files in parallel (M9.2) and re-fetch links; any request sent on the same api object during that window (e.g. `/do`'s events call) carries a captcha token minted for another action → "Verification code is invalid".
   - Stickiness: if that GET raises (rate limit, network), `captcha_token` is never cleared; every later request carries the stale token until the bot restarts.
3. `_read_new` re-raises an error on the **first** page (only later pages fall back), and `refresh_index` does not catch it, so `/do` fails instead of falling back.

### Steps

1. **Own `download_links` request, no shared state.** In `pikpak_wms/core/client.py`, stop calling the SDK's `get_download_url`. Do the same two requests yourself: `captcha_init(action=f"GET:/drive/v1/files/{file_id}")`, then the GET with **explicit headers** built from `api.get_headers()` plus `X-Captcha-Token` and the custom User-Agent the SDK would use (`api.build_custom_user_agent()`), passed through the SDK's `_make_request(..., headers=...)`. Never assign `api.captcha_token`. Keep the existing retry/rate-limit/auth mapping (route it through `_call` or an equivalent helper). Keep `download_links`' return contract unchanged.
2. **Captcha errors: refresh for the action and retry once.** In `WmsClient._call` (or a small helper used by it), recognise captcha refusals (`verification code`, `captcha`, error_code 4002 / `captcha_invalid` if visible; match loosely like the other hint tuples). On the first such error: clear any leftover `api.captcha_token` (log once at WARNING that a leaked token was cleared, never log the token), mint a captcha for that call's action (`"GET:/drive/v1/events"` for events; give `_call` an optional `action=` for the calls that need it), retry once with that token sent as an explicit header (not via shared state). Second failure → raise a new `CaptchaError(WmsError)` from `core/errors.py`.
3. **Events failure never fails `/do`.** In `eventsync`:
   - `_read_new`: an error on the first page (except `AuthError`, which must still propagate) is returned as a lost/failed feed, not raised.
   - `sync_events` / `refresh_index`: an events failure becomes `fallback="incremental"` with reason `the event feed failed: <short error>`; the cursor is **not** rebased (keep the remembered one, so the next good sync resumes). `allow_full` callers (scheduled job) also use incremental for this reason — a failing feed is not a lost cursor.
   - The plan says so: `note_freshness` / the plan line shows the kind as incremental, and add one plan note (i18n en + zh) such as `Event sync failed (<reason>); used an incremental stocktake instead`. The admin alert goes through the existing `_alert_once`.
4. **`wms events`** (`ops/listing.py` → `client.events`) benefits from step 2; `--raw` output unchanged.
5. **Tests** (new `tests/test_wms_m95.py`, fake api objects only):
   - `download_links` never sets `api.captcha_token`; a concurrent `events` call during a slow `download_links` sends no `X-Captcha-Token` of another action.
   - A leaked `api.captcha_token` is cleared and the call retried once with a captcha for its own action; the second failure raises `CaptchaError`; exactly one extra `captcha_init`.
   - First-page events error → `refresh_index` runs an incremental stocktake, returns kind `incremental`, cursor unchanged, plan carries the new note (en + zh), alert once; `AuthError` still propagates.
   - Existing M8.1 tests still pass (adjust only where the first-page behaviour intentionally changed; list them in HANDOFF).
6. `ruff check .` clean; full `pytest -q` green (report count: 1922 → N).
7. **HANDOFF**: new section `## Stage 3 · M9.5: captcha scoping and event-sync fallback` in the M9.4 format, including "Deploy steps (Cowork)": pull image, restart `bot` only, `docker compose exec -T bot wms events --raw --limit 5` succeeds, `grep -E "captcha|event sync" ` in bot logs, Saki tests `/do`. Rollback: image `6b77466`.
8. Commit `fix(wms): M9.5 - scope PikPak captcha per request, events fall back to incremental stocktake`, push to the branch.

### Acceptance (checked from outside)

- [ ] No assignment to `captcha_token` on the api object anywhere in `pikpak_wms/` except clearing a leaked one.
- [ ] `tests/test_wms_m95.py` exists, covers the 3 groups above; `pytest -q` green, count printed.
- [ ] `ruff check .` clean; gitleaks config untouched.
- [ ] HANDOFF M9.5 section with deploy steps and the list of changed existing tests.
- [ ] Commit SHA printed and pushed.

## Part B — adopt agents-md v2.0.0 (separate commit, no code changes)

Source: `~/Developer/agents-md/ops/handoffs/2026-10-04-to-pikpak-wms.md`, English part §1. Follow it exactly:

1. `AGENTS.md` (new, ≤ 600 words). Line 1 exactly: `> Universal v2.0.0 — universal rules: ~/Developer/agents-md/core/AGENTS.md (read §2, §5, §6, §9 at boot)`. Then the §12 block from `~/Developer/agents-md/overlays/pikpak-wms.md`, with `Commands:` filled **only** with commands that have actually run in this repo (e.g. `pytest -q`, `ruff check .`, `uv run …` you ran in Part A, the `docker compose` commands documented as run in HANDOFF deploy steps). No IPs, hostnames, domains, NAS model or channel IDs (the repo is public). End with `<!-- genes:start --><!-- genes:end -->`.
2. `CLAUDE.md` (new): `@AGENTS.md` plus a line `Claude specifics: ~/Developer/agents-md/core/CLAUDE.md`.
3. `ops/evolution/journal.jsonl` (empty) and `ops/gene/genes.yaml` with WMS genes only (`scope: project:pikpak-wms`, same schema as `~/Developer/agents-md/ops/gene/genes.yaml`, `version: 1`): never edit `data/db` / `wms.yaml` from the host; never touch Telegram sessions/auth keys, `TG_DIRECT_MEDIA` stays off; `wms do` / `outbound` plan by default, `--apply` only on Saki's word; NL model targets (model-only ≥ 80 %, with rules ≥ 95 %, dangerous = 0, ≤ 5 s); restart `bot` only, never `proxy`. Ones not in Saki's words: `status: candidate`. Add `REDESIGN.md` with frontmatter `project: pikpak-wms` (empty body) so project genes compile in.
4. Run `SKILLBUILDER_HUB=$PWD/ops uv run ~/Developer/skill-builder/hub/scripts/compile-agents.py .` and confirm the genes block is filled.
5. Commit `chore: adopt agents-md v2.0.0 (thin AGENTS.md, genes, journal)`, push (never force).

Acceptance: `head -1 AGENTS.md` is the line above; `grep -c "@AGENTS.md" CLAUDE.md` = 1; `grep -c "genes:start" AGENTS.md` = 1; `wc -w AGENTS.md` ≤ 600 before the compiled block (state the number); gitleaks clean.

## Out of scope / when unsure

- No NAS, Telegram, live PikPak, CI or GitHub settings. No history rewrite.
- Do not upgrade pikpakapi.
- If something is ambiguous, pick the conservative option and record it under "Deviations and open questions" in HANDOFF. Ask only if a step would be unsafe.
- Finish by printing both commit SHAs, the test count and ruff result.
