"""
filters.py — the two checks that gate nearly every command:
  * admin_filter     -> is this user an admin (config.ADMINS)?
  * ensure_subscribed -> has this user joined every force-sub channel?

Both are meant to be called/used at the very top of a handler, before any
other work happens.
"""
from pyrogram import filters
from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ADMINS
from plugins.helper.db import db
from plugins.helper.settings import settings

# ---------------------------------------------------------------- admin ---- #


async def _is_admin(_, __, message: Message) -> bool:
    return bool(message.from_user) and message.from_user.id in ADMINS


admin_filter = filters.create(_is_admin)


def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


# -------------------------------------------------------------- fsub ------- #


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
        except Exception:
            # bot isn't admin there / channel unreachable — skip rather than block everyone
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
