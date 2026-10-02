"""Node selection: modes per group, auto re-picks, and subscription health.

The bot owns four mihomo groups (docs/wms/M9.1 §A): ``FAST`` (a select the bot
points at the fastest node), ``TG-PICK`` (what Telegram uses first), ``PROXY``
(general access) and ``PROBE``. Everything goes through
:class:`~tgmd.traffic.mihomo.MihomoClient`, whose writes are whitelisted.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.error
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..i18n import t
from .mihomo import MihomoClient
from .probe import NodeResult, Prober, ProbeRun, best_node, provider_nodes
from .store import TrafficStore

if TYPE_CHECKING:
    from ..config import TrafficConfig
    from ..db import Database
    from .gate import TrafficControl

log = logging.getLogger(__name__)

STATE_KEY = "traffic:nodes"
GROUPS = ("PROXY", "TG-PICK")
MODES = ("auto-latency", "auto-speed", "manual")
DEFAULT_MODES = {"PROXY": "auto-latency", "TG-PICK": "auto-speed"}
AUTO_TARGET = {"auto-latency": "AUTO-LATENCY", "auto-speed": "FAST"}
HYSTERESIS = 1.25
"""An automatic switch needs the new node to be this much faster (or the old one dead)."""
HEALTH_EVERY = 60.0
DEAD_RATIO = 0.8
DEAD_FOR = 600.0
FIRST_PROBE_DELAY = 300.0

Notify = Callable[..., Awaitable[None]]


class Busy(Exception):
    """A Telegram upload is in its final part; the exit is not switched now."""


@dataclass
class GroupStatus:
    group: str
    mode: str
    selected: str
    """What the group points at: ``AUTO-LATENCY``, ``FAST`` or a node."""
    node: str
    """The node behind it."""
    latency_ms: int | None
    down_mbps: float | None
    up_mbps: float | None
    price: float | None


def _result(row: dict) -> NodeResult:
    return NodeResult(row["name"], row["price"], row["latency_ms"], row["down_mbps"] or 0.0,
                      row["up_mbps"] or 0.0, bool(row["alive"]), row["tested_at"])


class NodeManager:
    def __init__(self, config: TrafficConfig, client: MihomoClient, store: TrafficStore,
                 db: Database | None, control: TrafficControl, prober: Prober, *,
                 notify: Notify | None = None, clock: Callable[[], float] = time.time) -> None:
        self._config = config
        self._client = client
        self._store = store
        self._db = db
        self._control = control
        self._prober = prober
        self._notify = notify
        self._clock = clock
        self.modes: dict[str, dict[str, Any]] = {}
        self.last_auto_switch = 0.0
        self.incident = False
        self._dead_since: float | None = None
        self._last_health = 0.0
        self._born = clock()
        self._probe_task: asyncio.Task | None = None
        self.last_error = ""
        self.alive_count: tuple[int, int] | None = None
        self.pick_lists: dict[str, list[str]] = {}
        """Per group, the node names the manual picker last showed (callbacks carry an index)."""

    # ------------------------------------------------------------ persistence

    async def load(self) -> None:
        if self._db is None:
            return
        data = await self._db.kv_get_json(STATE_KEY)
        if isinstance(data, dict):
            self.modes = {g: m for g, m in (data.get("modes") or {}).items()
                          if g in GROUPS and m.get("mode") in MODES}
            self.last_auto_switch = float(data.get("last_auto_switch") or 0.0)
            self.incident = bool(data.get("incident"))

    async def _save(self) -> None:
        if self._db is not None:
            await self._db.kv_set_json(STATE_KEY, {
                "modes": self.modes, "last_auto_switch": self.last_auto_switch,
                "incident": self.incident})

    def mode_of(self, group: str) -> tuple[str, str | None]:
        saved = self.modes.get(group)
        if saved:
            return saved["mode"], saved.get("pick")
        return DEFAULT_MODES[group], None

    def _target(self, group: str) -> str | None:
        mode, pick = self.mode_of(group)
        return pick if mode == "manual" else AUTO_TARGET[mode]

    async def apply_saved(self) -> None:
        """Re-apply what the owner chose, at start. Groups never chosen are left alone."""
        for group in self.modes:
            target = self._target(group)
            if not target:
                continue
            try:
                await asyncio.to_thread(self._client.select, group, target)
            except Exception as exc:  # noqa: BLE001 - mihomo may not be up yet
                log.warning("could not re-apply %s=%s at start: %s", group, target, exc)

    # --------------------------------------------------------------- choosing

    async def set_mode(self, group: str, mode: str, pick: str | None = None) -> None:
        """The owner's choice. A manual pick stops auto re-picks for the group."""
        if group not in GROUPS or mode not in MODES or (mode == "manual" and not pick):
            raise ValueError("bad node selection")
        if self._control.upload_final:
            raise Busy()
        self.modes[group] = {"mode": mode, "pick": pick if mode == "manual" else None}
        await self._save()
        if mode == "auto-speed":
            await self._update_fast(force=True)
        target = self._target(group)
        await asyncio.to_thread(self._client.select, group, target)
        if group == "TG-PICK":
            await asyncio.to_thread(self._client.close_telegram_connections)

    async def _update_fast(self, *, force: bool) -> bool:
        """Point ``FAST`` at the best measured node, subject to the auto-switch rules."""
        results = [_result(row) for row in await asyncio.to_thread(self._store.nodes)]
        best = best_node(results)
        if best is None:
            return False
        now = self._clock()
        current = await asyncio.to_thread(self._client.now, "FAST")
        if current == best.name:
            return False
        known = next((r for r in results if r.name == current), None)
        dead = known is None or not known.alive
        if not force:
            if self._control.upload_final:
                return False
            if not dead and known is not None and best.down_mbps < known.down_mbps * HYSTERESIS:
                return False
            if now - self.last_auto_switch < self._config.switch_min_minutes * 60:
                return False
        await asyncio.to_thread(self._client.select, "FAST", best.name)
        if not force:
            self.last_auto_switch = now
            await self._save()
        if await asyncio.to_thread(self._client.now, "TG-PICK") == "FAST":
            await asyncio.to_thread(self._client.close_telegram_connections)
        await self._say(t("nodes.switched", old=current or "?", new=best.name,
                          mbps=f"{best.down_mbps:.0f}"))
        return True

    # ---------------------------------------------------------------- probing

    @property
    def probing(self) -> bool:
        return self._probe_task is not None and not self._probe_task.done()

    async def probe_now(self) -> ProbeRun | None:
        """Measure every node now (also what the scheduled run does)."""
        try:
            run = await self._prober.run_async()
        except Exception as exc:  # noqa: BLE001 - a missing PROBE group must not crash anything
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("node probe failed: %s", self.last_error)
            return None
        self.last_error = ""
        if any(self.mode_of(g)[0] == "auto-speed" for g in GROUPS):
            try:
                await self._update_fast(force=False)
            except Exception:
                log.warning("could not update FAST after a probe", exc_info=True)
        return run

    async def status(self) -> list[GroupStatus]:
        rows = {row["name"]: row for row in await asyncio.to_thread(self._store.nodes)}
        out = []
        for group in GROUPS:
            mode, _pick = self.mode_of(group)
            try:
                selected = await asyncio.to_thread(self._client.now, group)
                node = selected
                if selected in ("AUTO-LATENCY", "FAST"):
                    node = await asyncio.to_thread(self._client.now, selected) or selected
            except Exception:  # noqa: BLE001 - show what is known
                selected = node = ""
            row = rows.get(node) or {}
            out.append(GroupStatus(group, mode, selected, node, row.get("latency_ms"),
                                   row.get("down_mbps"), row.get("up_mbps"), row.get("price")))
        return out

    async def alive_nodes(self) -> list[dict]:
        """Alive nodes with what is known about them, fastest first (for the manual picker)."""
        rows = {row["name"]: row for row in await asyncio.to_thread(self._store.nodes)}
        nodes = await asyncio.to_thread(provider_nodes, self._client)
        merged = [{"name": n["name"], **(rows.get(n["name"]) or {})}
                  for n in nodes if n["alive"]]
        merged.sort(key=lambda r: -(r.get("down_mbps") or 0.0))
        return merged

    # ------------------------------------------------------------------ tick

    async def tick(self, now: float | None = None) -> None:
        now = now if now is not None else self._clock()
        if now - self._last_health >= HEALTH_EVERY:
            self._last_health = now
            await self.health(now)
        hours = self._config.probe_hours
        if hours > 0 and not self.probing and now - self._born >= FIRST_PROBE_DELAY:
            last = await asyncio.to_thread(self._store.last_probe)
            if now - last >= hours * 3600:
                self._probe_task = asyncio.create_task(self.probe_now(), name="node-probe")

    async def stop(self) -> None:
        if self._probe_task is not None:
            self._probe_task.cancel()
            await asyncio.gather(self._probe_task, return_exceptions=True)

    # ---------------------------------------------------------------- health

    async def health(self, now: float) -> None:
        """Subscription health (alert only): a failing provider, or most nodes dead for 10 min."""
        failed = False
        nodes: list[dict] = []
        try:
            nodes = await asyncio.to_thread(provider_nodes, self._client)
            failed = not nodes
        except urllib.error.HTTPError as exc:
            failed = exc.code >= 500
            if not failed:
                return  # no such provider: a configuration matter, not an expired subscription
        except (urllib.error.URLError, OSError, ValueError):
            return  # mihomo itself is not answering: nothing to say about the subscription
        total = len(nodes)
        alive = sum(1 for n in nodes if n["alive"])
        self.alive_count = (alive, total)
        mostly_dead = total > 0 and (total - alive) / total >= DEAD_RATIO
        if mostly_dead:
            self._dead_since = self._dead_since or now
        else:
            self._dead_since = None
        sick = failed or (mostly_dead and now - (self._dead_since or now) >= DEAD_FOR)
        if sick and not self.incident:
            self.incident = True
            await self._save()
            await self._say(t("nodes.health.bad", alive=alive, total=total))
        elif not sick and self.incident:
            self.incident = False
            await self._save()
            await self._say(t("nodes.health.recovered", alive=alive, total=total))

    async def _say(self, text: str, buttons=None) -> None:
        log.info("nodes: %s", text.replace("\n", " "))
        if self._notify is None:
            return
        try:
            await self._notify(text, buttons) if buttons else await self._notify(text)
        except Exception:
            log.warning("could not deliver a node message", exc_info=True)
