import logging

from pyrogram import Client

import config
from config import API_ID, API_HASH, BOT_TOKEN
from plugins.helper.settings import settings
from plugins.filestore.deletion import restore_pending_deletions

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


class Bot(Client):
    def __init__(self):
        super().__init__(
            name="filestore_bot",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            workers=50,
            plugins={"root": "plugins"},
            sleep_threshold=5,
        )

    async def start(self):
        await super().start()
        me = await self.get_me()
        config.BOT_USERNAME = me.username

        await settings.load()
        await restore_pending_deletions(self)

        logger.info(f"{me.first_name} started as @{me.username}")

    async def stop(self, *args):
        await super().stop()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    Bot().run()
