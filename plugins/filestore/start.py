from pyrogram import Client, filters
from pyrogram.types import Message

from plugins.helper.db import db
from plugins.helper.force_sub import ensure_subscribed
from plugins.helper.start_message import send_start_message
from plugins.filestore.delivery import deliver


@Client.on_message(filters.private & filters.command("start"))
async def start(client, message: Message):
    await db.add_user(message.from_user.id)

    if len(message.command) > 1:
        # Only gate access when a user is actually trying to open a file —
        # a bare /start (just browsing/opening the bot) is never blocked.
        if not await ensure_subscribed(client, message):
            return  # join-required message already sent
        code = message.command[1]
        await deliver(client, message.from_user.id, code)
        return

    await send_start_message(client, message.chat.id, message.from_user.mention)

