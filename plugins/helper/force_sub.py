"""
admin_settings.py — /setting panel.

Buttons trigger a "waiting for reply" state per-admin (AWAITING dict); the
next plain text message that admin sends is captured as the new value.
"""
import asyncio
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from pyrogram.enums import ChatMemberStatus

from plugins.helper.filters import admin_filter
from plugins.helper.settings import settings
from plugins.helper.time_parser import format_time, parse_time

# admin_id -> setting key currently being edited
AWAITING: dict = {}

# Store warning messages to avoid spamming
WARNED_USERS = {}

FIELD_LABELS = {
    "force_sub_channels": "Force-sub channels (space separated ids/usernames, or 'none')",
    "auto_delete_time": "Auto-delete timer (e.g. '10m', '1h', or '0' to disable)",
    "protect_content": "Protect content — reply 'on' or 'off'",
    "start_text": "Start message text (use {mention} for the user's mention)",
    "custom_caption": "Extra caption line appended to delivered files (or 'none')",
    "leave_message": "DM sent when a user leaves a force-sub channel (use {mention}, {chat_title}, or 'none' to disable)",
}


def _panel_text() -> str:
    s = settings.all()
    fsub = ", ".join(str(c) for c in s["force_sub_channels"]) or "none"
    auto_del = format_time(s["auto_delete_time"]) if s["auto_delete_time"] else "disabled"
    return (
        "⚙️ <b>Bot settings</b>\n\n"
        f"• Force-sub channels: <code>{fsub}</code>\n"
        f"• Auto-delete: <code>{auto_del}</code>\n"
        f"• Protect content: <code>{s['protect_content']}</code>\n"
        f"• Custom caption: <code>{s['custom_caption'] or 'none'}</code>\n"
        f"• Leave message: <code>{'enabled' if s['leave_message'] else 'disabled'}</code>\n\n"
        "Tap a field below to change it."
    )


def _panel_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 Force-sub channels", callback_data="setting:force_sub_channels")],
        [InlineKeyboardButton("⏱ Auto-delete timer", callback_data="setting:auto_delete_time")],
        [InlineKeyboardButton("🛡 Protect content", callback_data="setting:protect_content")],
        [InlineKeyboardButton("📝 Start text", callback_data="setting:start_text")],
        [InlineKeyboardButton("💬 Custom caption", callback_data="setting:custom_caption")],
        [InlineKeyboardButton("💔 Leave message", callback_data="setting:leave_message")],
    ])


@Client.on_message(filters.command("setting") & filters.private & admin_filter)
async def setting_panel(client, message: Message):
    await message.reply_text(_panel_text(), reply_markup=_panel_buttons())


@Client.on_callback_query(filters.regex(r"^setting:") & admin_filter)
async def setting_pick(client, query: CallbackQuery):
    key = query.data.split(":", 1)[1]
    AWAITING[query.from_user.id] = key
    await query.answer()
    await query.message.reply_text(
        f"✏️ Send the new value.\n<i>{FIELD_LABELS[key]}</i>"
    )


def _has_pending_setting(_, __, message: Message) -> bool:
    if not message.from_user or message.from_user.id not in AWAITING:
        return False
    # let ordinary commands (e.g. /cancel, /batch) through untouched
    return not (message.text or "").startswith("/")


@Client.on_message(
    filters.private & filters.text & admin_filter & filters.create(_has_pending_setting)
)
async def setting_apply(client, message: Message):
    key = AWAITING.pop(message.from_user.id)
    raw = message.text.strip()

    try:
        if key == "force_sub_channels":
            if raw.lower() == "none":
                value = []
            else:
                value = []
                for tok in raw.split():
                    value.append(int(tok) if tok.lstrip("-").isdigit() else tok.lstrip("@"))
        elif key == "auto_delete_time":
            value = 0 if raw == "0" else parse_time(raw)
        elif key == "protect_content":
            value = raw.lower() in ("on", "true", "yes", "1")
        elif key in ("start_text", "custom_caption", "leave_message"):
            value = "" if raw.lower() == "none" else raw
        else:
            await message.reply_text("Unknown setting.")
            return
    except ValueError as e:
        await message.reply_text(f"❌ Couldn't parse that: {e}")
        AWAITING[message.from_user.id] = key  # let them retry
        return

    await settings.set(key, value)
    await message.reply_text(_panel_text(), reply_markup=_panel_buttons())


# ============ NEW: Auto-warning when users leave force-sub channels ============

async def get_channel_link(client, chat_id):
    """Get a clickable link for a channel"""
    try:
        chat = await client.get_chat(chat_id)
        if chat.username:
            return f"https://t.me/{chat.username}"
        else:
            # Private channel - get invite link
            try:
                invite_link = await client.create_chat_invite_link(chat_id, member_limit=1)
                return invite_link.invite_link
            except:
                return None
    except:
        return None


@Client.on_chat_member_updated()
async def handle_member_update(client, update):
    """Check when users leave force-sub channels and warn them"""
    # Only process if user left (not joined)
    if update.new_chat_member.status != ChatMemberStatus.LEFT:
        return
    
    user = update.from_user
    if not user:
        return
    
    # Check if this channel is in force_sub_channels
    force_sub_channels = settings.get("force_sub_channels", [])
    chat_id = update.chat.id
    
    # Convert to int for comparison (force_sub_channels can have ints or strings)
    target_chat_id = int(chat_id) if str(chat_id).lstrip('-').isdigit() else str(chat_id)
    
    # Check if the channel is in the force-sub list
    is_force_sub = False
    for fsub in force_sub_channels:
        if str(fsub) == str(target_chat_id) or str(fsub).lstrip('@') == str(target_chat_id).lstrip('@'):
            is_force_sub = True
            break
    
    if not is_force_sub:
        return
    
    # Check if user is already in any other force-sub channel
    # We'll check all force-sub channels
    is_in_any = False
    for fsub in force_sub_channels:
        try:
            member = await client.get_chat_member(fsub, user.id)
            if member.status not in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
                is_in_any = True
                break
        except:
            continue
    
    # If user has left ALL force-sub channels, warn them
    if not is_in_any:
        # Check if we've already warned this user recently (cooldown)
        current_time = asyncio.get_event_loop().time()
        if user.id in WARNED_USERS:
            if current_time - WARNED_USERS[user.id] < 300:  # 5 minute cooldown
                return
        
        # Get channel link
        channel_link = await get_channel_link(client, chat_id)
        channel_name = update.chat.title or "the channel"
        
        # Send warning message
        warning_text = (
            f"⚠️ <b>You left {channel_name}!</b>\n\n"
            f"To continue using this bot, you must join the force-subscribed channel.\n\n"
            f"👉 <a href='{channel_link or '#'}'>Join {channel_name}</a>\n\n"
            f"<i>You will not be able to use the bot until you rejoin.</i>"
        )
        
        try:
            await client.send_message(
                user.id,
                warning_text,
                disable_web_page_preview=False
            )
            WARNED_USERS[user.id] = current_time
            
            # Try to send again after 5 minutes if they still haven't joined
            asyncio.create_task(schedule_reminder(client, user.id, chat_id, channel_name, channel_link))
        except Exception as e:
            print(f"Couldn't send warning to {user.id}: {e}")
    
    # If user has left one channel but is still in others, send a different message
    elif is_in_any and update.old_chat_member.status not in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
        # User left one channel but is still in others
        channel_link = await get_channel_link(client, chat_id)
        channel_name = update.chat.title or "the channel"
        
        warning_text = (
            f"⚠️ <b>You left {channel_name}!</b>\n\n"
            f"This channel is required to use this bot. Please rejoin:\n\n"
            f"👉 <a href='{channel_link or '#'}'>Join {channel_name}</a>"
        )
        
        try:
            await client.send_message(user.id, warning_text, disable_web_page_preview=False)
        except:
            pass


async def schedule_reminder(client, user_id, channel_id, channel_name, channel_link):
    """Send a reminder after 5 minutes if user still hasn't rejoined"""
    await asyncio.sleep(300)  # 5 minutes
    
    # Check if user has rejoined
    try:
        member = await client.get_chat_member(channel_id, user_id)
        if member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
            # Still left, send another reminder
            warning_text = (
                f"⏰ <b>Reminder: You still haven't joined {channel_name}!</b>\n\n"
                f"Please join to continue using the bot:\n\n"
                f"👉 <a href='{channel_link or '#'}'>Join {channel_name}</a>"
            )
            await client.send_message(user_id, warning_text, disable_web_page_preview=False)
    except:
        pass


@Client.on_message(filters.command("check_force") & filters.private & admin_filter)
async def check_force_sub_status(client, message: Message):
    """Admin command to check force-sub status for users"""
    force_sub_channels = settings.get("force_sub_channels", [])
    
    if not force_sub_channels:
        await message.reply_text("No force-sub channels configured.")
        return
    
    # Get recent users (this is a simple example - you might want to expand this)
    try:
        # Try to get chat members from the first channel
        chat_id = force_sub_channels[0]
        members = []
        async for member in client.get_chat_members(chat_id):
            if len(members) >= 10:  # Limit to 10 for demo
                break
            members.append(f"• {member.user.mention} ({member.user.id})")
        
        if members:
            await message.reply_text(
                f"📊 <b>Recent users in channel {chat_id}</b>\n\n" + 
                "\n".join(members) +
                "\n\n<i>Use /setting to modify force-sub channels.</i>"
            )
        else:
            await message.reply_text("No members found or channel is empty.")
    except Exception as e:
        await message.reply_text(f"Error checking members: {e}")
