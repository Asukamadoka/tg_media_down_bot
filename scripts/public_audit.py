#!/usr/bin/env python3
"""Audit a public repository for values that should not be public (docs/wms/M9.3 §C).

Scans the files of HEAD and every line ever added in any commit, with a list of patterns
(addresses, ids, tokens, device parameters). It reports where and when, never what:
a value is shown as at most its first four characters and its length.

    python scripts/public_audit.py --json audit.json           # findings, machine-readable
    python scripts/public_audit.py --before audit.json --md docs/security/<date>-public-audit.md

``--before`` is a JSON of an earlier run; the report then also says whether each finding was
in HEAD *then*. gitleaks (``gitleaks git .``) covers secrets with entropy rules; this covers
what no entropy rule can: a LAN address or a chat id is not random.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

HEX = "0-9a-fA-F"

# category -> list of (pattern id, regex). Generic shapes only: no value is written here.
PATTERNS: dict[str, list[tuple[str, str]]] = {
    "token": [
        ("telegram-bot-token", r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
        ("telegram-api-hash", rf"(?i)api[_-]?hash[\"'\s:=]{{1,5}}[{HEX}]{{32}}\b"),
        ("telegram-session-string", r"\b1[A-Za-z0-9_-]{240,}={0,2}"),
        ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        ("pikpak-token", r"(?i)(?:refresh|access)[_-]?token[\"'\s:=]{1,5}[A-Za-z0-9._-]{30,}"),
        ("subscription-url-key",
         r"https?://[^\s\"'<>]+[?&](?:key|token|sub|secret)=[A-Za-z0-9_%.-]{8,}"),
        ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})"),
        ("anthropic-or-openai-key", r"\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}"),
        ("private-key-block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ],
    "address": [
        ("lan-10", r"(?<![\d.])10\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.]*\d)"),
        ("lan-192", r"(?<![\d.])192\.168\.\d{1,3}\.\d{1,3}(?![\d.]*\d)"),
        ("lan-172", r"(?<![\d.])172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}(?![\d.]*\d)"),
        ("mdns-local-host", r"\b[A-Za-z0-9-]+\.local\b"),
        ("tailnet-host", r"\b[a-z0-9-]+\.tail[0-9a-f]+\.ts\.net\b"),
        ("smb-url", r"smb://[^\s\"'<>]+"),
    ],
    "personal": [
        ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}\b"),
        ("telegram-chat-id", r"(?<!\d)-100\d{9,}(?!\d)"),
        ("telegram-user-id",
         r"(?i)(?:owner|admin|user|chat|cache)[_ -]?(?:id|ids)\b[\"'\s:=\[]{1,6}-?\d{8,}"),
    ],
    "device": [
        ("nas-volume-path", r"/(?:volume\d+|Volume\d+|mnt/(?!c/)[A-Za-z0-9_-]+)/[^\s\"'<>]+"),
        ("mac-home-path", r"/Users/[A-Za-z0-9._-]+"),
        ("linux-home-path", r"/home/(?!tgmd\b)[A-Za-z0-9._-]+"),
        ("nas-model", r"(?i)\b(?:UGREEN|UGOS|DXP\d{3,4}\w*|Synology|DS\d{3}\+?|QNAP)\b"),
        ("puid-pgid", r"\b(?:PUID|PGID)\s*[=:]\s*\d+|user:\s*[\"']?\d{3,5}:\d{2,5}"),
    ],
}

# Values that look like findings and are not: documentation addresses, bot-less mail, etc.
IGNORE_VALUES = re.compile(
    r"(?i)(?:@example\.|@anthropic\.com|users\.noreply\.github\.com|noreply@|@localhost|"
    r"@sentry|@pytest|@dataclass|@property|@staticmethod|@classmethod|@app\.|@router|"
    r"@asynccontextmanager|@overload|@abstractmethod|@pytest\.|\.local_dir)")
# The placeholders this repository uses, loopback, and shapes that are plainly invented:
# sequential digits, a repeated digit, `name.local`, a network written as a CIDR.
IGNORE_VALUE_RE = re.compile(
    r"^(?:10\.0\.0\.\d+|192\.168\.0\.\d+|172\.16\.0\.\d+|127\.|0\.0\.0\.0|"
    r"(?:nas|name|host|server|mac|example|model|my-nas)\.local|/volume9/|"
    r"-100(?:1234567890|9876543210|9999999999|1111111111|0000000000)|"
    r"123456789:AA|1234567\d*:AA|smb://…|smb://(?:<[^>]+>|10\.0\.0\.\d+|nas|nas-host|host|server)/)")
FAKE_NUMBER = re.compile(r"(?:1234567890?|9876543210?|0123456789|(\d)\1{5,})")
SKIP_PATHS = re.compile(
    r"(?:^|/)(?:\.git/|__pycache__/|.*\.pyc$|.*\.(?:png|jpg|ico|woff2?)$|"
    r"scripts/public_audit\.py$|\.gitleaks\.toml$|docs/security/)")  # they describe the patterns
COMPILED = [(cat, pid, re.compile(rx)) for cat, rules in PATTERNS.items() for pid, rx in rules]


@dataclass(frozen=True)
class Hit:
    category: str
    rule: str
    file: str
    value_id: str
    mask: str


def mask(value: str) -> str:
    """At most four characters of the value, and how long it was."""
    return f"{value[:4]}…({len(value)})"


def scan_text(text: str, file: str) -> set[Hit]:
    found: set[Hit] = set()
    for line in text.splitlines():
        if len(line) > 4000:
            line = line[:4000]
        for category, rule, rx in COMPILED:
            for match in rx.finditer(line):
                value = match.group(0)
                if IGNORE_VALUES.search(value) or IGNORE_VALUE_RE.match(value):
                    continue
                if rule == "email" and value.startswith("@"):
                    continue
                if rule == "telegram-user-id" and FAKE_NUMBER.search(value):
                    continue
                if rule == "telegram-api-hash" and FAKE_NUMBER.search(value):
                    continue
                tail = line[match.end():match.end() + 4]
                if rule in ("lan-172", "lan-192") and tail.startswith(("/", ".0/")):
                    continue  # a whole range written as a CIDR, not an address
                digest = hashlib.sha256(value.encode()).hexdigest()[:10]
                found.add(Hit(category, rule, file, digest, mask(value)))
    return found


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True,
                          errors="replace").stdout


def head_hits() -> set[Hit]:
    hits: set[Hit] = set()
    # Tracked files and new ones not yet added (they are about to be public too).
    for name in git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0"):
        if not name or SKIP_PATHS.search(name):
            continue
        try:
            text = Path(name).read_text("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits |= scan_text(text, name)
    return hits


def history_hits() -> dict[Hit, str]:
    """Every finding in any added line of any commit → the oldest commit that had it."""
    first: dict[Hit, str] = {}
    log = git("log", "--all", "--reverse", "-p", "--no-color", "--format=@@commit %h",
              "--diff-filter=AMR", "-U0")
    commit, file, buffer = "", "", []

    def flush() -> None:
        if file and buffer and not SKIP_PATHS.search(file):
            for hit in scan_text("\n".join(buffer), file):
                first.setdefault(hit, commit)
        buffer.clear()

    for line in log.splitlines():
        if line.startswith("@@commit "):
            flush()
            commit, file = line.split()[1], ""
        elif line.startswith("+++ b/"):
            flush()
            file = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            buffer.append(line[1:])
    flush()
    return first


def collect() -> list[dict]:
    head = head_hits()
    history = history_hits()
    rows = []
    for hit in sorted(set(head) | set(history),
                      key=lambda h: (h.category, h.rule, h.file, h.value_id)):
        rows.append({"category": hit.category, "rule": hit.rule, "file": hit.file,
                     "value": hit.value_id, "mask": hit.mask,
                     "first_commit": history.get(hit, "-"), "in_head": hit in head})
    return rows


def report(rows: list[dict], before: list[dict] | None, today: str) -> str:
    then = {(r["category"], r["rule"], r["file"], r["value"]): r["in_head"] for r in before or []}
    lines = [f"# Public repository audit, {today}", "",
             "Generated by `scripts/public_audit.py` (patterns, no values) next to "
             "`gitleaks git .`. **No value appears here**: each finding shows at most its first "
             "four characters and its length, so this file is safe to publish. A finding is one "
             "value in one file; `first commit` is the oldest commit in any branch that "
             "contained it.", ""]
    by_category: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_category[row["category"]].append(row)
    lines += ["## Headline", "", "| category | findings | files | still in HEAD |",
              "|---|---|---|---|"]
    for category in PATTERNS:
        items = by_category.get(category, [])
        lines.append(f"| {category} | {len(items)} | {len({r['file'] for r in items})} | "
                     f"{sum(r['in_head'] for r in items)} |")
    if before:
        started = sum(1 for v in then.values() if v)
        lines += ["", f"At the start of this audit {started} findings were in HEAD; "
                      f"{sum(r['in_head'] for r in rows)} are now."]
    for category in PATTERNS:
        items = by_category.get(category, [])
        lines += ["", f"## {category} ({len(items)})", ""]
        if not items:
            lines.append("Nothing found.")
            continue
        lines += ["| rule | file | value | first commit | in HEAD now | in HEAD at audit start |",
                  "|---|---|---|---|---|---|"]
        for r in items:
            key = (r["category"], r["rule"], r["file"], r["value"])
            was = {True: "yes", False: "no"}.get(then.get(key), "n/a") if before else "n/a"
            lines.append(f"| {r['rule']} | `{r['file']}` | `{r['mask']}` | {r['first_commit']} | "
                         f"{'yes' if r['in_head'] else 'no'} | {was} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", help="write the findings here")
    parser.add_argument("--before", help="an earlier --json, for the 'at audit start' column")
    parser.add_argument("--md", help="write the Markdown report here")
    parser.add_argument("--date", default="", help="date in the report title")
    parser.add_argument("--footer", help="a Markdown file appended to the report")
    parser.add_argument("--fail-on-head", action="store_true",
                        help="exit 1 when anything is still in HEAD")
    args = parser.parse_args(argv)
    rows = collect()
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    if args.md:
        before = json.loads(Path(args.before).read_text()) if args.before else None
        text = report(rows, before, args.date or "today")
        if args.footer:
            text += "\n" + Path(args.footer).read_text()
        Path(args.md).write_text(text)
    counts = defaultdict(int)
    for row in rows:
        counts[row["category"]] += 1
    in_head = sum(r["in_head"] for r in rows)
    print(f"{len(rows)} findings ({', '.join(f'{k}: {v}' for k, v in sorted(counts.items()))}); "
          f"{in_head} still in HEAD")
    return 1 if args.fail_on_head and in_head else 0


if __name__ == "__main__":
    sys.exit(main())
