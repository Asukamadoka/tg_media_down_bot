"""Switching mihomo to a new node list, with backup and rollback (docs/wms/M9.6 §E).

The node list lives in a file provider the bot owns. A switch writes ``<file>.new``,
keeps the active file as ``<file>.bak-<UTC>``, renames ``.new`` over it, asks mihomo to
re-read it (the one whitelisted provider write) and verifies. Anything wrong: the newest
backup is put back, mihomo re-reads it, and the old state is verified too. Blocking code;
callers run it in a thread.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..config import SubscriptionConfig
from ..traffic.mihomo import TG_GROUP, MihomoClient
from .detect import compile_sentinel, real_count
from .validate import Validated, digest_of_file_text, names_of_file_text

log = logging.getLogger(__name__)

BACKUPS_KEPT = 5
POLL_SECONDS = 5.0
MAX_DELAY_TESTS = 12
Checkpoint = Callable[[dict], None]


@dataclass
class SwitchResult:
    ok: bool
    reason: str = ""
    """Stable code when not ok: ``not_reloaded``, ``count_mismatch``, ``no_alive_node``, ..."""
    rolled_back: bool = False
    restored_ok: bool | None = None
    """After a rollback: did the old state verify? False means the old file is bad too."""
    backup: str = ""


class Switcher:
    def __init__(self, config: SubscriptionConfig, client: MihomoClient, *,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._config = config
        self._client = client
        self._clock = clock
        self._sleep = sleep
        self._sentinel = compile_sentinel(config.sentinel_regex)

    @property
    def path(self) -> Path:
        return Path(self._config.provider_file)

    @property
    def new_path(self) -> Path:
        return self.path.with_name(self.path.name + ".new")

    def backups(self) -> list[Path]:
        """Newest first."""
        found = self.path.parent.glob(re.sub(r"([\[\]*?])", r"[\1]", self.path.name) + ".bak-*")
        return sorted(found, key=lambda p: p.name, reverse=True)

    def current_text(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return None

    def current_digest(self) -> str | None:
        text = self.current_text()
        return digest_of_file_text(text) if text is not None else None

    # ----------------------------------------------------------------- switching

    def switch(self, validated: Validated, *, checkpoint: Checkpoint | None = None) -> SwitchResult:
        path, new = self.path, self.new_path
        previous = self._updated_at()
        old_names = self._names()
        backup: Path | None = None
        try:
            self._write_new(validated.provider_yaml)
            if path.exists():
                stamp = datetime.fromtimestamp(self._clock(), UTC).strftime("%Y%m%dT%H%M%S%fZ")
                backup = path.with_name(f"{path.name}.bak-{stamp}")
                shutil.copyfile(path, backup)
                self._prune()
            if checkpoint is not None:
                checkpoint({"backup": str(backup) if backup else "", "digest": validated.digest,
                            "expect_real": validated.real_count})
            os.replace(new, path)
        finally:
            new.unlink(missing_ok=True)  # never left behind
        reason = self._reload_and_verify(validated.real_count, previous,
                                         set(validated.names), old_names)
        if not reason:
            return SwitchResult(True, backup=str(backup or ""))
        log.warning("subscription switch failed (%s); restoring the backup", reason)
        result = self._restore(backup)
        result.reason = reason
        return result

    def rollback(self) -> SwitchResult:
        """``/sub rollback``: the newest backup becomes the active file."""
        backups = self.backups()
        if not backups:
            return SwitchResult(False, "no_backup")
        result = self._restore(backups[0])
        result.ok = bool(result.restored_ok)
        return result

    def recover(self, meta: dict) -> SwitchResult:
        """After a crash in the middle of a switch: drop ``.new``; if the rename had happened,
        put the backup back."""
        self.new_path.unlink(missing_ok=True)
        backup = Path(meta["backup"]) if meta.get("backup") else None
        if backup is None or not backup.exists() or self.current_digest() != meta.get("digest"):
            return SwitchResult(False, "interrupted")  # the active file is still the old one
        result = self._restore(backup)
        result.reason = "interrupted"
        return result

    # ------------------------------------------------------------------ internals

    def _write_new(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.new_path, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        self.new_path.chmod(0o644)

    def _prune(self) -> None:
        for old in self.backups()[BACKUPS_KEPT:]:
            old.unlink(missing_ok=True)

    def _restore(self, backup: Path | None) -> SwitchResult:
        if backup is None or not backup.exists():
            return SwitchResult(False, "no_backup", rolled_back=False)
        text = backup.read_text(encoding="utf-8")
        previous, names = self._updated_at(), self._names()
        tmp = self.path.with_name(self.path.name + ".restore")
        try:
            shutil.copyfile(backup, tmp)
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)
        expect = real_count(names_of_file_text(text), self._sentinel)
        reason = self._reload_and_verify(expect, previous, set(), names)
        return SwitchResult(False, rolled_back=True, restored_ok=not reason, backup=str(backup))

    def _updated_at(self) -> str | None:
        try:
            return str(self._client.provider().get("updatedAt") or "") or None
        except Exception:  # noqa: BLE001 - mihomo may be down; verification will say so
            return None

    def _names(self) -> set[str]:
        try:
            return {str(p.get("name") or "") for p in self._client.provider().get("proxies") or []}
        except Exception:  # noqa: BLE001
            return set()

    def _reload_and_verify(self, expect_real: int, previous: str | None, new_names: set[str],
                           old_names: set[str]) -> str:
        try:
            self._client.reload_nodes()
        except Exception:  # noqa: BLE001
            return "reload_failed"
        deadline = self._clock() + self._config.verify_seconds
        reason = "timeout"
        names: list[str] = []
        while True:
            try:
                provider = self._client.provider()
            except Exception:  # noqa: BLE001
                reason = "provider_unreadable"
            else:
                names = [str(p.get("name") or "") for p in provider.get("proxies") or []]
                stamp = str(provider.get("updatedAt") or "") or None
                if previous is not None and stamp == previous:
                    reason = "not_reloaded"
                elif real_count(names, self._sentinel) != expect_real:
                    reason = "count_mismatch"
                else:
                    reason = ""
            if not reason:
                break
            if self._clock() >= deadline:
                return reason
            self._sleep(POLL_SECONDS)
        return self._check_nodes(names, new_names, old_names)

    def _check_nodes(self, names: list[str], new_names: set[str], old_names: set[str]) -> str:
        need = self._config.verify_alive
        if need > 0:
            alive = 0
            real = [n for n in names if real_count([n], self._sentinel)]
            for node in real[:MAX_DELAY_TESTS]:
                if self._client.delay(node) is not None:
                    alive += 1
                    if alive >= need:
                        break
            if alive < need:
                return "no_alive_node"
        try:  # Telegram last, through the TG group's current exit
            chain = self._client.current_exit(TG_GROUP)
        except Exception:  # noqa: BLE001
            return "tg_unreadable"
        exit_node = chain[-1] if len(chain) > 1 else ""
        if not exit_node or exit_node.upper() in ("DIRECT", "REJECT"):
            return "tg_no_exit"
        if exit_node in names:
            return "" if self._client.delay(exit_node) is not None else "tg_exit_dead"
        if exit_node in old_names and exit_node not in names:
            return "tg_exit_missing"  # points at a node the new list does not have
        return ""
