from pyrogram import Client, filters
from pyrogram.types import Message

from plugins.helper.db import db
from plugins.helper.settings import settings
from plugins.helper.filters import ensure_subscribed
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


# Track "request to join" submissions for force-sub channels that use
# join-request mode, so ensure_subscribed() can treat them as satisfied.
@Client.on_chat_join_request()
async def track_join_request(client, chat_join_request):
    channel_ids = {
        ch if isinstance(ch, int) else None
        for ch in settings.get("force_sub_channels")
    }
    chat_id = chat_join_request.chat.id
    if chat_id in channel_ids or chat_join_request.chat.username in settings.get("force_sub_channels"):
        await db.record_join_request(chat_join_request.from_user.id, chat_id)
