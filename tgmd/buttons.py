"""Inline keyboard construction, isolated because it is layer-specific.

Telegram's schema layer 229, which Telethon 1.45 speaks, replaced the old
family of ``KeyboardButtonUrl`` / ``KeyboardButtonWebView`` constructors with
one ``KeyboardInlineButton`` carrying a separate type object. Code written
against the older names imports fine from memory and then fails at runtime,
so the construction lives here alone and is covered by tests: a Telethon
upgrade that moves it again breaks a test rather than the first button press.
"""

from __future__ import annotations

from telethon.tl.types import (
    InlineButtonTypeCallback,
    InlineButtonTypeWebView,
    KeyboardInlineButton,
    KeyboardInlineButtonRow,
    ReplyInlineMarkup,
)


def webview_button(text: str, url: str) -> ReplyInlineMarkup:
    """A button that opens ``url`` as a Mini App inside the Telegram client.

    Telegram only accepts HTTPS URLs here, and silently does nothing with
    anything else, so callers must not offer this for a plain-HTTP address.
    """
    return ReplyInlineMarkup(
        [
            KeyboardInlineButtonRow(
                [KeyboardInlineButton(text=text, type=InlineButtonTypeWebView(url=url))]
            )
        ]
    )


def callback_buttons(rows: list[list[tuple[str, str]]]) -> ReplyInlineMarkup:
    """Rows of buttons that send ``data`` back to the bot when pressed.

    ``data`` must fit Telegram's 64-byte limit; callers use short ASCII
    prefixes such as ``wms:apply:12``.
    """
    for row in rows:
        for _text, data in row:
            if len(data.encode()) > 64:
                raise ValueError(f"callback data over 64 bytes: {data!r}")
    return ReplyInlineMarkup(
        [
            KeyboardInlineButtonRow(
                [
                    KeyboardInlineButton(
                        text=text, type=InlineButtonTypeCallback(data=data.encode())
                    )
                    for text, data in row
                ]
            )
            for row in rows
        ]
    )
