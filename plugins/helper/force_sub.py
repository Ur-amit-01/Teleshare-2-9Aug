"""
force_sub.py — everything related to the force-subscribe gate lives here,
isolated from filters.py, so that if force-sub misbehaves (wrong channel id,
bot not admin in the channel, buttons not showing, "request to join" not
being recognized, etc.) there's exactly one file to open.

Contains:
  * _channel_join_button -> builds the "➕ Join X" button for one channel
  * get_missing_channels -> which force-sub channels a user hasn't joined
  * ensure_subscribed    -> the gate called at the top of /start
  * track_join_request   -> records "request to join" submissions so
                             ensure_subscribed can treat them as satisfied
                             for private/request-only channels
  * handle_channel_leave -> fires the moment a user leaves/is removed from
                             a force-sub channel, DMing them a "come back"
                             message with a rejoin button
"""
import logging

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserNotParticipant
from pyrogram.types import ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message

from plugins.helper.db import db
from plugins.helper.settings import settings
from plugins.helper.filters import is_admin

logger = logging.getLogger(__name__)


async def _channel_join_button(client, channel) -> InlineKeyboardButton:
    chat = await client.get_chat(channel)
    if chat.username:
        url = f"https://t.me/{chat.username}"
    else:
        url = chat.invite_link or (await client.export_chat_invite_link(chat.id))
    return InlineKeyboardButton(f"➕ Join {chat.title}", url=url)


async def get_missing_channels(client, user_id: int) -> list:
    """Returns the list of force-sub channels this user hasn't joined/requested."""
    missing = []
    for channel in settings.get("force_sub_channels"):
        try:
            await client.get_chat_member(channel, user_id)
            continue  # already a member
        except UserNotParticipant:
            # Not a member yet — but if they've sent a "request to join" for a
            # private/request-only channel, treat that as satisfying the check.
            chat = await client.get_chat(channel)
            if await db.has_pending_join_request(user_id, chat.id):
                continue
            missing.append(channel)
        except Exception as e:
            # BUGFIX: this used to be a silent `except Exception: continue`,
            # which means if the bot isn't actually an admin in the
            # configured force-sub channel (or the id/username is wrong),
            # EVERY user would silently skip that channel's check — the
            # force-sub requirement would appear completely disabled with no
            # error anywhere. We still don't want to block every user for a
            # misconfigured channel, but we now log it loudly so it's
            # obvious *why* subscribe-gating isn't kicking in.
            logger.warning(
                f"Force-sub check failed for channel={channel!r} "
                f"(bot likely isn't an admin there, or the id/username is wrong): {e}"
            )
            continue
    return missing


async def ensure_subscribed(client, message: Message) -> bool:
    """
    Returns True if the user may proceed. If not, sends the "please join"
    message with buttons (including a Try Again button that resumes any
    deep-link payload) and returns False.
    """
    if not settings.get("force_sub_channels"):
        return True
    if message.from_user and is_admin(message.from_user.id):
        return True

    missing = await get_missing_channels(client, message.from_user.id)
    if not missing:
        return True

    buttons = [[await _channel_join_button(client, ch)] for ch in missing]
    payload = message.command[1] if len(message.command) > 1 else ""
    from config import BOT_USERNAME
    resume_url = f"https://t.me/{BOT_USERNAME}?start={payload}" if payload else f"https://t.me/{BOT_USERNAME}?start=start"
    buttons.append([InlineKeyboardButton("🔄 Try Again", url=resume_url)])

    await message.reply_text(
        "🔒 <b>Join required</b>\n\n"
        "Please join the channel(s) below, then tap <b>Try Again</b>.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return False


# Track "request to join" submissions for force-sub channels that use
# join-request mode, so ensure_subscribed() can treat them as satisfied.
@Client.on_chat_join_request()
async def track_join_request(client, chat_join_request):
    channel_ids = {
        ch if isinstance(ch, int) else None
        for ch in settings.get("force_sub_channels")
    }
    chat_id = chat_join_request.chat.id
    if chat_id in channel_ids or chat_join_request.chat.username in settings.get("force_sub_channels"):
        await db.record_join_request(chat_join_request.from_user.id, chat_id)


# ------------------------------------------------------------- on-leave ---- #

# Statuses that count as "was actually in the channel".
_WAS_MEMBER = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
# Statuses that count as "isn't anymore" — covers both leaving on their own
# and being kicked/banned.
_NOW_GONE = {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}


def _is_force_sub_channel(chat) -> bool:
    configured = settings.get("force_sub_channels")
    return chat.id in configured or (chat.username and chat.username in configured)


@Client.on_chat_member_updated()
async def handle_channel_leave(client, update: ChatMemberUpdated):
    """
    Fires on every membership change in every chat the bot administers.
    We only care about: (a) it's a configured force-sub channel, and
    (b) the user went from "in" to "out" (left, kicked, or banned).

    NOTE: Telegram only sends these updates for chats where the bot is an
    admin — which force-sub channels already require, so no extra setup
    is needed for this to start working.
    """
    if not _is_force_sub_channel(update.chat):
        return

    old_status = update.old_chat_member.status if update.old_chat_member else None
    new_status = update.new_chat_member.status if update.new_chat_member else None
    if old_status not in _WAS_MEMBER or new_status not in _NOW_GONE:
        return

    user = (update.new_chat_member and update.new_chat_member.user) or \
           (update.old_chat_member and update.old_chat_member.user)
    if not user or user.is_bot:
        return

    # Their old join-request record (if any) is now stale — clear it so a
    # future re-join via "request to join" is tracked fresh.
    await db.clear_join_request(user.id, update.chat.id)

    message_template = settings.get("leave_message")
    if not message_template:
        return  # admin disabled this feature via /setting

    try:
        button = await _channel_join_button(client, update.chat.id)
    except Exception as e:
        logger.warning(f"Couldn't build rejoin button for {update.chat.id}: {e}")
        return

    try:
        await client.send_message(
            user.id,
            message_template.format(chat_title=update.chat.title, mention=user.mention),
            reply_markup=InlineKeyboardMarkup([[button]]),
        )
    except Exception as e:
        # Most common cause: user has never started the bot in DM, so
        # Telegram won't let the bot message them first.
        logger.info(f"Couldn't send leave-message to user {user.id}: {e}")
     
