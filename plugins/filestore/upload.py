"""
upload.py — how files get into the system.

Two flows, both admin-only:
  * Direct send: admin sends/forwards a single file in private -> backed up
    and linked immediately.
  * Batch: /batch starts a session, every file sent after that is collected
    (albums included) until /done, which produces one link for everything.

Note: batch sessions live in memory only (BATCH_SESSIONS below). A restart
while a batch is open will lose that in-progress batch — finished links are
unaffected since those are already in the database.
"""
from pyrogram import Client, filters
from pyrogram.types import Message

from config import BACKUP_CHANNEL, LOG_CHANNEL
from plugins.helper.filters import admin_filter
from plugins.helper.media import extract_media
from plugins.filestore.linking import save_link, build_deep_link

# admin_id -> list of collected entries while a batch session is open
BATCH_SESSIONS: dict = {}

MEDIA_FILTER = (
    filters.document | filters.video | filters.photo | filters.audio
    | filters.voice | filters.animation | filters.video_note | filters.sticker
)


@Client.on_message(filters.command("batch") & filters.private & admin_filter)
async def start_batch(client, message: Message):
    BATCH_SESSIONS[message.from_user.id] = []
    await message.reply_text(
        "📦 <b>Batch mode started.</b>\n"
        "Send me all the files you want in this link, then send /done.\n"
        "Send /cancel to discard this batch."
    )


@Client.on_message(filters.command("cancel") & filters.private & admin_filter)
async def cancel_batch(client, message: Message):
    if BATCH_SESSIONS.pop(message.from_user.id, None) is None:
        await message.reply_text("There's no batch in progress.")
    else:
        await message.reply_text("🗑 Batch discarded.")


@Client.on_message(filters.command("done") & filters.private & admin_filter)
async def finish_batch(client, message: Message):
    entries = BATCH_SESSIONS.pop(message.from_user.id, None)
    if entries is None:
        await message.reply_text("There's no batch in progress. Start one with /batch.")
        return
    if not entries:
        await message.reply_text("That batch was empty — nothing to link.")
        return

    code = await save_link(message.from_user.id, entries, is_batch=True)
    link = build_deep_link(code)
    await message.reply_text(
        f"✅ <b>Batch link ready</b> ({len(entries)} file(s))\n\n<code>{link}</code>"
    )
    if LOG_CHANNEL:
        await client.send_message(
            LOG_CHANNEL, f"📦 Batch link created by {message.from_user.mention}: {link}"
        )


@Client.on_message(filters.private & admin_filter & MEDIA_FILTER)
async def handle_admin_media(client, message: Message):
    admin_id = message.from_user.id

    backup_msg = await message.copy(chat_id=BACKUP_CHANNEL)
    media = extract_media(backup_msg)
    if not media:
        await message.reply_text("⚠️ Couldn't read that file, please try again.")
        return
    media_type, file_id, caption = media

    entry = {
        "message_id": backup_msg.id,
        "media_type": media_type,
        "file_id": file_id,
        "caption": caption,
        # use the *original* incoming message's group id — copying to the
        # backup channel one at a time doesn't reliably preserve Telegram's
        # own grouping, but we only need this internally for re-upload albums.
        "media_group_id": message.media_group_id,
    }

    if admin_id in BATCH_SESSIONS:
        BATCH_SESSIONS[admin_id].append(entry)
        # Keep it quiet — one message per file would get spammy for big batches.
        try:
            await message.react(emoji="👍")
        except Exception:
            pass
        return

    # Direct mode — one file, one link, right away.
    code = await save_link(admin_id, [entry], is_batch=False)
    link = build_deep_link(code)
    await message.reply_text(f"✅ <b>File stored.</b>\n\n<code>{link}</code>")
    if LOG_CHANNEL:
        await client.send_message(
            LOG_CHANNEL, f"📄 File linked by {message.from_user.mention}: {link}"
        )
