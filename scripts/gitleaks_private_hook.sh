#!/usr/bin/env bash
# Pre-commit hook: scan the staged changes with Saki's private value rules, if he has them.
#
# The rules (the exact real host, chat id, model) must never be in this repository, so they live in
# ~/.config/tg_media_down_bot/gitleaks.private.toml (override: GITLEAKS_PRIVATE_CONFIG). Without
# that file, or without gitleaks, this does nothing and succeeds. See docs/security/README.md.
set -euo pipefail

config="${GITLEAKS_PRIVATE_CONFIG:-$HOME/.config/tg_media_down_bot/gitleaks.private.toml}"
[ -f "$config" ] || exit 0
command -v gitleaks >/dev/null 2>&1 || exit 0

exec gitleaks git --pre-commit --staged --config "$config" --redact --no-banner --exit-code 1
