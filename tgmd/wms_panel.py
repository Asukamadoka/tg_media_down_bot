"""The warehouse panel: a Telegram Mini App for admins (WMS M4).

It shows the plans waiting for confirmation, lets an admin read one and
apply or discard it, and lists the audit trail with an undo for each entry.
Nothing here decides anything: every button calls the same WMS pipeline as
``wms apply`` / ``wms undo`` (rule 6), through :class:`tgmd.wms.WmsInBot`.

Identity is Telegram's signed ``initData``, checked exactly as the PikPak
login Mini App checks it, and only admins get past it. Permanent deletion
is not offered at all: a plan containing one is refused (rule 2).

The page's text is the bot's catalogue, handed to the script as one JSON
object, so nothing a translation contains can break out of the script.
"""

from __future__ import annotations

import json
import logging
from html import escape
from typing import Any

from aiohttp import web

from pikpak_wms.ops.embed import WmsError

from . import i18n
from .config import Config
from .i18n import t
from .miniapp import InitDataError, validate_init_data
from .wms import WmsInBot

log = logging.getLogger(__name__)

PAGE_PATH = "/wms/app"
API_PATH = "/wms/api"

_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "X-Content-Type-Options": "nosniff",
}

# Keys the page needs, handed over once as JSON.
_PAGE_KEYS = (
    "wms.panel.title", "wms.panel.tab_plans", "wms.panel.tab_audit", "wms.panel.loading",
    "wms.panel.no_plans", "wms.panel.no_audit", "wms.panel.view", "wms.panel.apply",
    "wms.panel.discard", "wms.panel.undo", "wms.panel.back", "wms.panel.confirm_apply",
    "wms.panel.confirm_discard", "wms.panel.confirm_undo", "wms.panel.done",
    "wms.panel.unreachable", "wms.panel.status",
)

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 14px; font: 15px/1.5 system-ui, -apple-system, sans-serif;
  background: var(--tg-theme-bg-color, #f6f6f7); color: var(--tg-theme-text-color, #16181d); }
h1 { font-size: 1.1rem; margin: 0 0 4px; }
.sub { margin: 0 0 12px; font-size: .8rem; opacity: .7; }
nav { display: flex; gap: 8px; margin-bottom: 12px; }
nav button, .row button { border: 0; border-radius: 8px; padding: 7px 12px; font-size: .9rem;
  background: var(--tg-theme-secondary-bg-color, #e6e8ec); color: inherit; }
nav button.on, .row button.main { background: var(--tg-theme-button-color, #2b6cb0);
  color: var(--tg-theme-button-text-color, #fff); }
.item { padding: 10px 12px; margin-bottom: 8px; border-radius: 10px;
  background: var(--tg-theme-secondary-bg-color, #fff); }
.item p { margin: 0 0 6px; }
.row { display: flex; gap: 6px; flex-wrap: wrap; }
pre { white-space: pre-wrap; word-break: break-all; font-size: .78rem; margin: 0 0 10px; }
.msg { padding: 10px; border-radius: 8px; margin-bottom: 10px; font-size: .88rem;
  background: var(--tg-theme-secondary-bg-color, #eef); }
"""

_SCRIPT = """
const S = JSON.parse(document.getElementById('strings').textContent);
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const main = document.getElementById('main');
const note = document.getElementById('note');
const el = (tag, text, cls) => { const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
async function call(action, extra) {
  const r = await fetch('API_PATH', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign({initData: tg ? tg.initData : '', action}, extra || {}))});
  const body = await r.json().catch(() => ({ok: false, error: S['wms.panel.unreachable']}));
  if (!body.ok) throw new Error(body.error || S['wms.panel.unreachable']);
  return body;
}
function say(text) { note.textContent = text || ''; note.style.display = text ? '' : 'none'; }
function button(text, handler, cls) {
  const b = el('button', text, cls); b.onclick = handler; return b;
}
async function guarded(work) { try { await work(); } catch (e) { say(e.message); } }
function confirmThen(question, work) {
  if (tg && tg.showConfirm) { tg.showConfirm(question, ok => ok && guarded(work)); }
  else if (window.confirm(question)) { guarded(work); }
}
async function showPlans() {
  tab('plans'); main.textContent = S['wms.panel.loading'];
  const {plans, status} = await call('plans');
  main.textContent = '';
  main.appendChild(el('p', status, 'sub'));
  if (!plans.length) { main.appendChild(el('p', S['wms.panel.no_plans'])); return; }
  for (const p of plans) {
    const box = el('div', undefined, 'item');
    box.appendChild(el('p', p.header));
    const made = p.created_at.slice(0, 16).replace('T', ' ');
    box.appendChild(el('p', p.status_text + ' · ' + made, 'sub'));
    const row = el('div', undefined, 'row');
    row.appendChild(button(S['wms.panel.view'], () => guarded(() => showPlan(p.id))));
    box.appendChild(row); main.appendChild(box);
  }
}
async function showPlan(id) {
  const {lines} = await call('plan', {id});
  main.textContent = '';
  main.appendChild(el('pre', lines.join('\\n')));
  const row = el('div', undefined, 'row');
  row.appendChild(button(S['wms.panel.apply'], () => confirmThen(S['wms.panel.confirm_apply'],
    async () => {
      const r = await call('apply', {id}); say(r.summary); await showPlan(id);
    }), 'main'));
  row.appendChild(button(S['wms.panel.discard'], () => confirmThen(S['wms.panel.confirm_discard'],
    async () => { await call('discard', {id}); say(S['wms.panel.done']); await showPlans(); })));
  row.appendChild(button(S['wms.panel.back'], () => guarded(showPlans)));
  main.appendChild(row);
}
async function showAudit() {
  tab('audit'); main.textContent = S['wms.panel.loading'];
  const {entries} = await call('audit');
  main.textContent = '';
  if (!entries.length) { main.appendChild(el('p', S['wms.panel.no_audit'])); return; }
  for (const e of entries) {
    const box = el('div', undefined, 'item');
    box.appendChild(el('p', '#' + e.id + '  ' + e.what));
    box.appendChild(el('p', e.at.slice(0, 16).replace('T', ' ') + ' UTC · ' + e.rule_name, 'sub'));
    const row = el('div', undefined, 'row');
    row.appendChild(button(S['wms.panel.undo'], () => guarded(async () => {
      const preview = await call('undo_preview', {id: e.id});
      confirmThen(S['wms.panel.confirm_undo'] + '\\n' + preview.what, async () => {
        await call('undo', {id: e.id}); say(S['wms.panel.done']); await showAudit(); });
    })));
    box.appendChild(row); main.appendChild(box);
  }
}
function tab(name) {
  document.getElementById('t-plans').className = name === 'plans' ? 'on' : '';
  document.getElementById('t-audit').className = name === 'audit' ? 'on' : '';
}
document.getElementById('t-plans').onclick = () => { say(''); guarded(showPlans); };
document.getElementById('t-audit').onclick = () => { say(''); guarded(showAudit); };
say(''); guarded(showPlans);
"""


def render_panel() -> str:
    strings = {key: t(key) for key in _PAGE_KEYS}
    # "</" cannot appear inside the JSON, so the script element cannot be closed early.
    payload = json.dumps(strings, ensure_ascii=False).replace("</", "<\\/")
    script = _SCRIPT.replace("API_PATH", API_PATH)
    return (
        f'<!doctype html><html lang="{i18n.language()}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        f"<title>{escape(t('wms.panel.title'))}</title>"
        f"<style>{_STYLE}</style>"
        '<script src="https://telegram.org/js/telegram-web-app.js"></script>'
        "</head><body>"
        f"<h1>{escape(t('wms.panel.title'))}</h1>"
        f"<p class=\"sub\">{escape(t('wms.panel.subtitle'))}</p>"
        f"<nav><button id=\"t-plans\" class=\"on\">{escape(t('wms.panel.tab_plans'))}</button>"
        f"<button id=\"t-audit\">{escape(t('wms.panel.tab_audit'))}</button></nav>"
        '<div class="msg" id="note" style="display:none"></div>'
        '<div id="main"></div>'
        f'<script type="application/json" id="strings">{payload}</script>'
        f"<script>{script}</script>"
        "</body></html>"
    )


class WmsPanel:
    """Serves the panel on the bot's HTTP server."""

    def __init__(self, config: Config, wms: WmsInBot) -> None:
        self._config = config
        self._wms = wms
        self._running = False

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get(PAGE_PATH, self._handle_page)
        router.add_post(API_PATH, self._handle_api)
        self._running = True

    def unavailable_reason(self) -> str | None:
        """Why the panel cannot be offered right now (for a person), or None."""
        http = self._config.http
        if not self._config.wms.enabled:
            return t("wms.off")
        if not http.usable or not self._running:
            return t("wms.panel.no_http")
        if not http.base_url.startswith("https://"):
            return t("wms.panel.no_https", url=http.base_url)
        return None

    @property
    def url(self) -> str | None:
        if self.unavailable_reason() is not None:
            return None
        return f"{self._config.http.base_url}{PAGE_PATH}"

    async def _handle_page(self, _request: web.Request) -> web.Response:
        return web.Response(text=render_panel(), content_type="text/html", charset="utf-8",
                            headers=_HEADERS)

    async def _handle_api(self, request: web.Request) -> web.Response:
        def reply(code: int = 200, **body: Any) -> web.Response:
            body.setdefault("ok", code == 200)
            return web.json_response(body, status=code, headers=_HEADERS)

        try:
            body = await request.json()
        except ValueError:
            return reply(400, error=t("wms.panel.bad_request"))
        if not isinstance(body, dict):
            return reply(400, error=t("wms.panel.bad_request"))
        try:
            data = validate_init_data(str(body.get("initData") or ""),
                                      self._config.telegram.bot_token)
        except InitDataError as exc:
            log.info("rejected a WMS panel request: %s", exc)
            return reply(401, error=t("wms.panel.who"))
        user_id = data.user.id
        if not self._config.access.is_admin(user_id):
            log.info("refused the WMS panel to user %s, who is not an admin", user_id)
            return reply(403, error=t("wms.panel.admins_only"))
        embedded = self._wms.embedded
        if embedded is None:
            return reply(503, error=t("wms.off"))

        action = str(body.get("action") or "")
        try:
            item = int(body.get("id") or 0)
        except (TypeError, ValueError):
            return reply(400, error=t("wms.panel.bad_request"))
        try:
            if action == "plans":
                status = await embedded.status()
                return reply(plans=await embedded.open_plans(),
                             status=t("wms.panel.status", files=status["files"],
                                      when=(status["last_stocktake"] or "-")[:16]
                                      .replace("T", " "),
                                      open=status["open_plans"]))
            if action == "plan":
                return reply(lines=await embedded.plan_lines(item))
            if action == "apply":
                report = await embedded.apply(item)
                log.info("admin %s applied WMS plan %s from the panel", user_id, item)
                return reply(summary=report.summary(), outputs=report.outputs[:50])
            if action == "discard":
                await embedded.discard(item)
                return reply()
            if action == "audit":
                entries = await embedded.audit(limit=30)
                return reply(entries=[
                    {key: entry[key] for key in ("id", "what", "at", "rule_name")}
                    for entry in entries
                ])
            if action in ("undo_preview", "undo"):
                outcome = await embedded.undo(item, apply_now=action == "undo")
                if outcome.applied:
                    log.info("admin %s undid WMS audit entry %s from the panel", user_id, item)
                return reply(what=outcome.action.describe(), applied=outcome.applied)
        except WmsError as exc:
            return reply(409, error=exc.display())
        return reply(400, error=t("wms.panel.bad_request"))
