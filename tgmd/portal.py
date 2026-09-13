"""One-time login links so each user can connect their own PikPak account.

PikPak has no OAuth or device-code flow that this project can use, so
connecting an account means submitting its credentials once. Doing that in a
Telegram chat would leave the password sitting in message history on both
sides, so instead the bot sends a link to a short-lived page it serves itself.

The page is deliberately plain and clearly labelled as belonging to the
operator's own bot. It does not use PikPak's name or styling as if it were
PikPak, because a page that collects credentials must never look like it comes
from someone else. The credentials are used once to obtain a token, and only
the token is stored: see :func:`tgmd.pikpak.strip_credentials`.

Links are refused entirely over cleartext HTTP unless the host is loopback,
since the whole point is to move a password off the chat transcript rather
than onto the wire.
"""

from __future__ import annotations

import html
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from aiohttp import web

from .config import HttpConfig, PikPakConfig
from .miniapp import InitDataError, validate_init_data
from .pikpak import PikPakError, PikPakService
from .signing import TokenError, make_token, verify_token

log = logging.getLogger(__name__)

# A user gets a few tries before the link burns, so one typo is survivable but
# a stolen link is not a password oracle.
MAX_ATTEMPTS = 3

# The Mini App proves identity through Telegram rather than a link, so the
# only limit needed there is one that stops PikPak being hammered.
MINIAPP_ATTEMPT_LIMIT = 8
MINIAPP_ATTEMPT_WINDOW = 600.0

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

_SECURITY_HEADERS = {
    # The URL is a capability; no-referrer stops it leaking to other sites.
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}


class PortalError(RuntimeError):
    """A login link cannot be issued, with a reason meant for the user."""


@dataclass
class PendingLogin:
    """A link that has been issued but not yet used."""

    user_id: int
    expires_at: float
    attempts_left: int = MAX_ATTEMPTS

    @property
    def expired(self) -> bool:
        return self.expires_at <= time.time()


@dataclass
class _Page:
    """Rendered response body plus its HTTP status."""

    body: str
    status: int = 200


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


def _shell(title: str, inner: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f'<body><main class="card">{inner}</main></body></html>'
    )


def render_form(action: str, *, error: str | None = None) -> str:
    """The credential form. ``action`` must be the same one-time URL."""
    error_block = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return _shell(
        "Connect PikPak — tg_media_down_bot",
        "<h1>Connect your PikPak account</h1>"
        '<p class="sub">This page is served by your own media-downloader bot. '
        "It is not operated by PikPak.</p>"
        f'<form method="post" action="{html.escape(action)}">'
        '<label for="username">PikPak email or phone</label>'
        '<input id="username" name="username" type="text" autocomplete="username" '
        'autocapitalize="none" spellcheck="false" required autofocus>'
        '<label for="password">PikPak password</label>'
        '<input id="password" name="password" type="password" '
        'autocomplete="current-password" required>'
        "<button type=\"submit\">Connect</button>"
        "</form>"
        f"{error_block}"
        '<p class="note">Your credentials are sent to PikPak once to obtain an '
        "access token. Only that token is saved, never your password. The link "
        "to this page works once and expires shortly. Revoke access at any "
        "time with <code>/pikpak logout</code>.</p>",
    )


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


def render_result(title: str, message: str, *, good: bool) -> str:
    """A terminal page: either connected, or a refusal."""
    return _shell(
        f"{title} — tg_media_down_bot",
        f'<p class="ok">{"✅" if good else "❌"}</p>'
        f"<h1>{html.escape(title)}</h1>"
        f'<p class="sub">{html.escape(message)}</p>',
    )


def is_secure_base_url(base_url: str) -> bool:
    """True when a password may be collected through this address.

    HTTPS always qualifies. Plain HTTP qualifies only on loopback, which keeps
    local development workable without inviting a password over the network.
    """
    parsed = urlparse(base_url)
    if parsed.scheme == "https":
        return True
    if parsed.scheme != "http":
        return False
    return (parsed.hostname or "") in _LOOPBACK_HOSTS


class PikPakLoginPortal:
    """Issues and serves one-time PikPak login links."""

    def __init__(
        self,
        pikpak: PikPakService,
        pikpak_config: PikPakConfig,
        http_config: HttpConfig,
        secret: str,
        *,
        bot_token: str = "",
    ) -> None:
        self._pikpak = pikpak
        self._pikpak_config = pikpak_config
        self._http = http_config
        self._secret = secret
        self._bot_token = bot_token
        self._pending: dict[str, PendingLogin] = {}
        self._miniapp_attempts: dict[int, list[float]] = {}
        self._running = False

    # ------------------------------------------------------------- lifecycle

    def register(self, router: web.UrlDispatcher) -> None:
        """Attach the portal's routes to a running application."""
        router.add_get("/pikpak/login/{token}", self._handle_form)
        router.add_post("/pikpak/login/{token}", self._handle_submit)
        router.add_get("/pikpak/app", self._handle_miniapp)
        router.add_post("/pikpak/app", self._handle_miniapp_submit)
        self._running = True

    @property
    def miniapp_url(self) -> str | None:
        """URL for the Mini App button, or None when it cannot be offered.

        Telegram only opens ``web_app`` buttons over HTTPS, so a loopback or
        plain-HTTP deployment gets the one-time link instead.
        """
        if not self._pikpak_config.allow_user_login or not self._running:
            return None
        base = self._http.base_url
        if not base or not base.startswith("https://"):
            return None
        return f"{base}/pikpak/app"

    @property
    def enabled(self) -> bool:
        """True when links can be issued at all."""
        return (
            self._pikpak_config.allow_user_login
            and self._running
            and self._http.usable
        )

    def unavailable_reason(self) -> str | None:
        """Why a link cannot be issued right now, or None when it can."""
        if not self._pikpak_config.allow_user_login:
            return (
                "the operator has disabled per-user PikPak logins "
                "(pikpak.allow_user_login)"
            )
        if not self._http.usable:
            return (
                "the bot's HTTP server is not running with a public address, so "
                "there is nowhere to serve a login page. Set HTTP_ENABLED=true "
                "and PUBLIC_BASE_URL."
            )
        if not self._running:
            return "the login portal is not attached to the HTTP server"
        if not is_secure_base_url(self._http.base_url):
            return (
                f"PUBLIC_BASE_URL is {self._http.base_url}, which is plain HTTP. "
                "A login page must be served over HTTPS; put a TLS reverse "
                "proxy in front of the bot."
            )
        return None

    # ---------------------------------------------------------------- issuing

    def create_link(self, user_id: int) -> str:
        """Issue a one-time login URL for ``user_id``."""
        reason = self.unavailable_reason()
        if reason is not None:
            raise PortalError(reason)

        self._sweep()
        # Only one live link per user: issuing a new one invalidates the old.
        for nonce in [n for n, p in self._pending.items() if p.user_id == user_id]:
            self._pending.pop(nonce, None)

        nonce = secrets.token_urlsafe(16)
        ttl = self._pikpak_config.login_link_ttl
        expires_at = time.time() + ttl
        self._pending[nonce] = PendingLogin(user_id=user_id, expires_at=expires_at)
        token = make_token(self._secret, f"login:{user_id}:{nonce}", int(expires_at))
        return f"{self._http.base_url}/pikpak/login/{token}"

    def revoke(self, user_id: int) -> None:
        """Drop any outstanding link for a user."""
        for nonce in [n for n, p in self._pending.items() if p.user_id == user_id]:
            self._pending.pop(nonce, None)

    def _sweep(self) -> None:
        for nonce in [n for n, p in self._pending.items() if p.expired]:
            self._pending.pop(nonce, None)

    # --------------------------------------------------------------- serving

    def _resolve(self, token: str) -> tuple[str, PendingLogin]:
        """Validate a token and return its nonce and pending record."""
        try:
            payload = verify_token(self._secret, token)
        except TokenError as exc:
            raise PortalError(str(exc)) from None

        parts = payload.split(":")
        if len(parts) != 3 or parts[0] != "login":
            raise PortalError("that is not a login link")
        try:
            user_id = int(parts[1])
        except ValueError:
            raise PortalError("that login link is malformed") from None

        nonce = parts[2]
        pending = self._pending.get(nonce)
        if pending is None:
            raise PortalError(
                "this link has already been used, or the bot has restarted. "
                "Send /pikpak login again for a fresh one."
            )
        if pending.expired:
            self._pending.pop(nonce, None)
            raise PortalError("this link has expired. Send /pikpak login again.")
        if pending.user_id != user_id:
            self._pending.pop(nonce, None)
            raise PortalError("that login link is malformed")
        return nonce, pending

    @staticmethod
    def _respond(page: _Page) -> web.Response:
        return web.Response(
            text=page.body,
            status=page.status,
            content_type="text/html",
            charset="utf-8",
            headers=_SECURITY_HEADERS,
        )

    async def _handle_form(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        try:
            self._resolve(token)
        except PortalError as exc:
            return self._respond(
                _Page(render_result("Link unavailable", str(exc), good=False), 410)
            )
        return self._respond(_Page(render_form(str(request.rel_url))))

    # --------------------------------------------------------------- Mini App

    async def _handle_miniapp(self, _request: web.Request) -> web.Response:
        return self._respond(_Page(render_miniapp()))

    def _miniapp_throttled(self, user_id: int) -> bool:
        """True when this user has tried too often recently."""
        now = time.time()
        attempts = [
            stamp
            for stamp in self._miniapp_attempts.get(user_id, [])
            if now - stamp < MINIAPP_ATTEMPT_WINDOW
        ]
        self._miniapp_attempts[user_id] = attempts
        return len(attempts) >= MINIAPP_ATTEMPT_LIMIT

    async def _handle_miniapp_submit(self, request: web.Request) -> web.Response:
        """Log in using the identity Telegram signed into initData."""

        def problem(message: str, status: int) -> web.Response:
            return web.json_response(
                {"ok": False, "error": message}, status=status, headers=_SECURITY_HEADERS
            )

        if not self._pikpak_config.allow_user_login:
            return problem("Per-user PikPak logins are disabled.", 403)

        try:
            body = await request.json()
        except Exception:
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
        if self._miniapp_throttled(user_id):
            return problem("Too many attempts. Wait a few minutes.", 429)
        self._miniapp_attempts.setdefault(user_id, []).append(time.time())

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

    # ------------------------------------------------------- one-time link

    async def _handle_submit(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        try:
            nonce, pending = self._resolve(token)
        except PortalError as exc:
            return self._respond(
                _Page(render_result("Link unavailable", str(exc), good=False), 410)
            )

        form = await request.post()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        action = str(request.rel_url)

        if not username or not password:
            return self._respond(
                _Page(render_form(action, error="Enter both fields."), 400)
            )

        try:
            await self._pikpak.login_with_password(pending.user_id, username, password)
        except PikPakError as exc:
            pending.attempts_left -= 1
            if pending.attempts_left <= 0:
                self._pending.pop(nonce, None)
                log.info(
                    "login link for user %s burned after failed attempts",
                    pending.user_id,
                )
                return self._respond(
                    _Page(
                        render_result(
                            "Too many attempts",
                            "This link is no longer valid. Send /pikpak login "
                            "again for a fresh one.",
                            good=False,
                        ),
                        429,
                    )
                )
            remaining = pending.attempts_left
            return self._respond(
                _Page(
                    render_form(
                        action,
                        error=f"{exc} — {remaining} attempt(s) left.",
                    ),
                    401,
                )
            )

        # Success: the link is spent.
        self._pending.pop(nonce, None)
        return self._respond(
            _Page(
                render_result(
                    "PikPak connected",
                    "Transfers will now go to your own PikPak account. You can "
                    "close this page and return to Telegram.",
                    good=True,
                )
            )
        )
