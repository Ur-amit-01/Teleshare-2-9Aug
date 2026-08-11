from pyrogram import Client, filters
from pyrogram.types import Message

from plugins.helper.db import db
from plugins.helper.settings import settings
from plugins.helper.force_sub import ensure_subscribed
from plugins.filestore.delivery import deliver


@Client.on_message(filters.private & filters.command("start"))
async def start(client, message: Message):
    await db.add_user(message.from_user.id)

    if not await ensure_subscribed(client, message):
        return  # join-required message already sent

    if len(message.command) > 1:
        code = message.command[1]
        await deliver(client, message.from_user.id, code)
        return

    text = settings.get("start_text").format(mention=message.from_user.mention)
    await message.reply_text(text)
    
