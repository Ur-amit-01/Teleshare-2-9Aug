"""
start_message.py — builds/sends the bot's "start" content: an optional
photo plus the start_text setting, with the top-level Test Series menu
(one button per institute) rendered as inline buttons.

`main_menu_content()` is the shared builder — both the bare /start handler
and the "🏠 Main Menu" / "⬅️ Back" buttons inside the Test Series menu
(plugins/filestore/test_series.py) call it, so the two never drift apart.

Kept in its own module (rather than inside start.py) so deletion.py can
import it without a circular import through delivery.py -> deletion.py.
"""
import logging

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.helper.settings import settings
from plugins.helper.db import db
from plugins.helper.photo_ref import send_photo_ref

logger = logging.getLogger(__name__)


# A row is filled with buttons as long as the combined label length stays
# under this, so short labels pair up while long ones get a full row to
# themselves and never get visually cramped.
_MAX_ROW_WIDTH = 30
# Hard cap on buttons per row — even if two short labels would fit width-
# wise, more than this starts looking cramped on narrow phone screens.
_MAX_PER_ROW = 2


def _arrange_buttons(buttons: list) -> list:
    """Greedily pack InlineKeyboardButton objects into rows, using each
    button's label length as its "width". Keeps short-label buttons
    together on one row and gives long-label buttons a row of their own,
    so no button's text ever gets visually cramped or cut off."""
    rows, row, row_width = [], [], 0
    for button in buttons:
        label_width = len(button.text)
        if row and (
            row_width + label_width > _MAX_ROW_WIDTH
            or len(row) >= _MAX_PER_ROW
        ):
            rows.append(row)
            row, row_width = [], 0
        row.append(button)
        row_width += label_width
    if row:
        rows.append(row)
    return rows


async def _institutes_keyboard() -> InlineKeyboardMarkup | None:
    institutes = await db.get_all_institutes()
    if not institutes:
        return None
    buttons = [
        InlineKeyboardButton(f"🏫 {inst['name']}", callback_data=f"ts:inst:{inst['_id']}")
        for inst in institutes
    ]
    return InlineKeyboardMarkup(_arrange_buttons(buttons))


async def main_menu_content(mention: str = None):
    """Returns (text, reply_markup) for the top-level Test Series menu —
    shared by send_start_message() and the ts:main callback."""
    text = settings.get("start_text")
    if mention:
        try:
            text = text.format(mention=mention)
        except (KeyError, IndexError):
            pass  # text has no {mention} placeholder (or a stray brace) — send as-is
    reply_markup = await _institutes_keyboard()
    return text, reply_markup


async def send_start_message(client, chat_id: int, mention: str = None):
    """Send the admin-configured start photo (if any) with the start_text
    caption/message, plus a button for every institute, to chat_id."""
    text, reply_markup = await main_menu_content(mention)
    photo = settings.get("start_photo")
    if photo:
        try:
            await send_photo_ref(client, chat_id, photo, caption=text, reply_markup=reply_markup)
            return
        except Exception as e:
            # A bad reference in start_photo (dead URL, a legacy file_id
            # from before a BOT_TOKEN swap, etc.) must not take down every
            # /start — fall through to the text-only send below instead of
            # crashing the handler.
            logger.warning(
                f"start_photo ({photo!r}) failed to send, falling back to text-only: {e}"
            )
    await client.send_message(
        chat_id, text, disable_web_page_preview=True, reply_markup=reply_markup
    )

  
