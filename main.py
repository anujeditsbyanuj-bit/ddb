

import os
import sys
import logging
import threading
from pathlib import Path

from flask import Flask

# Setup logging FIRST
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log')
    ]
)

logger = logging.getLogger(__name__)

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
    logger.info("Environment variables loaded from .env")
except ImportError:
    logger.warning("python-dotenv not installed, using system environment variables")

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)

# Import handlers and settings (FIXED: import order)
from config.settings import TELEGRAM_BOT_TOKEN, DOWNLOAD_DIR
from bot.handlers import (
    start_command,
    help_command,
    about_command,
    handle_message,
    button_callback,
    error_handler
)



# Render health server
WEB_PORT = int(os.environ.get("PORT", "10000"))
health_app = Flask(__name__)

@health_app.get("/")
def health_check():
    return "DiskWala Bot is running", 200

@health_app.get("/health")
def health():
    return "OK", 200

def start_health_server():
    health_app.run(host="0.0.0.0", port=WEB_PORT, threaded=True, use_reloader=False)


def validate_environment():
    """Validate all required environment variables"""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("=" * 50)
        logger.error("MISSING REQUIRED ENVIRONMENT VARIABLES")
        logger.error("=" * 50)
        logger.error("❌ TELEGRAM_BOT_TOKEN is not set")
        logger.error("=" * 50)
        logger.error("Please set the required environment variables in .env file")
        logger.error("You can copy .env.example to .env and fill in your values")
        logger.error("=" * 50)
        return False
    
    logger.info("✅ All required environment variables are set")
    return True


def main():
    """Start the bot (FIXED: complete implementation)"""
    logger.info("=" * 50)
    logger.info("DiskWala Video Downloader Bot")
    logger.info("Version: 1.0.0")
    logger.info("=" * 50)
    
    if not validate_environment():
        sys.exit(1)
    
    # Create download directory
    Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
    logger.info(f"Download directory: {os.path.abspath(DOWNLOAD_DIR)}")
    
    logger.info("Starting DiskWala Downloader Bot...")

    # Render requires the service to listen on the assigned PORT.
    threading.Thread(target=start_health_server, daemon=True).start()
    logger.info(f"Render health server listening on 0.0.0.0:{WEB_PORT}")

    try:
        # Create application
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Add handlers
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("about", about_command))
        application.add_handler(CallbackQueryHandler(button_callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        # Add error handler
        application.add_error_handler(error_handler)
        
        logger.info("✅ Bot started successfully!")
        logger.info("Polling for updates...")
        logger.info("Press Ctrl+C to stop the bot")
        logger.info("=" * 50)
        
        # Start polling
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    
    except Exception as e:
        logger.error(f"Failed to start bot: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n" + "=" * 50)
        logger.info("Bot stopped by user")
        logger.info("=" * 50)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
