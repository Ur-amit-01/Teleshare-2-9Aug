"""
upload.py — how files get into the system.

Two flows, both admin-only:
  * Direct send: admin sends/forwards a single file (or album). Nothing is
    written to the backup channel yet — the bot only remembers *where* the
    message came from. A link is only generated after the admin confirms
    via the Yes/No prompt.
  * Batch: /batch starts a session, every file/album sent after that is
    remembered (still not copied anywhere) until /done, which produces one
    link for everything (no per-item confirmation — /done is the
    confirmation).

IMPORTANT — backup-channel timing:
Media is copied into BACKUP_CHANNEL in exactly one place: `_finalize_entries()`,
which runs only once a link is actually about to be created (on "✅ Yes", or
on /done). Tapping "❌ No" or running /cancel discards everything with
nothing to clean up in the backup channel, because nothing was ever sent
there. This intentionally differs from copying-on-receipt: nothing should
land in the backup channel until a shareable link is actually generated.

Albums (media groups) are always handled as a single unit — via
get_media_group() while collecting, and copy_media_group() when finalizing —
so they stay grouped exactly as they were sent instead of arriving as
separate messages.

Note: batch sessions and pending single-file confirmations live in memory
only. A restart while one is open will lose it — finished links are
unaffected since those are already in the database.
"""
import asyncio
import logging
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

logger = logging.getLogger(__name__)

# admin_id -> list of collected "pending items" while a batch session is open.
# A pending item is just a pointer (src_chat_id/src_message_id) to a message
# that hasn't been copied anywhere yet.
BATCH_SESSIONS: dict = {}

# admin_id -> asyncio.Lock. Every collect-then-append happens under this
# lock, and /done acquires it too, so /done can never finalize a batch while
# a file that was sent moments earlier is still mid-append — otherwise that
# file would silently miss the link.
_ADMIN_LOCKS: dict = {}


def _lock_for(admin_id: int) -> asyncio.Lock:
    lock = _ADMIN_LOCKS.get(admin_id)
    if lock is None:
        lock = asyncio.Lock()
        _ADMIN_LOCKS[admin_id] = lock
    return lock


# admin_id -> list of pending items awaiting a Yes/No link-generation confirmation
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


def _pending_item(message: Message) -> dict:
    """A lightweight reference to a not-yet-backed-up message. Nothing is
    copied anywhere at this point — that only happens in _finalize_entries()."""
    return {
        "src_chat_id": message.chat.id,
        "src_message_id": message.id,
        "media_group_id": message.media_group_id,
    }


def _group_pending(items: list) -> list:
    """Group consecutive pending items sharing a media_group_id into sub-lists."""
    groups, current, current_gid = [], [], object()
    for item in items:
        gid = item.get("media_group_id")
        if gid and gid == current_gid:
            current.append(item)
        else:
            if current:
                groups.append(current)
            current = [item]
            current_gid = gid
    if current:
        groups.append(current)
    return groups


async def _finalize_entries(client, items: list) -> list:
    """
    The ONLY place media is ever copied into BACKUP_CHANNEL. Runs once a
    link is actually about to be created (Yes / /done), never before.
    Returns the entries ready to be handed to save_link().
    """
    entries = []
    for group in _group_pending(items):
        src_chat_id = group[0]["src_chat_id"]
        src_message_id = group[0]["src_message_id"]
        try:
            if len(group) > 1:
                copied = await client.copy_media_group(BACKUP_CHANNEL, src_chat_id, src_message_id)
            else:
                copied = [await client.copy_message(BACKUP_CHANNEL, src_chat_id, src_message_id)]
        except Exception as e:
            logger.warning(f"Couldn't back up message {src_chat_id}/{src_message_id}: {e}")
            continue  # source message may have been deleted/edited meanwhile — skip it

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
                "media_group_id": group[0]["media_group_id"],
            })
    return entries


@Client.on_message(filters.command("batch") & filters.private & admin_filter)
async def start_batch(client, message: Message):
    BATCH_SESSIONS[message.from_user.id] = []
    await message.reply_text(
        "📦 <b>Batch mode started.</b>\n"
        "Send me all the files you want in this link, then send /done.\n"
        "Send /cancel to discard this batch.\n"
        "Nothing is uploaded to storage until /done."
    )


@Client.on_message(filters.command("cancel") & filters.private & admin_filter)
async def cancel_batch(client, message: Message):
    admin_id = message.from_user.id
    async with _lock_for(admin_id):
        if BATCH_SESSIONS.pop(admin_id, None) is None:
            await message.reply_text("There's no batch in progress.")
        else:
            await message.reply_text("🗑 Batch discarded — nothing was ever stored.")


@Client.on_message(filters.command("done") & filters.private & admin_filter)
async def finish_batch(client, message: Message):
    admin_id = message.from_user.id
    # Waits for any file that's still being appended right now to finish,
    # before we look at the list.
    async with _lock_for(admin_id):
        items = BATCH_SESSIONS.pop(admin_id, None)
        if items is None:
            await message.reply_text("There's no batch in progress. Start one with /batch.")
            return
        if not items:
            await message.reply_text("That batch was empty — nothing to link.")
            return

    status = await message.reply_text("⏳ Saving files and generating your link...")

    entries = await _finalize_entries(client, items)
    if not entries:
        await status.edit_text(
            "❌ Couldn't back up any of those files (they may have been deleted). Please try the batch again."
        )
        return

    code = await save_link(admin_id, entries, is_batch=True)
    link = build_deep_link(code)
    await status.edit_text(
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
        try:
            # Non-destructive: just reads the album's messages, copies nothing.
            group_msgs = await client.get_media_group(message.chat.id, message.id)
        except Exception as e:
            logger.warning(f"get_media_group failed, falling back to single item: {e}")
            group_msgs = [message]
        items = [_pending_item(m) for m in group_msgs]
    else:
        items = [_pending_item(message)]

    # Held for the append below, so /done can't pop the session mid-write —
    # otherwise a file sent moments earlier could silently miss the batch.
    async with _lock_for(admin_id):
        if admin_id in BATCH_SESSIONS:
            BATCH_SESSIONS[admin_id].extend(items)
            # Keep it quiet — one message per file would get spammy for big batches.
            try:
                await message.react(emoji="👍")
            except Exception:
                pass
            return

    # Direct mode — hold the references and ask for confirmation before
    # touching the backup channel at all.
    PENDING_SINGLE[admin_id] = items
    label = f"this {len(items)}-item album" if len(items) > 1 else "this file"
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
    items = PENDING_SINGLE.pop(admin_id, None)
    if items is None:
        await query.answer("Nothing pending — that request expired.", show_alert=True)
        return

    decision = query.data.split(":", 1)[1]

    if decision == "no":
        # Nothing was ever copied to the backup channel, so there's nothing
        # to delete — discarding is just forgetting the pending reference.
        await query.answer()
        await query.message.edit_text("🗑 Discarded — no link was generated, nothing was stored.")
        return

    await query.answer()
    await query.message.edit_text("⏳ Saving file(s) and generating your link...")

    entries = await _finalize_entries(client, items)
    if not entries:
        await query.message.edit_text(
            "❌ Couldn't back up that file (it may have been deleted). Please send it again."
        )
        return

    code = await save_link(admin_id, entries, is_batch=len(entries) > 1)
    link = build_deep_link(code)
    await query.message.edit_text(
        f"✅ <b>Link ready</b> ({len(entries)} file(s))\n\n<code>{link}</code>"
    )
    if LOG_CHANNEL:
        await client.send_message(
            LOG_CHANNEL, f"📄 Link created by {query.from_user.mention}: {link}"
        )
     
