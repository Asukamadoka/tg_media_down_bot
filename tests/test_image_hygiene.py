"""The image is public: only code and example config go into it (docs/wms/M9.3 §C.5)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_COPY = {"requirements.txt", "tgmd/", "pikpak_wms/", "config/", "tests/nl/"}


def copies() -> list[str]:
    sources = []
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        if line.startswith("COPY "):
            parts = line.split()[1:]
            sources += [p for p in parts[:-1] if not p.startswith("--")]
    return sources


def ignored() -> list[str]:
    return [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
            if line.strip() and not line.startswith("#")]


def test_the_dockerfile_copies_only_code_and_examples():
    assert set(copies()) <= ALLOWED_COPY


def test_only_example_files_are_in_the_copied_config_folder():
    tracked = subprocess.run(["git", "ls-files", "config"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.split()
    assert tracked and all(re.search(r"\.example\.ya?ml$", name) for name in tracked)


def test_the_build_context_leaves_out_docs_tests_briefs_and_env_files():
    rules = set(ignored())
    for needed in (".env*", "docs", "deploy", "scripts", ".github", "*.md", "tests/*",
                   "config/wms.yaml", "config/rules.yaml", "wms-token.json", ".git"):
        assert needed in rules, needed
    assert "!tests/nl" in rules          # the one part of tests/ the image runs


def test_the_build_context_excludes_private_value_files_except_the_example():
    rules = set(ignored())
    assert "deploy/private.env*" in rules
    assert ".gitleaks.private.toml" in rules
    # `deploy` is ignored as a whole, so the example must not be re-included either.
    assert "!deploy/private.env.example" not in rules and "!deploy" not in rules
    # Neither a private env file nor the private gitleaks rules may be tracked.
    tracked = subprocess.run(["git", "ls-files", "deploy", ".gitleaks.private.toml"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
    assert not [n for n in tracked
                if Path(n).name.startswith("private.env") and n != "deploy/private.env.example"]
    assert ".gitleaks.private.toml" not in tracked


def test_the_gitignore_keeps_private_value_files_out_of_git():
    rules = (ROOT / ".gitignore").read_text()
    for needed in ("deploy/private.env*", "!deploy/private.env.example", ".gitleaks.private.toml"):
        assert needed in rules, needed


def test_the_private_env_example_holds_placeholders_only():
    text = (ROOT / "deploy" / "private.env.example").read_text()
    for line in text.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        assert name.isupper(), name
        # Empty, a placeholder in <angle brackets>, or an invented value.
        assert value in {"", "/library", "-1001234567890"} or "<" in value, line


def test_the_gitignore_keeps_the_real_config_and_secrets_out_of_git():
    rules = (ROOT / ".gitignore").read_text()
    for needed in (".env", "config/wms.yaml", "config/rules.yaml", "wms-token.json", "sessions/"):
        assert needed in rules, needed
