# Brief: M9.5.1 — never persist or restore a PikPak captcha token

Repo `~/Developer/tg_media_down_bot`, branch `claude/telegram-media-downloader-bot-samm1v` (HEAD `cbbc6b8`, deployed on the NAS). One commit.

## Goal

Live finding after deploying M9.5: the stored PikPak session in the bot database carries a non-empty `captcha_token`. `PikPakApi.to_dict()` copies the whole `__dict__` (including `captcha_token` and `user_agent`), `tgmd/pikpak.py::_persist` stores it (only username/password are stripped), and `_restore` → `PikPakApi.from_dict()` writes it back. A captcha minted for one action (`GET:/drive/v1/files/<id>`, parked by the SDK's `get_download_url`) was persisted during a token refresh, so every restored client sends a stale `X-Captcha-Token`: events answered "Verification code is invalid", and after a restart the `_restore` probe (`get_quota_info`) fails the same way and the user sees "your PikPak session has expired".

## Steps

1. `tgmd/pikpak.py`: add the captcha state to what is never persisted — `captcha_token` (and the derived `user_agent`, which the SDK rebuilds) — in `strip_credentials` or a sibling `strip_transient` used by `_persist`. In `_restore`, drop those keys from the saved dict **before** `from_dict`, so sessions already stored with a captcha are healed on the next restore; after a successful probe, re-persist the cleaned record once.
2. If the probe still fails with a captcha-type error (`verification code`, `captcha`), retry the probe once with `client.captcha_token = None` before declaring the session unusable; log at INFO which case happened, never the token.
3. Check every other `to_dict` / `from_dict` / `kv_set_json` path for PikPak clients (user and shared keys, `login_with_password`, the WMS provider) and apply the same rule.
4. Tests (new `tests/test_pikpak_captcha_persist.py`, fake SDK only): a client with `captcha_token` set is persisted without it; a stored record that contains `captcha_token` restores to a client whose `captcha_token` is None and whose probe succeeds; the cleaned record is written back; a captcha error on the probe is retried once; unrelated fields are untouched.
5. `ruff check .`, `pytest -q` green (state the count: 1935 → N), gitleaks clean.
6. HANDOFF: section `## Stage 3 · M9.5.1: no captcha in a stored PikPak session` with the live evidence above (no values), deploy steps (pull, restart `bot` only, then `docker compose exec -T bot wms events --raw --limit 5` must work without a new `/pikpak login`), rollback to `cbbc6b8`.
7. Commit `fix(pikpak): never persist or restore a captcha token in a stored session`, push (never force), print the SHA.

## Acceptance

- [ ] No PikPak client record is written with `captcha_token`; restore strips it from old records and re-persists clean.
- [ ] New tests cover the 5 cases; suite green with count; ruff and gitleaks clean.
- [ ] HANDOFF M9.5.1 section with deploy steps.

## Out of scope

No NAS, no Telegram, no live PikPak. Do not upgrade pikpakapi. Do not print or log tokens.
