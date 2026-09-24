"""The only exceptions that leave the core layer.

Everything the PikPak SDK or the network can throw is turned into one of
these in :mod:`pikpak_wms.core.client`, so the layers above never need to
know which SDK is underneath.
"""

from __future__ import annotations


class WmsError(RuntimeError):
    """A PikPak operation failed in a way worth telling the user about."""


class AuthError(WmsError):
    """Logging in, or refreshing the session, failed."""


class RateLimitedError(WmsError):
    """PikPak kept refusing for going too fast, even after backing off."""


class NotFoundError(WmsError):
    """A path or file the operation needs does not exist."""
