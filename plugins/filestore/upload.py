"""
upload.py — how files get into the system.

Two flows, both admin-only:
  * Direct send: admin sends/forwards a single file (or album). Nothing is
    written to the backup channel yet — the bot only remembers *where* the
    message came from. A link is only generated after the admin confirms
    via the Yes/No prompt.
  * Batch: /batch starts a session, every file/album sent after that is
    remembered (still not copied anywhere). Each new file updates a single
    running counter message ("N file(s) received in batch so far") carrying
    ✅ Yes / ❌ No buttons — tapping ✅ Yes produces one link for everything
    (no per-item confirmation — that tap is the confirmation), tapping ❌ No
    discards it. /done and /cancel still work as text-command equivalents
    of those two buttons, for anyone who prefers typing.

IMPORTANT — backup-channel timing:
Media is copied into BACKUP_CHANNEL in exactly one place: `_finalize_entries()`,
which runs only once a link is actually about to be created (on "✅ Yes", or
/done). Tapping "❌ No" or running /cancel discards everything with
nothing to clean up in the backup channel, because nothing was ever sent
there. This intentionally differs from copying-on-receipt: nothing should
land in the backup channel until a shareable link is actually generated.

IMPORTANT — ordering:
Handlers for different incoming messages can run concurrently (an album's
extra get_media_group() await can let a later, faster single-file message
finish appending first). Telegram message IDs are monotonically increasing
per chat, so BATCH_SESSIONS is re-sorted by src_message_id on every append —
that's what keeps the saved order matching the order files were actually
sent in, regardless of which async task happened to finish first.

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
from pyrogram.errors import FloodWait
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

# admin_id -> the "N file(s) received in batch" status Message we keep editing,
# so progress updates don't spam one message per file.
_BATCH_STATUS_MSG: dict = {}

# Delay between successive copies into BACKUP_CHANNEL. Telegram enforces a
# roughly 1-message/second limit per chat; firing copies back-to-back into
# the same channel is exactly what trips FloodWait mid-batch.
_COPY_SPACING_SECONDS = 0.4

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


async def _copy_with_flood_retry(coro_fn, *args, max_retries: int = 4, **kwargs):
    """
    Awaits coro_fn(*args, **kwargs), retrying on FloodWait instead of letting
    it bubble up as a generic failure. Pyrogram already auto-sleeps for short
    FloodWaits (below its sleep_threshold), but for a real batch the wait
    time between successive copies into the same channel can stack up past
    that threshold — this catches those explicitly rather than treating a
    rate limit as "the source message was deleted".
    """
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except FloodWait as e:
            last_err = e
            wait = e.value + 1
            logger.warning(f"FloodWait: sleeping {wait}s (attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(wait)
    raise last_err


async def _finalize_entries(client, items: list) -> tuple:
    """
    The ONLY place media is ever copied into BACKUP_CHANNEL. Runs once a
    link is actually about to be created (Yes / /done), never before.
    Returns (entries, failures) — entries are ready for save_link(), and
    failures is a list of (src_chat_id, src_message_id, error_str) for
    anything that genuinely couldn't be copied, so callers can report the
    real reason instead of guessing.
    """
    entries = []
    failures = []
    groups = _group_pending(items)
    for i, group in enumerate(groups):
        src_chat_id = group[0]["src_chat_id"]
        src_message_id = group[0]["src_message_id"]
        try:
            if len(group) > 1:
                copied = await _copy_with_flood_retry(
                    client.copy_media_group, BACKUP_CHANNEL, src_chat_id, src_message_id
                )
            else:
                copied = [await _copy_with_flood_retry(
                    client.copy_message, BACKUP_CHANNEL, src_chat_id, src_message_id
                )]
        except Exception as e:
            logger.warning(f"Couldn't back up message {src_chat_id}/{src_message_id}: {e}")
            failures.append((src_chat_id, src_message_id, str(e)))
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

        # Space out successive copies so we don't trigger the very FloodWait
        # we're trying to avoid. No point sleeping after the last group.
        if i < len(groups) - 1:
            await asyncio.sleep(_COPY_SPACING_SECONDS)

    return entries, failures


async def _report_failures(client, failures: list, context: str):
    """Best-effort: dump the real exception text to LOG_CHANNEL so failures
    are actually diagnosable instead of living only in a log file nobody sees."""
    if not failures or not LOG_CHANNEL:
        return
    lines = [f"⚠️ {context}: {len(failures)} item(s) failed to back up:"]
    for src_chat_id, src_message_id, err in failures[:10]:
        lines.append(f"• {src_chat_id}/{src_message_id} — {err}")
    if len(failures) > 10:
        lines.append(f"...and {len(failures) - 10} more.")
    try:
        await client.send_message(LOG_CHANNEL, "\n".join(lines))
    except Exception:
        pass


def _batch_controls(count: int) -> InlineKeyboardMarkup:
    """The Yes/No-style controls attached to the running counter message,
    replacing the need to type /done or /cancel."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Yes, finish ({count})", callback_data="batchctl:done"),
        InlineKeyboardButton("❌ No, cancel", callback_data="batchctl:cancel"),
    ]])


async def _finalize_and_report_batch(client, admin_id: int, items: list, status: Message, mention: str):
    """Shared tail-end of the batch flow, used by both /done and the
    ✅ Yes button so they behave identically."""
    entries, failures = await _finalize_entries(client, items)
    await _report_failures(client, failures, f"Batch by {mention}")
    if not entries:
        detail = failures[0][2] if failures else "unknown error"
        note = " Check the log channel for details." if LOG_CHANNEL else f"\n\nReason: {detail}"
        await status.edit_text(
            f"❌ Couldn't back up any of those {len(items)} file(s) — this usually means Telegram "
            f"rate-limited the backup channel mid-batch, not that the files were deleted. "
            f"Try /batch again with fewer files, or wait a moment first.{note}"
        )
        return
    if failures:
        await status.edit_text(
            f"⚠️ {len(failures)} of {len(items)} file(s) couldn't be backed up and were skipped "
            f"(likely rate-limited). Generating a link for the {len(entries)} that succeeded..."
        )

    code = await save_link(admin_id, entries, is_batch=True)
    link = build_deep_link(code)
    await status.edit_text(
        f"✅ <b>Batch link ready</b> ({len(entries)} file(s))\n\n<code>{link}</code>"
    )
    if LOG_CHANNEL:
        await client.send_message(LOG_CHANNEL, f"📦 Batch link created by {mention}: {link}")


@Client.on_message(filters.command("batch") & filters.private & admin_filter)
async def start_batch(client, message: Message):
    BATCH_SESSIONS[message.from_user.id] = []
    _BATCH_STATUS_MSG.pop(message.from_user.id, None)
    await message.reply_text(
        "📦 <b>Batch mode started.</b>\n"
        "Send me all the files you want in this link.\n"
        "A counter will appear below with ✅ Yes / ❌ No buttons — tap "
        "✅ Yes when you're done to generate the link, or ❌ No to discard "
        "the batch.\n"
        "Nothing is uploaded to storage until you tap ✅ Yes."
    )


@Client.on_message(filters.command("cancel") & filters.private & admin_filter)
async def cancel_batch(client, message: Message):
    admin_id = message.from_user.id
    async with _lock_for(admin_id):
        _BATCH_STATUS_MSG.pop(admin_id, None)
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
    _BATCH_STATUS_MSG.pop(admin_id, None)
    await _finalize_and_report_batch(client, admin_id, items, status, message.from_user.mention)


@Client.on_callback_query(filters.regex(r"^batchctl:") & admin_filter)
async def batch_controls(client, query: CallbackQuery):
    admin_id = query.from_user.id
    decision = query.data.split(":", 1)[1]

    async with _lock_for(admin_id):
        items = BATCH_SESSIONS.pop(admin_id, None)
        _BATCH_STATUS_MSG.pop(admin_id, None)

    if items is None:
        await query.answer("That batch already finished or expired.", show_alert=True)
        return

    if decision == "cancel":
        await query.answer()
        await query.message.edit_text(
            "🗑 Batch discarded — nothing was ever stored.", reply_markup=None
        )
        return

    if not items:
        await query.answer()
        await query.message.edit_text(
            "That batch was empty — nothing to link.", reply_markup=None
        )
        return

    await query.answer()
    await query.message.edit_text(
        "⏳ Saving files and generating your link...", reply_markup=None
    )
    await _finalize_and_report_batch(
        client, admin_id, items, query.message, query.from_user.mention
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
            # Handlers for different messages can run concurrently (e.g. an
            # album's get_media_group() await lets a later, faster single-file
            # message finish appending first). message.id is monotonically
            # increasing per chat, so re-sorting on every append guarantees
            # the stored order always matches the order messages were
            # actually sent in, regardless of which handler finished first.
            BATCH_SESSIONS[admin_id].sort(key=lambda it: it["src_message_id"])
            try:
                await message.react(emoji="👍")
            except Exception:
                pass

            # One running counter message, edited in place, instead of a new
            # message per file — stays quiet even for big batches. Carries
            # the ✅ Yes / ❌ No controls so /done and /cancel aren't required.
            count = len(BATCH_SESSIONS[admin_id])
            label = "file" if count == 1 else "files"
            text = f"📥 {count} {label} received in batch so far."
            markup = _batch_controls(count)
            status_msg = _BATCH_STATUS_MSG.get(admin_id)
            try:
                if status_msg:
                    await status_msg.edit_text(text, reply_markup=markup)
                else:
                    _BATCH_STATUS_MSG[admin_id] = await message.reply_text(text, reply_markup=markup)
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

    entries, failures = await _finalize_entries(client, items)
    await _report_failures(client, failures, f"Single upload by {query.from_user.mention}")
    if not entries:
        detail = failures[0][2] if failures else "unknown error"
        note = " Check the log channel for details." if LOG_CHANNEL else f"\n\nReason: {detail}"
        await query.message.edit_text(
            f"❌ Couldn't back up that file.{note}"
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


 
