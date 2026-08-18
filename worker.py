import os
import logging
import asyncio
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
import main

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def run():
    token = main.TELEGRAM_TOKEN
    if not token:
        logger.warning("TELEGRAM_TOKEN not set; worker will not start.")
        return

    app = ApplicationBuilder().token(token).build()
    # Reuse the handlers defined in main.py
    try:
        app.add_handler(CommandHandler('start', main.start))
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), main.handle_message))
    except Exception as e:
        logger.exception("Failed to add handlers from main")

    logger.info("Worker: starting Telegram polling...")
    try:
        asyncio.run(app.run_polling())
    except Exception:
        logger.exception("Worker: polling stopped with exception")


if __name__ == '__main__':
    run()
