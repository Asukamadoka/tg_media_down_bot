"""Turning a parsed link into actual Telegram messages.

This is where the awkward parts of the Telegram API live: private chats that
can only be addressed once they are in the session cache, invite links that
have to be joined first, album items that are separate messages sharing a
``grouped_id``, and comment links that point into a linked discussion group.
"""

from __future__ import annotations

import logging
import time

from telethon import TelegramClient
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    MsgIdInvalidError,
    UserAlreadyParticipantError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    GetDiscussionMessageRequest,
    ImportChatInviteRequest,
)
from telethon.tl.types import ChatInviteAlready, ChatInvitePeek, PeerChannel

from .links import MessageRef, to_peer_id

log = logging.getLogger(__name__)

# Album items sit within a few ids of each other; this window is generous
# enough for the 10-item maximum Telegram allows.
_ALBUM_WINDOW = 12

# Re-listing dialogs is expensive on large accounts, so it is rate limited.
_DIALOG_PRIME_INTERVAL = 600.0


class ResolveError(RuntimeError):
    """The link cannot be resolved, with a message meant for the user."""


class Resolver:
    """Resolves :class:`MessageRef` values against a connected client."""

    def __init__(self, client: TelegramClient, *, auto_join: bool = False) -> None:
        self._client = client
        self._auto_join = auto_join
        self._dialogs_primed_at = 0.0

    # -------------------------------------------------------------- entities

    async def _prime_dialogs(self) -> None:
        """Populate the session's entity cache by walking the dialog list.

        Telethon can only turn a bare ``PeerChannel`` into a usable peer if it
        has seen that channel before. Listing dialogs once puts every joined
        chat into the cache, which is what makes ``t.me/c/...`` links work.
        """
        now = time.monotonic()
        if now - self._dialogs_primed_at < _DIALOG_PRIME_INTERVAL:
            return
        self._dialogs_primed_at = now
        count = 0
        async for _ in self._client.iter_dialogs():
            count += 1
        log.debug("primed entity cache from %d dialogs", count)

    async def _join_invite(self, invite_hash: str):
        """Resolve an invite hash to a chat, joining it if allowed."""
        try:
            invite = await self._client(CheckChatInviteRequest(hash=invite_hash))
        except (InviteHashInvalidError, InviteHashExpiredError) as exc:
            raise ResolveError(
                "that invite link is invalid or has expired"
            ) from exc

        if isinstance(invite, (ChatInviteAlready, ChatInvitePeek)):
            return invite.chat

        title = getattr(invite, "title", "that chat")
        if not self._auto_join:
            raise ResolveError(
                f"the account is not a member of “{title}”. Join it first, or "
                "enable download.auto_join_invites."
            )

        try:
            updates = await self._client(ImportChatInviteRequest(hash=invite_hash))
        except UserAlreadyParticipantError:
            invite = await self._client(CheckChatInviteRequest(hash=invite_hash))
            return getattr(invite, "chat", None)
        except (InviteHashInvalidError, InviteHashExpiredError) as exc:
            raise ResolveError("that invite link is invalid or has expired") from exc

        chats = getattr(updates, "chats", None) or []
        if not chats:
            raise ResolveError(f"joined “{title}” but Telegram returned no chat")
        log.info("joined chat via invite link: %s", title)
        return chats[0]

    async def entity(self, ref: MessageRef):
        """Resolve the chat a reference points at."""
        if ref.invite_hash:
            return await self._join_invite(ref.invite_hash)

        if ref.is_private:
            peer = PeerChannel(int(ref.chat))
            try:
                return await self._client.get_entity(peer)
            except ChannelPrivateError as exc:
                raise ResolveError(
                    "that chat is private and the account is not a member of it"
                ) from exc
            except (ValueError, ChannelInvalidError):
                # Not in the cache yet: list dialogs once, then try again.
                await self._prime_dialogs()
            try:
                return await self._client.get_entity(peer)
            except ChannelPrivateError as exc:
                raise ResolveError(
                    "that chat is private and the account is not a member of it"
                ) from exc
            except (ValueError, ChannelInvalidError) as exc:
                raise ResolveError(
                    f"chat {to_peer_id(int(ref.chat))} is not reachable. The "
                    "account reading messages must be a member of it."
                ) from exc

        try:
            return await self._client.get_entity(ref.chat)
        except (UsernameNotOccupiedError, UsernameInvalidError) as exc:
            raise ResolveError(f"no chat called @{ref.chat} exists") from exc
        except ChannelPrivateError as exc:
            raise ResolveError(
                f"@{ref.chat} is private and the account is not a member of it"
            ) from exc
        except ValueError as exc:
            raise ResolveError(f"could not resolve @{ref.chat}") from exc

    # -------------------------------------------------------------- messages

    async def _fetch(self, entity, ids: list[int]):
        """Fetch messages by id, mapping API errors to user-facing ones."""
        try:
            result = await self._client.get_messages(entity, ids=ids)
        except MsgIdInvalidError as exc:
            raise ResolveError(
                "Telegram rejected those message ids for this chat"
            ) from exc
        except ChatAdminRequiredError as exc:
            raise ResolveError(
                "the account needs admin rights in that chat to read it"
            ) from exc
        except ChannelPrivateError as exc:
            raise ResolveError(
                "that chat is private and the account is not a member of it"
            ) from exc
        except FloodWaitError as exc:
            raise ResolveError(
                f"Telegram asked us to wait {exc.seconds}s before reading that "
                "chat again"
            ) from exc
        if result is None:
            return []
        if not isinstance(result, list):
            return [result]
        return result

    async def _expand_album(self, entity, message) -> list:
        """Return every message belonging to the same album as ``message``."""
        grouped_id = getattr(message, "grouped_id", None)
        if not grouped_id:
            return [message]

        window = list(
            range(
                max(message.id - _ALBUM_WINDOW, 1),
                message.id + _ALBUM_WINDOW + 1,
            )
        )
        try:
            neighbours = await self._fetch(entity, window)
        except ResolveError:
            return [message]

        album = [
            candidate
            for candidate in neighbours
            if candidate is not None
            and getattr(candidate, "grouped_id", None) == grouped_id
        ]
        album.sort(key=lambda item: item.id)
        return album or [message]

    async def _resolve_comment(self, entity, post_id: int, comment_id: int):
        """Resolve ``?comment=`` by hopping into the linked discussion group."""
        try:
            discussion = await self._client(
                GetDiscussionMessageRequest(peer=entity, msg_id=post_id)
            )
        except Exception as exc:  # Telethon raises a variety of RPC errors here
            raise ResolveError(
                "that post has no comment thread the account can read"
            ) from exc

        messages = getattr(discussion, "messages", None) or []
        if not messages:
            raise ResolveError("that post has no comment thread")

        discussion_chat = await messages[0].get_chat()
        found = await self._fetch(discussion_chat, [comment_id])
        found = [m for m in found if m is not None]
        if not found:
            raise ResolveError(f"comment {comment_id} no longer exists")
        return discussion_chat, found

    async def resolve(self, ref: MessageRef) -> tuple[object, list]:
        """Resolve a reference to ``(chat, messages)``.

        Album items are expanded unless the link carried ``?single``. Missing
        or deleted ids are dropped; an empty result raises
        :class:`ResolveError` so the caller always has something to report.
        """
        entity = await self.entity(ref)

        if ref.is_invite_only:
            raise ResolveError(
                "that invite link points at a chat, not at a message. Send a "
                "message link such as https://t.me/c/123456/789."
            )

        if ref.comment_id is not None and ref.ids:
            entity, messages = await self._resolve_comment(
                entity, ref.ids[0], ref.comment_id
            )
            return entity, messages

        messages = [m for m in await self._fetch(entity, list(ref.ids)) if m is not None]
        if not messages:
            raise ResolveError(
                f"no message found at {ref.describe()} (it may have been deleted)"
            )

        if len(messages) == 1 and not ref.single:
            messages = await self._expand_album(entity, messages[0])

        return entity, messages

    async def join_public(self, username: str) -> None:
        """Join a public chat with the reading account."""
        try:
            await self._client(JoinChannelRequest(username))
        except Exception as exc:
            raise ResolveError(f"could not join @{username}: {exc}") from exc
