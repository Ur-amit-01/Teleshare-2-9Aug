from pyrogram import Client, filters
from pyrogram.types import Message

from config import BACKUP_CHANNEL
from plugins.helper.db import db
from plugins.helper.filters import admin_filter


@Client.on_message(filters.command("delete_link") & filters.private & admin_filter)
async def delete_link(client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: <code>/delete_link CODE</code>")
        return

    code = message.command[1]
    file_doc = await db.get_file_link(code)
    if not file_doc:
        await message.reply_text("❌ No such link.")
        return

    message_ids = [e["message_id"] for e in file_doc["messages"]]
    try:
        await client.delete_messages(BACKUP_CHANNEL, message_ids)
    except Exception:
        pass  # backup copies may already be gone — link record deletion still proceeds

    await db.delete_file_link(code)
    await message.reply_text(f"🗑 Link <code>{code}</code> and its backed-up file(s) deleted.")
