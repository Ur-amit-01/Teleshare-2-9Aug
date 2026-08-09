"""
upload.py — how files get into the system.

Two flows, both admin-only:
  * Direct send: admin sends/forwards a single file (or album) in private.
    It's copied to the backup channel right away, but a link is only
    generated after the admin confirms via the Yes/No prompt.
  * Batch: /batch starts a session, every file/album sent after that is
    collected until /done, which produces one link for everything (no
    per-item confirmation — /done is the confirmation).

Albums (media groups) are always copied as a single unit — via
copy_media_group — both into the backup channel and back out to the user,
so they stay grouped exactly as they were sent instead of arriving as
separate messages.

Note: batch sessions and pending single-file confirmations live in memory
only. A restart while one is open will lose it — finished links are
unaffected since those are already in the database.
"""
import time

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import BACKUP_CHANNEL, LOG_CHANNEL
from plugins.helper.filters import admin_filter
from plugins.helper.media import extract_media
from plugins.filestore.linking import save_link, build_deep_link

# admin_id -> list of collected entries while a batch session is open
BATCH_SESSIONS: dict = {}

# admin_id -> list of entries awaiting a Yes/No link-generation confirmation
PENDING_SINGLE: dict = {}

# media_group_id -> claimed_at timestamp. Telegram fires the handler once per
# item in an album; we only want to process the album once, on whichever
# item arrives first.
_CLAIMED_GROUPS: dict = {}
_GROUP_CLAIM_TTL = 30  # seconds; comfortably longer than an album can take to arrive/process

MEDIA_FILTER = (
    filters.document | filters.video | filters.photo | filters.audio
    | filters.voice | filters.animation | filters.video_note | filters.sticker
)


def _claim_group(gid: str) -> bool:
    """Returns True if this call is the one that should process album `gid`."""
    now = time.time()
    for g, ts in list(_CLAIMED_GROUPS.items()):
        if now - ts > _GROUP_CLAIM_TTL:
            _CLAIMED_GROUPS.pop(g, None)
    if gid in _CLAIMED_GROUPS:
        return False
    _CLAIMED_GROUPS[gid] = now
    return True


async def _copy_album(client, message: Message) -> list:
    """Copy the whole album `message` belongs to into the backup channel as
    one group, returning one entry per item."""
    copied = await client.copy_media_group(BACKUP_CHANNEL, message.chat.id, message.id)
    entries = []
    for cp in copied:
        media = extract_media(cp)
        if not media:
            continue
        media_type, file_id, caption = media
        entries.append({
            "message_id": cp.id,
            "media_type": media_type,
            "file_id": file_id,
            "caption": caption,
            "media_group_id": message.media_group_id,
        })
    return entries


async def _copy_single(message: Message) -> dict:
    backup_msg = await message.copy(chat_id=BACKUP_CHANNEL)
    media = extract_media(backup_msg)
    if not media:
        return None
    media_type, file_id, caption = media
    return {
        "message_id": backup_msg.id,
        "media_type": media_type,
        "file_id": file_id,
        "caption": caption,
        "media_group_id": None,
    }


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

    if message.media_group_id:
        if not _claim_group(message.media_group_id):
            return  # another item from this same album already triggered processing
        entries = await _copy_album(client, message)
        if not entries:
            await message.reply_text("⚠️ Couldn't read that album, please try again.")
            return
    else:
        entry = await _copy_single(message)
        if not entry:
            await message.reply_text("⚠️ Couldn't read that file, please try again.")
            return
        entries = [entry]

    if admin_id in BATCH_SESSIONS:
        BATCH_SESSIONS[admin_id].extend(entries)
        # Keep it quiet — one message per file would get spammy for big batches.
        try:
            await message.react(emoji="👍")
        except Exception:
            pass
        return

    # Direct mode — hold the entries and ask for confirmation before linking.
    PENDING_SINGLE[admin_id] = entries
    label = f"this {len(entries)}-item album" if len(entries) > 1 else "this file"
    await message.reply_text(
        f"Generate a shareable link for {label}?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes", callback_data="linkgen:yes"),
            InlineKeyboardButton("❌ No", callback_data="linkgen:no"),
        ]]),
    )


@Client.on_callback_query(filters.regex(r"^linkgen:") & admin_filter)
async def confirm_link(client, query: CallbackQuery):
    admin_id = query.from_user.id
    entries = PENDING_SINGLE.pop(admin_id, None)
    if entries is None:
        await query.answer("Nothing pending — that request expired.", show_alert=True)
        return

    decision = query.data.split(":", 1)[1]

    if decision == "no":
        for e in entries:
            try:
                await client.delete_messages(BACKUP_CHANNEL, e["message_id"])
            except Exception:
                pass
        await query.answer()
        await query.message.edit_text("🗑 Discarded — no link was generated.")
        return

    await query.answer()
    code = await save_link(admin_id, entries, is_batch=len(entries) > 1)
    link = build_deep_link(code)
    await query.message.edit_text(
        f"✅ <b>Link ready</b> ({len(entries)} file(s))\n\n<code>{link}</code>"
    )
    if LOG_CHANNEL:
        await client.send_message(
            LOG_CHANNEL, f"📄 Link created by {query.from_user.mention}: {link}"
        )
