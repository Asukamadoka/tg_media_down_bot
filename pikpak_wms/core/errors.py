"""The only exceptions that leave the core layer.

Everything the PikPak SDK or the network can throw is turned into one of
these in :mod:`pikpak_wms.core.client`, so the layers above never need to
know which SDK is underneath.
"""

from __future__ import annotations


class WmsError(RuntimeError):
    """A PikPak operation failed in a way worth telling the user about.

    ``str(exc)`` stays English for logs. When raised with a catalogue
    ``key``, :meth:`display` gives the person's language instead.
    """

    def __init__(self, message: str = "", *, key: str | None = None, **kwargs: object) -> None:
        super().__init__(message or key or "")
        self.key = key
        self.kwargs = kwargs

    def display(self) -> str:
        if self.key is None:
            return str(self)
        from ..i18n import t  # the catalogue imports nothing from here

        return t(self.key, **self.kwargs)


class AuthError(WmsError):
    """Logging in, or refreshing the session, failed."""


class RateLimitedError(WmsError):
    """PikPak kept refusing for going too fast, even after backing off."""


class NotFoundError(WmsError):
    """A path or file the operation needs does not exist."""
