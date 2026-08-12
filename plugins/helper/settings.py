"""
settings.py — the DB-backed, admin-editable settings layer.

This is deliberately separate from config.py:
  config.py  -> read once from the environment at boot, needs a restart to change
  settings   -> stored in Mongo, editable at runtime via /setting, cached in memory

Every key has a hard-coded default below so the bot works out of the box even
before an admin has touched /setting.
"""
from typing import Any, Dict

DEFAULTS: Dict[str, Any] = {
    # list of channel ids/usernames a user must join before using the bot.
    # entries can be plain "-100..." ids (normal channels) or channel ids that
    # use "request to join" — both are supported by the fsub filter.
    "force_sub_channels": [],
    # seconds to wait before deleting delivered files. 0 = never auto-delete.
    "auto_delete_time": 0,
    # shown right after delivering files when auto-delete is enabled.
    "auto_delete_notice": "⏳ These file(s) will be auto-deleted in {time}. Forward/save them now.",
    # if True, delivered messages are sent with forwarding/saving disabled.
    "protect_content": False,
    # shown on a bare /start (no deep-link payload), and resent (without a
    # photo re-check — see start_photo below) after an auto-deleted file's
    # "get it again" moment. {mention} is available but optional.
    "start_text": (
        "> 📚 𝗡𝗘𝗘𝗧 𝗦𝗧𝗨𝗗𝗬 𝗠𝗔𝗧𝗘𝗥𝗜𝗔𝗟𝗦\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "**     ✦ Choose a material below**\n"
        "**     ✦ Tap the button to get PDF**"
    ),
    # (label, deep-link code) pairs rendered as inline buttons under
    # start_text — see plugins/helper/start_message.py for how these are
    # laid out into rows.
    "start_materials": [
        ("All Med Easy", "JAKWudVK"),
        ("Anand Mani Pocket Book", "1QCmMsuK"),
        ("MTG Fingertips", "y02GqyvF"),
        ("Vedantu TATVA", "WWz834R9"),
        ("HC Verma", "FbBsxeOM"),
        ("NCERT Punch", "isYY6kAB"),
        ("Unacademy Modules", "dRqVII4n"),
        ("Cengage Materials", "KvwSKgKr"),
        ("Allen Modules", "otyvXl9p"),
        ("PYQs", "cyHbUpA2"),
        ("PW Modules", "6wLXrHjl"),
        ("Masterclass in Biology", "pLeOXBL6"),
        ("Aakash Modules", "K9mTX0y7"),
        ("PW Real test series (Phase-2)", "ejcgwRo5"),
        ("Flashcards", "xmF0j2u8"),
    ],
    # file_id (after an admin uploads a photo via /setting) or a direct
    # image URL to send above start_text on /start and after an
    # auto-deleted file. Empty = text-only, no photo.
    "start_photo": "",
    # extra line appended under every delivered file's caption, {} unused if empty.
    "custom_caption": "",
    # DMed to a user the moment they leave/are removed from a force-sub
    # channel. {mention} and {chat_title} are available. Empty/"none" via
    # /setting disables this feature entirely.
    "leave_message": (
        "💔 {mention}, you just left {chat_title}...\n\n"
        "We really don't want to lose you like this. Come back? "
        "It only takes a second and we'd love to have you around again."
    ),
}


class SettingsManager:
    def __init__(self, db):
        self.db = db
        self._cache: Dict[str, Any] = dict(DEFAULTS)

    async def load(self):
        """Call once at boot: pulls saved overrides from Mongo into the cache."""
        stored = await self.db.get_all_settings()
        self._cache = dict(DEFAULTS)
        self._cache.update(stored)

    def get(self, key: str) -> Any:
        return self._cache.get(key, DEFAULTS.get(key))

    async def set(self, key: str, value: Any):
        self._cache[key] = value
        await self.db.save_setting(key, value)

    def all(self) -> Dict[str, Any]:
        return dict(self._cache)


from plugins.helper.db import db  # noqa: E402  (avoid circular import at module load)

# Single shared instance used by every plugin. Call `await settings.load()` at boot.
settings = SettingsManager(db)
