"""The PikPak login Mini App, so each user can connect their own account.

PikPak has no OAuth or device-code flow that this project can use, so
connecting an account means submitting its credentials once. Doing that in a
Telegram chat leaves the password in message history for a moment, so the
preferred path is a Mini App: a small form Telegram opens inside its own
client, served by this bot over HTTPS. Telegram signs who opened it (see
:mod:`tgmd.miniapp`), so there is no login link that could leak.

The page is deliberately plain and clearly labelled as belonging to the
operator's own bot. It does not use PikPak's name or styling as if it were
PikPak, because a page that collects credentials must never look like it comes
from someone else. The credentials are used once to obtain a token, and only
the token is stored: see :func:`tgmd.pikpak.strip_credentials`.

Where the Mini App cannot be offered (no HTTPS address), ``/setup pikpak``
collects the same two fields in chat instead. A third path, a one-time login
link, was removed: it needed the same HTTPS address as the Mini App, so on
any real deployment it was never the one offered.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from aiohttp import web

from .config import HttpConfig, PikPakConfig
from .miniapp import InitDataError, validate_init_data
from .pikpak import PikPakError, PikPakService

log = logging.getLogger(__name__)

# Telegram proves who is submitting, so the only limit needed is one that
# stops PikPak being hammered with guesses.
MINIAPP_ATTEMPT_LIMIT = 8
MINIAPP_ATTEMPT_WINDOW = 600.0

_SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}


_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px;
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  background: #f6f6f7; color: #16181d;
}
@media (prefers-color-scheme: dark) {
  body { background: #15171c; color: #e9eaee; }
  .card { background: #1e2128; border-color: #2d323c; }
  input { background: #15171c; color: #e9eaee; border-color: #39404c; }
  .note { background: #21252e; }
}
.card {
  max-width: 27rem; margin: 6vh auto; padding: 24px;
  background: #fff; border: 1px solid #e2e4e9; border-radius: 12px;
}
h1 { margin: 0 0 4px; font-size: 1.15rem; }
.sub { margin: 0 0 20px; font-size: .85rem; opacity: .7; }
label { display: block; margin: 14px 0 5px; font-weight: 600; font-size: .85rem; }
input {
  width: 100%; padding: 10px 12px; font-size: 1rem;
  border: 1px solid #cfd3da; border-radius: 8px; background: #fff; color: inherit;
}
button {
  width: 100%; margin-top: 20px; padding: 11px;
  font-size: 1rem; font-weight: 600; color: #fff;
  background: #2b6cb0; border: 0; border-radius: 8px; cursor: pointer;
}
button:hover { background: #245a94; }
.note {
  margin-top: 20px; padding: 12px; border-radius: 8px;
  background: #f0f1f4; font-size: .8rem; line-height: 1.5; opacity: .9;
}
.error {
  margin: 16px 0 0; padding: 11px 12px; border-radius: 8px;
  background: #fdeaea; border: 1px solid #f5c2c2; color: #8a1f1f; font-size: .87rem;
}
@media (prefers-color-scheme: dark) {
  .error { background: #3a1d1d; border-color: #5c2a2a; color: #ffb4b4; }
}
.ok { font-size: 2rem; }
code { font-size: .85em; }
"""


_MINIAPP_SCRIPT = """
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const form = document.getElementById('f');
const errorBox = document.getElementById('e');
const button = document.getElementById('b');
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  errorBox.textContent = '';
  button.disabled = true;
  button.textContent = 'Connecting…';
  try {
    const response = await fetch(window.location.pathname, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        initData: tg ? tg.initData : '',
        username: form.username.value,
        password: form.password.value
      })
    });
    const result = await response.json();
    if (response.ok && result.ok) {
      document.body.innerHTML =
        '<main class="card"><p class="ok">\\u2705</p><h1>PikPak connected</h1>' +
        '<p class="sub">Transfers now go to your own PikPak account.</p></main>';
      if (tg) { setTimeout(() => tg.close(), 1600); }
      return;
    }
    errorBox.textContent = result.error || 'That did not work.';
  } catch (problem) {
    errorBox.textContent = 'Could not reach the bot: ' + problem;
  }
  button.disabled = false;
  button.textContent = 'Connect';
});
"""


def render_miniapp() -> str:
    """The Mini App page, which Telegram opens inside its own client.

    Identity comes from Telegram's signed initData rather than a one-time
    link, so there is no token in the URL at all.
    """
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        "<title>Connect PikPak</title>"
        f"<style>{_STYLE}</style>"
        '<script src="https://telegram.org/js/telegram-web-app.js"></script>'
        "</head><body>"
        '<main class="card">'
        "<h1>Connect your PikPak account</h1>"
        '<p class="sub">This page belongs to your own media-downloader bot. '
        "It is not operated by PikPak.</p>"
        '<form id="f">'
        '<label for="username">PikPak email or phone</label>'
        '<input id="username" name="username" type="text" autocomplete="username" '
        'autocapitalize="none" spellcheck="false" required>'
        '<label for="password">PikPak password</label>'
        '<input id="password" name="password" type="password" '
        'autocomplete="current-password" required>'
        '<button id="b" type="submit">Connect</button>'
        "</form>"
        '<p class="error" id="e" style="background:none;border:0;padding:0"></p>'
        '<p class="note">Telegram tells me who you are, so there is no login '
        "link to leak. Your password is sent to PikPak once to obtain an "
        "access token; only the token is stored. Disconnect any time with "
        "<code>/pikpak logout</code>.</p>"
        "</main>"
        f"<script>{_MINIAPP_SCRIPT}</script>"
        "</body></html>"
    )


class PikPakLoginPortal:
    """Serves the PikPak login Mini App on the bot's HTTP server."""

    def __init__(
        self,
        pikpak: PikPakService,
        pikpak_config: PikPakConfig,
        http_config: HttpConfig,
        *,
        bot_token: str,
        is_allowed: Callable[[int], bool],
    ) -> None:
        self._pikpak = pikpak
        self._pikpak_config = pikpak_config
        self._http = http_config
        self._bot_token = bot_token
        # Telegram's signature proves who is submitting, not that they may
        # use this bot. Every other write goes through the handlers' access
        # check, so this one must too.
        self._is_allowed = is_allowed
        self._attempts: dict[int, list[float]] = {}
        self._running = False

    def register(self, router: web.UrlDispatcher) -> None:
        """Attach the Mini App's routes to a running application."""
        router.add_get("/pikpak/app", self._handle_page)
        router.add_post("/pikpak/app", self._handle_submit)
        self._running = True

    def unavailable_reason(self) -> str | None:
        """Why the Mini App cannot be offered right now, or None when it can."""
        if not self._pikpak_config.allow_user_login:
            return (
                "the operator has disabled per-user PikPak logins "
                "(pikpak.allow_user_login)"
            )
        if not self._http.usable:
            return (
                "the bot's HTTP server is not running with a public address, so "
                "there is nowhere to serve the login form. Set HTTP_ENABLED=true "
                "and PUBLIC_BASE_URL."
            )
        if not self._running:
            return "the login form is not attached to the HTTP server"
        if not self._http.base_url.startswith("https://"):
            return (
                f"PUBLIC_BASE_URL is {self._http.base_url}, which is plain HTTP. "
                "Telegram only opens Mini Apps over HTTPS; put a TLS reverse "
                "proxy in front of the bot."
            )
        return None

    @property
    def miniapp_url(self) -> str | None:
        """URL for the Mini App button, or None when it cannot be offered."""
        if self.unavailable_reason() is not None:
            return None
        return f"{self._http.base_url}/pikpak/app"

    async def _handle_page(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=render_miniapp(),
            content_type="text/html",
            charset="utf-8",
            headers=_SECURITY_HEADERS,
        )

    def _throttled(self, user_id: int) -> bool:
        """True when this user has tried too often recently."""
        now = time.time()
        attempts = [
            stamp
            for stamp in self._attempts.get(user_id, [])
            if now - stamp < MINIAPP_ATTEMPT_WINDOW
        ]
        self._attempts[user_id] = attempts
        return len(attempts) >= MINIAPP_ATTEMPT_LIMIT

    async def _handle_submit(self, request: web.Request) -> web.Response:
        """Log in using the identity Telegram signed into initData."""

        def problem(message: str, status: int) -> web.Response:
            return web.json_response(
                {"ok": False, "error": message}, status=status, headers=_SECURITY_HEADERS
            )

        if not self._pikpak_config.allow_user_login:
            return problem("Per-user PikPak logins are disabled.", 403)

        try:
            body = await request.json()
        except ValueError:  # JSONDecodeError; the server side checks no content type
            return problem("Malformed request.", 400)
        if not isinstance(body, dict):
            return problem("Malformed request.", 400)

        try:
            init_data = validate_init_data(
                str(body.get("initData") or ""), self._bot_token
            )
        except InitDataError as exc:
            log.info("rejected a Mini App submission: %s", exc)
            return problem(
                "Telegram could not confirm who you are. Reopen this page from "
                "the bot.",
                401,
            )

        user_id = init_data.user.id
        if not self._is_allowed(user_id):
            log.info("refused a Mini App login from user %s, who is not allowed", user_id)
            return problem("You are not allowed to use this bot.", 403)
        if self._throttled(user_id):
            return problem("Too many attempts. Wait a few minutes.", 429)
        self._attempts.setdefault(user_id, []).append(time.time())

        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if not username or not password:
            return problem("Enter both fields.", 400)

        try:
            await self._pikpak.login_with_password(user_id, username, password)
        except PikPakError as exc:
            return problem(str(exc), 401)

        log.info("user %s connected PikPak through the Mini App", user_id)
        return web.json_response({"ok": True}, headers=_SECURITY_HEADERS)
