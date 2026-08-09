"""linking.py — shared code-generation + Files-doc creation used by upload & range_files."""
import secrets
import string

import config
from config import BACKUP_CHANNEL
from plugins.helper.db import db

_ALPHABET = string.ascii_letters + string.digits


async def generate_code(length: int = 8) -> str:
    while True:
        code = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if not await db.get_file_link(code):
            return code


async def save_link(admin_id: int, messages: list, is_batch: bool) -> str:
    """messages: list of {message_id, media_type, media_group_id, caption}"""
    code = await generate_code()
    await db.create_file_link(
        code,
        {
            "messages": messages,
            "is_batch": is_batch,
            "backup_chat_id": BACKUP_CHANNEL,
            "created_by": admin_id,
            "protect_content": None,  # None = follow global setting
            "auto_delete": None,      # None = follow global setting
        },
    )
    return code


def build_deep_link(code: str) -> str:
    return f"https://t.me/{config.BOT_USERNAME}?start={code}"
