"""Add rules to the rules file without disturbing what is already there.

A scheduled natural-language command (「每天凌晨把新文件按类型归档」) becomes
a rule in the rules file, which stays the one place behaviour lives
(rule 3). The file is the person's: its comments and layout are kept by
appending text rather than rewriting it, the result is validated, and on
any failure the original is put back untouched.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ..config import Config, data_dir, rules_path
from ..core.errors import WmsError
from ..rules.schema import Rule, RulesError, load_rules


def rule_to_dict(rule: Rule) -> dict[str, Any]:
    data: dict[str, Any] = {"name": rule.name}
    if not rule.enabled:
        data["enabled"] = False
    if rule.stage != "organize":
        data["stage"] = rule.stage
    if rule.schedule is not None:
        data["schedule"] = rule.schedule.model_dump()
    data["scope"] = rule.scope
    if not rule.recursive:
        data["recursive"] = False
    match = rule.match.model_dump(exclude_none=True, exclude_defaults=True, mode="json")
    if match:
        data["match"] = match
    data["actions"] = [
        {step.op: step.spec.model_dump(exclude_defaults=True, mode="json")}
        for step in rule.actions
    ]
    return data


def target_file(config: Config) -> Path:
    """Where new rules go: the rules file in use, or a new one on the data volume."""
    path = config.rules_file or rules_path()
    if path.exists():
        return path
    return data_dir() / "rules.yaml"


def append_rules(path: Path, rules: list[Rule], *, comment: str = "") -> None:
    original = path.read_text(encoding="utf-8") if path.exists() else None
    text = original if original is not None else "version: 1\n\nrules:\n"
    if not re.search(r"^rules:", text, re.MULTILINE):
        text = text.rstrip("\n") + "\n\nrules:\n"
    elif re.search(r"^rules:\s*\[\s*\]\s*$", text, re.MULTILINE):
        text = re.sub(r"^rules:\s*\[\s*\]\s*$", "rules:", text, flags=re.MULTILINE)
    # Follow the file's own list indentation ("  - name:" or "- name:").
    found = re.search(r"^rules:\s*\n(?:\s*#.*\n|\s*\n)*(\s*)- ", text, re.MULTILINE)
    indent = found.group(1) if found else "  "

    block = []
    if comment:
        block.append(f"{indent}# {comment.replace(chr(10), ' ')}")
    for rule in rules:
        dumped = yaml.safe_dump([rule_to_dict(rule)], allow_unicode=True, sort_keys=False,
                                width=1000)
        block.extend(indent + line for line in dumped.rstrip("\n").split("\n"))
    new_text = text.rstrip("\n") + "\n\n" + "\n".join(block) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    try:
        loaded = load_rules(path)
        names = {rule.name for rule in loaded.rules}
        missing = [rule.name for rule in rules if rule.name not in names]
        if missing:
            raise RulesError(f"the new rules did not land in the list: {missing}")
    except (RulesError, yaml.YAMLError) as exc:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(original, encoding="utf-8")
        raise WmsError(f"could not add rules to {path}: {exc}", key="nl.error.rules_file",
                       path=str(path), error=str(exc)) from exc
