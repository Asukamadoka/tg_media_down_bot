"""Signed links never leave in text (docs/wms/M9.3 §A.7).

A PikPak download link carries ``sign``, ``pr``, ``userid`` and ``fileid`` in its
query: whoever reads it can fetch the file. :func:`redact` turns every
``https://host/path?query`` into ``https://host/…?<redacted>``; text without a
query is left alone. aiohttp's ``ClientResponseError`` puts the full URL in its
message, so exception text is passed through here before it is logged, stored in a
plan result or shown. :func:`install_log_redaction` does it for every log record.
"""

from __future__ import annotations

import logging
import re
import traceback

_URL = re.compile(r"""(?P<scheme>https?://)(?P<host>[^\s/?#'")>\]]+)(?P<path>[^\s?#'")>\]]*)\?(?P<query>[^\s'")>\]]*)""")


def redact(text: str) -> str:
    """``text`` with the query of every URL in it replaced."""
    if "?" not in text or "://" not in text:
        return text
    return _URL.sub(lambda m: f"{m['scheme']}{m['host']}/…?<redacted>", text)


_installed = False


def install_log_redaction() -> None:
    """Make every log record (message and traceback) pass through :func:`redact`."""
    global _installed
    if _installed:
        return
    _installed = True
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string is the caller's problem
            return record
        if "://" in message and "?" in message:
            record.msg, record.args = redact(message), ()
        if record.exc_info and record.exc_info[0] is not None:
            text = "".join(traceback.format_exception(*record.exc_info))
            if "://" in text and "?" in text:
                record.exc_text = redact(text).rstrip("\n")
        return record

    logging.setLogRecordFactory(factory)
