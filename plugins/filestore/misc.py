from datetime import datetime

from pyrogram import Client, filters
from pyrogram.types import Message

from plugins.helper.db import db
from plugins.helper.filters import admin_filter, is_admin
from plugins.filestore.upload import (
    BATCH_SESSIONS, PENDING_SINGLE, _BATCH_STATUS_MSG, _cancel_batch_timeout,
)
from plugins.filestore.test_series import AWAITING_TS, PENDING_PAPER_ENTRIES
from plugins.filestore.admin_settings import AWAITING as SETTINGS_AWAITING

START_TIME = datetime.utcnow()

ADMIN_HELP = """<b>🛠 Admin commands</b>

• Send any file — stores it and hands back a shareable link right away
• /batch — start collecting several files and/or text messages into one link, finish with the ✅ button
• Use the ❌ button on the batch progress message to discard the batch currently in progress
• /range_files SOURCE_CHAT_ID FIRST_ID LAST_ID — bulk-import an existing range of messages
• /delete CODE_OR_LINK — remove a link and its backed-up file(s)
• /broadcast — reply to a message (or add text) to send it to every user
• /setting — view/edit force-sub channels, auto-delete timer, protect content, etc.
• /testseries — manage the Institute → Test Series → Papers menu shown on /start
• /cancel — abandon whatever admin prompt/batch/upload is currently pending
• /stats — usage statistics
"""

USER_HELP = """<b>ℹ️ Help</b>

Tap /start to browse test series by institute, then series, then paper.
Or open a link someone shared with you (t.me/<i>bot</i>?start=CODE) and I'll
deliver the file(s) behind it — you may need to join a channel or two first
if asked.
"""


@Client.on_message(filters.command("help") & filters.private)
async def help_command(client, message: Message):
    text = USER_HELP
    if is_admin(message.from_user.id):
        text += "\n" + ADMIN_HELP
    await message.reply_text(text)


@Client.on_message(filters.command("cancel") & filters.private & admin_filter)
async def cancel_command(client, message: Message):
    """One escape hatch for every admin 'awaiting a reply' state — batch
    sessions, a pending single-file link confirmation, an open /testseries
    prompt (including addpapers), or a /setting field edit — since each of
    those plugins only handles the input it expects and previously left an
    admin stuck with no way out except waiting out a timeout."""
    admin_id = message.from_user.id
    cancelled = []

    if admin_id in BATCH_SESSIONS:
        BATCH_SESSIONS.pop(admin_id, None)
        _cancel_batch_timeout(admin_id)
        status_msg = _BATCH_STATUS_MSG.pop(admin_id, None)
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        cancelled.append("batch session")

    if admin_id in PENDING_SINGLE:
        PENDING_SINGLE.pop(admin_id, None)
        cancelled.append("pending link confirmation")

    if admin_id in AWAITING_TS:
        AWAITING_TS.pop(admin_id, None)
        cancelled.append("test series prompt")

    orphaned = PENDING_PAPER_ENTRIES.pop(admin_id, None)
    if orphaned:
        cancelled.append(
            f"paper-adding session ({len(orphaned)} file(s) already backed up but "
            "not yet linked or attached to any paper — safe to discard)"
        )

    if admin_id in SETTINGS_AWAITING:
        SETTINGS_AWAITING.pop(admin_id, None)
        cancelled.append("settings prompt")

    if not cancelled:
        await message.reply_text("Nothing to cancel.")
        return

    await message.reply_text("✅ Cancelled: " + ", ".join(cancelled) + ".")


@Client.on_message(filters.command("stats") & filters.private & admin_filter)
async def stats_command(client, message: Message):
    users = await db.total_users_count()
    links = await db.total_links_count()
    files = await db.total_files_stored()
    uptime = datetime.utcnow() - START_TIME

    await message.reply_text(
        "📊 <b>Stats</b>\n\n"
        f"• Users: <code>{users}</code>\n"
        f"• Links created: <code>{links}</code>\n"
        f"• Files stored: <code>{files}</code>\n"
        f"• Uptime: <code>{str(uptime).split('.')[0]}</code>"
    )

