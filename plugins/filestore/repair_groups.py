"""
repair_groups.py — one-off admin command to fix links whose stored
media_group_id falsely claims several entries were backed up into
BACKUP_CHANNEL as one real Telegram album, when they were actually copied
in individually (see the bugfix in test_series.py's ts_apply_paper_media
and range_files.py).

Symptom this fixes: /start <code> (or a Test Series paper) fails with
"Copy failed ... message doesn't belong to a media group" in the logs,
immediately followed by a MEDIA_EMPTY error from the re-upload fallback.

Why this got WORSE after a BOT_TOKEN swap: the primary delivery path
(copy_message/copy_media_group straight from BACKUP_CHANNEL) never touches
file_id at all, so it's token-independent. But a broken grouping always
fell through to the re-upload fallback, which resends using the entries'
raw stored file_id — and a raw file_id only works for the bot that
originally received it. Under the old token that fallback still (silently)
worked, masking the bad grouping. After swapping BOT_TOKEN, those file_ids
became foreign to the new bot and the fallback started failing outright,
which is why links that "used to work" broke specifically after the swap.

Run /fixgroups once as an admin. It scans every saved link, and for any
group of entries that claim to share a media_group_id, verifies against
BACKUP_CHANNEL whether they're actually grouped there. If not, it clears
media_group_id on just those entries so delivery.py delivers them as
separate items (which always works, token or no token) instead of
attempting a copy_media_group that can never succeed. Nothing is deleted
and no message content changes — only the internal grouping flag.
"""
import logging

from pyrogram import Client, filters
from pyrogram.types import Message

from config import BACKUP_CHANNEL
from plugins.helper.db import db
from plugins.helper.filters import admin_filter
from plugins.filestore.delivery import _group_by_album

logger = logging.getLogger(__name__)


async def _is_real_group(client, group: list) -> bool:
    """True if `group` (entries sharing a media_group_id) actually exists
    as one Telegram album in BACKUP_CHANNEL, of the same size."""
    try:
        real = await client.get_media_group(BACKUP_CHANNEL, group[0]["message_id"])
    except Exception:
        return False
    return len(real) == len(group)


@Client.on_message(filters.command("fixgroups") & filters.private & admin_filter)
async def fix_groups(client, message: Message):
    status = await message.reply_text(
        "⏳ Scanning saved links for broken album groupings against "
        "BACKUP_CHANNEL... this can take a while for a large library."
    )

    checked_links = 0
    fixed_links = 0
    fixed_entries = 0

    cursor = db.files.find({"messages.media_group_id": {"$ne": None}})
    async for doc in cursor:
        entries = doc.get("messages") or []
        groups = _group_by_album(entries)
        if not any(len(g) > 1 for g in groups):
            continue  # nothing actually claims to be grouped in this link

        checked_links += 1
        changed = False
        for group in groups:
            if len(group) <= 1:
                continue
            if not await _is_real_group(client, group):
                for entry in group:
                    entry["media_group_id"] = None
                changed = True
                fixed_entries += len(group)

        if changed:
            await db.files.update_one(
                {"_id": doc["_id"]}, {"$set": {"messages": entries}}
            )
            fixed_links += 1

    await status.edit_text(
        "✅ <b>Done.</b>\n"
        f"• Links with grouped entries checked: <code>{checked_links}</code>\n"
        f"• Links repaired: <code>{fixed_links}</code>\n"
        f"• Entries un-grouped (now deliver individually): <code>{fixed_entries}</code>\n\n"
        "Repaired links will now deliver correctly regardless of BOT_TOKEN, "
        "since delivery no longer needs a real (but nonexistent) album copy "
        "for them."
    )
