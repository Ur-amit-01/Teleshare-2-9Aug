import os
import logging
import sys
import asyncio
from pyrogram import Client
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Track seen chats for NEW CHAT detection
_seen_chats = set()

# Read config directly from environment (set these in Koyeb's env var settings)
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

# name -> hint shown to the user if missing
REQUIRED_VARS = {
    "API_ID": "Get this from https://my.telegram.org",
    "API_HASH": "Get this from https://my.telegram.org",
    "SESSION_STRING": "Generate with a Pyrogram session-string script and paste the output",
}


def check_env():
    """Check for missing required environment variables and exit if any are missing."""
    problems = [(name, hint) for name, hint in REQUIRED_VARS.items() if not os.getenv(name)]
    if not problems:
        return

    logger.warning("⚠️  Missing/empty environment variable(s):")
    for name, hint in problems:
        logger.warning(f"   - {name}  ({hint})")

    logger.error(
        f"Cannot start: {', '.join(name for name, _ in problems)} must be set in Koyeb's "
        f"environment variables. Fix and restart."
    )
    sys.exit(1)


class UserBot(Client):
    def __init__(self):
        super().__init__(
            name="message_listener",
            api_id=int(API_ID),
            api_hash=API_HASH,
            session_string=SESSION_STRING,
            workers=50,
            sleep_threshold=5,
        )
        # Register message handler for both incoming and outgoing
        self.add_handler(MessageHandler(self._message_handler), group=0)

    async def _message_handler(self, client, message: Message):
        """Handle all incoming and outgoing messages."""
        try:
            chat = message.chat
            chat_name = chat.title or (chat.first_name or "Unknown")
            chat_id = chat.id

            # Check if this is a new chat
            is_new_chat = chat_id not in _seen_chats
            if is_new_chat:
                _seen_chats.add(chat_id)
                logger.info(f"🆕 NEW CHAT detected: {chat_name} (ID: {chat_id})")

            # Determine message direction
            if message.outgoing:
                direction = "➡️ OUTGOING"
            else:
                direction = "⬅️ INCOMING"

            # Extract sender info
            if message.from_user:
                sender_name = message.from_user.first_name or "Unknown"
                sender_username = f"(@{message.from_user.username})" if message.from_user.username else ""
                sender_id = message.from_user.id
            else:
                sender_name = "Unknown"
                sender_username = ""
                sender_id = "Unknown"

            # Extract message content
            if message.text:
                content = message.text.replace("\n", " ")[:200]  # Limit length for logs
                if len(message.text) > 200:
                    content += "..."
            elif message.caption:
                content = message.caption.replace("\n", " ")[:200]
                if len(message.caption) > 200:
                    content += "..."
            else:
                # Handle different media types
                if message.photo:
                    content = f"📷 Photo (size: {message.photo.width}x{message.photo.height})"
                elif message.video:
                    content = f"🎬 Video ({message.video.duration}s)"
                elif message.document:
                    content = f"📄 Document: {message.document.file_name or 'unnamed'}"
                elif message.audio:
                    content = f"🎵 Audio: {message.audio.title or 'unnamed'}"
                elif message.voice:
                    content = f"🎤 Voice message ({message.voice.duration}s)"
                elif message.sticker:
                    content = f"🖼️ Sticker: {message.sticker.emoji or ''}"
                elif message.animation:
                    content = "🎞️ GIF/Animation"
                elif message.video_note:
                    content = "📹 Video note"
                else:
                    content = "<media message>"

            # Build log message
            log_parts = [
                f"{direction}",
                f"Chat: {chat_name}",
                f"ID: {chat_id}",
                f"From: {sender_name} {sender_username} (ID: {sender_id})",
                f"Content: {content}"
            ]

            # Add reply info if present
            if message.reply_to_message:
                reply_sender = message.reply_to_message.from_user
                reply_name = reply_sender.first_name if reply_sender else "Unknown"
                log_parts.append(f"↩️ Replying to: {reply_name} (msg ID: {message.reply_to_message.id})")

            # Add forwarded info if present
            if message.forward_from:
                forward_name = message.forward_from.first_name or "Unknown"
                log_parts.append(f"↪️ Forwarded from: {forward_name}")
            elif message.forward_from_chat:
                forward_chat = message.forward_from_chat.title or "Unknown"
                log_parts.append(f"↪️ Forwarded from chat: {forward_chat}")

            # Add new chat tag
            if is_new_chat:
                log_parts.append("🆕 FIRST MESSAGE FROM THIS CHAT")

            logger.info(" | ".join(log_parts))

        except Exception as e:
            logger.error(f"❌ Error processing message: {e}")

    async def start(self):
        """Start the userbot and initialize."""
        await super().start()

        # Get user info
        me = await self.get_me()
        logger.info(f"✅ Logged in as: {me.first_name} (@{me.username})")
        logger.info(f"🆔 User ID: {me.id}")

        # Check if user is premium
        if me.is_premium:
            logger.info("⭐ Premium user account")

        # Load existing dialogs to identify new chats
        try:
            logger.info("📋 Loading existing dialogs...")
            dialog_count = 0
            async for dialog in self.get_dialogs():
                _seen_chats.add(dialog.chat.id)
                dialog_count += 1

                # Log first few dialogs for verification
                if dialog_count <= 5:
                    chat_name = dialog.chat.title or dialog.chat.first_name or "Unknown"
                    logger.info(f"   📂 Found: {chat_name} (ID: {dialog.chat.id})")

            logger.info(f"📊 Loaded {dialog_count} existing chats/dialogs")

        except Exception as e:
            logger.warning(f"⚠️ Could not preload dialogs: {e}")

        # Log startup status
        logger.info("=" * 60)
        logger.info("👂 MESSAGE LISTENER STARTED")
        logger.info("📨 Listening for all incoming AND outgoing messages")
        logger.info("🆕 New chats will be marked with 🆕")
        logger.info("📊 Listening to all private chats, groups, and channels")
        logger.info("=" * 60)

    async def stop(self, *args):
        """Stop the userbot gracefully."""
        logger.info("🛑 Stopping userbot...")
        await super().stop()
        logger.info("✅ Userbot stopped successfully.")


async def main():
    """Main entry point."""
    # Check environment before starting
    check_env()

    logger.info("🚀 Starting message listener userbot...")

    # Create and run userbot
    userbot = UserBot()

    try:
        async with userbot:
            # Keep the bot running
            await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("⏹️ Received interrupt signal, shutting down...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⏹️ Shutdown requested")
        sys.exit(0)
        
