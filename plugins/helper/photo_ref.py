"""
photo_ref.py — image references that survive a BOT_TOKEN swap.

A raw Telegram file_id is scoped to the bot that received it: switching
BOT_TOKEN (e.g. spinning up a fresh bot after a copyright strike) silently
breaks every stored file_id, because the new bot never received that file
itself and Telegram won't let it resend a file_id it doesn't recognize.

To dodge that, any photo an admin sets (start photo, institute image,
series image) is immediately reposted into BACKUP_CHANNEL — the same
channel plugins/filestore/delivery.py already uses for the exact same
reason — and only a small reference to that channel message is stored,
never the file_id itself. Any bot that's an admin of BACKUP_CHANNEL can
copy a message out of it regardless of which bot originally posted it, so
these references keep working across a token swap as long as
BACKUP_CHANNEL itself (and the new bot's admin rights on it) stay the
same.

A stored reference is one of:
  - a plain http(s) URL string — already token-independent, stored as-is
  - "backup:<message_id>"       — a photo living in BACKUP_CHANNEL

Note: values saved *before* this module existed are raw file_ids and will
still break on a new bot token — they fall through to the legacy branch
in send_photo_ref() below and need to be re-set once (through the normal
/setting or Test Series admin flow) to become portable.
"""
from config import BACKUP_CHANNEL

_PREFIX = "backup:"


def is_url_ref(ref: str) -> bool:
    return bool(ref) and ref.lower().startswith(("http://", "https://"))


async def store_photo_ref(client, file_id: str) -> str:
    """Reposts an admin-supplied photo (a file_id straight off an incoming
    message.photo) into BACKUP_CHANNEL and returns the reference string to
    save in the DB/settings.

    Raises whatever exception Telegram raises on failure — most commonly
    when file_id came from a "restrict saving content" forward, which
    Telegram won't let any bot resend at all. Callers should catch this
    and ask the admin to upload the image fresh instead of forwarding it.
    """
    msg = await client.send_photo(BACKUP_CHANNEL, photo=file_id)
    return f"{_PREFIX}{msg.id}"


async def send_photo_ref(client, chat_id: int, ref: str, caption: str = None, reply_markup=None):
    """Sends the photo a stored reference points to, to chat_id, and
    returns the resulting Message. Handles both kinds of reference (see
    module docstring), plus legacy raw file_ids saved before this module
    existed (which only work as long as the bot token hasn't changed)."""
    if ref.startswith(_PREFIX):
        message_id = int(ref[len(_PREFIX):])
        return await client.copy_message(
            chat_id, BACKUP_CHANNEL, message_id, caption=caption, reply_markup=reply_markup
        )
    return await client.send_photo(chat_id, photo=ref, caption=caption, reply_markup=reply_markup)


async def resolve_photo_source(client, ref: str) -> str:
    """Returns a usable photo source (a URL or file_id) for APIs that need
    actual media rather than a reference — e.g. InputMediaPhoto for
    edit_media — by resolving a 'backup:<message_id>' reference to the
    file_id of the photo living in BACKUP_CHANNEL. URL refs and legacy raw
    file_ids pass through unchanged."""
    if ref.startswith(_PREFIX):
        message_id = int(ref[len(_PREFIX):])
        msg = await client.get_messages(BACKUP_CHANNEL, message_id)
        return msg.photo.file_id
    return ref

