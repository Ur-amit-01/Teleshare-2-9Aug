"""
admin_settings.py — /setting panel.

Buttons trigger a "waiting for reply" state per-admin (AWAITING dict); the
next plain text message that admin sends is captured as the new value.
"""
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from plugins.helper.filters import admin_filter
from plugins.helper.settings import settings
from plugins.helper.time_parser import format_time, parse_time

# admin_id -> setting key currently being edited
AWAITING: dict = {}

FIELD_LABELS = {
    "force_sub_channels": "Force-sub channels (space separated ids/usernames, or 'none')",
    "auto_delete_time": "Auto-delete timer (e.g. '10m', '1h', or '0' to disable)",
    "protect_content": "Protect content — reply 'on' or 'off'",
    "start_text": "Start message text (use {mention} for the user's mention)",
    "start_photo": "Start message photo — send a photo, or reply with a direct image URL, or 'none' to remove it",
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
        f"• Start photo: <code>{'set' if s.get('start_photo') else 'none'}</code>\n"
        f"• Leave message: <code>{'enabled' if s['leave_message'] else 'disabled'}</code>\n\n"
        "Tap a field below to change it."
    )


def _panel_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 Force-sub channels", callback_data="setting:force_sub_channels")],
        [InlineKeyboardButton("⏱ Auto-delete timer", callback_data="setting:auto_delete_time")],
        [InlineKeyboardButton("🛡 Protect content", callback_data="setting:protect_content")],
        [InlineKeyboardButton("📝 Start text", callback_data="setting:start_text")],
        [InlineKeyboardButton("🖼 Start photo", callback_data="setting:start_photo")],
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


def _has_pending_photo_setting(_, __, message: Message) -> bool:
    return (
        message.from_user is not None
        and AWAITING.get(message.from_user.id) == "start_photo"
    )


@Client.on_message(
    filters.private & filters.photo & admin_filter & filters.create(_has_pending_photo_setting)
)
async def setting_apply_photo(client, message: Message):
    AWAITING.pop(message.from_user.id)
    await settings.set("start_photo", message.photo.file_id)
    await message.reply_text(_panel_text(), reply_markup=_panel_buttons())


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
        elif key in ("start_text", "custom_caption", "leave_message", "start_photo"):
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


