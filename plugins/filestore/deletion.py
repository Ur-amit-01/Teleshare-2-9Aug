"""
deletion.py — auto-delete for delivered files.

Every scheduled deletion is written to the `pending_deletions` collection
first, so if the bot restarts mid-wait, restore_pending_deletions() (called
from bot.py at startup) picks it back up instead of losing it.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from plugins.helper.db import db

logger = logging.getLogger(__name__)


async def _delete_later(client, job_id, chat_id: int, message_ids: list, delay: float):
    try:
        await asyncio.sleep(delay)
        await client.delete_messages(chat_id, message_ids)
    except Exception as e:
        logger.warning(f"Auto-delete failed for job {job_id}: {e}")
    finally:
        await db.remove_pending_deletion(job_id)


async def schedule_deletion(client, chat_id: int, message_ids: list, delay: int):
    """Schedule `message_ids` in `chat_id` to be deleted `delay` seconds from now."""
    if delay <= 0 or not message_ids:
        return
    doc = {
        "chat_id": chat_id,
        "message_ids": message_ids,
        "delete_at": datetime.utcnow() + timedelta(seconds=delay),
    }
    job_id = await db.add_pending_deletion(doc)
    asyncio.create_task(_delete_later(client, job_id, chat_id, message_ids, delay))


async def restore_pending_deletions(client):
    """Call once at startup to resume any auto-delete jobs that survived a restart."""
    jobs = await db.get_all_pending_deletions()
    now = datetime.utcnow()
    for job in jobs:
        remaining = (job["delete_at"] - now).total_seconds()
        asyncio.create_task(
            _delete_later(
                client, job["_id"], job["chat_id"], job["message_ids"], max(remaining, 0)
            )
        )
    if jobs:
        logger.info(f"Restored {len(jobs)} pending auto-delete job(s).")
