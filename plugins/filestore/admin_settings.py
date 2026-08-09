"""
admin_settings.py — /setting panel.

Buttons trigger a "waiting for reply" state per-admin (AWAITING dict). For
plain-value fields the next text message is parsed as the new value. For
"rich" fields (currently just start_text) the next message — text, an image,
an image with caption + buttons, even something forwarded from elsewhere —
is captured exactly as sent via plugins.helper.template.
"""
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from plugins.helper.filters import admin_filter
from plugins.helper.settings import settings
from plugins.helper.time_parser import format_time, parse_time
from plugins.helper.template import capture_template, describe_template

# admin_id -> setting key currently being edited
AWAITING: dict = {}

# fields captured as a full message (text/media/caption/buttons) rather than a plain value
RICH_FIELDS = {"start_text"}

FIELD_LABELS = {
    "force_sub_channels": "Force-sub channels (space separated ids/usernames, or 'none')",
    "auto_delete_time": "Auto-delete timer (e.g. '10m', '1h', or '0' to disable)",
    "protect_content": "Protect content — reply 'on' or 'off'",
    "start_text": (
        "Send the new start message — text, a photo/video/document with a caption, "
        "buttons, or forward it from elsewhere. It'll be shown exactly as sent. "
        "Use {mention} anywhere in the text/caption to insert the user's mention."
    ),
    "custom_caption": "Extra caption line appended to delivered files (or 'none')",
}


def _panel_text() -> str:
    s = settings.all()
    fsub = ", ".join(str(c) for c in s["force_sub_channels"]) or "none"
    auto_del = format_time(s["auto_delete_time"]) if s["auto_delete_time"] else "disabled"
    start_preview = describe_template(s["start_text"])
    return (
        "⚙️ <b>Bot settings</b>\n\n"
        f"• Force-sub channels: <code>{fsub}</code>\n"
        f"• Auto-delete: <code>{auto_del}</code>\n"
        f"• Protect content: <code>{s['protect_content']}</code>\n"
        f"• Start message: {start_preview}\n"
        f"• Custom caption: <code>{s['custom_caption'] or 'none'}</code>\n\n"
        "Tap a field below to change it."
    )


def _panel_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 Force-sub channels", callback_data="setting:force_sub_channels")],
        [InlineKeyboardButton("⏱ Auto-delete timer", callback_data="setting:auto_delete_time")],
        [InlineKeyboardButton("🛡 Protect content", callback_data="setting:protect_content")],
        [InlineKeyboardButton("📝 Start message", callback_data="setting:start_text")],
        [InlineKeyboardButton("💬 Custom caption", callback_data="setting:custom_caption")],
    ])


@Client.on_message(filters.command("setting") & filters.private & admin_filter)
async def setting_panel(client, message: Message):
    await message.reply_text(_panel_text(), reply_markup=_panel_buttons())


@Client.on_callback_query(filters.regex(r"^setting:") & admin_filter)
async def setting_pick(client, query: CallbackQuery):
    key = query.data.split(":", 1)[1]
    AWAITING[query.from_user.id] = key
    await query.answer()
    await query.message.reply_text(f"✏️ {FIELD_LABELS[key]}")


def _has_pending_setting(_, __, message: Message) -> bool:
    if not message.from_user or message.from_user.id not in AWAITING:
        return False
    # let ordinary commands (e.g. /cancel, /batch) through untouched
    return not (message.text or "").startswith("/")


@Client.on_message(filters.private & admin_filter & filters.create(_has_pending_setting))
async def setting_apply(client, message: Message):
    key = AWAITING.pop(message.from_user.id)

    if key in RICH_FIELDS:
        template = await capture_template(message)
        await settings.set(key, template)
        await message.reply_text(_panel_text(), reply_markup=_panel_buttons())
        return

    if not message.text:
        await message.reply_text("❌ That field needs plain text — please send text.")
        AWAITING[message.from_user.id] = key  # let them retry
        return

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
        elif key == "custom_caption":
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
    
