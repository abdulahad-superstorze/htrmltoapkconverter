"""HTML TO APK CONVERTER - Telegram bot entry point.

Run with:  python main.py      (BOT_TOKEN must be set as an environment variable)
"""
import logging
import signal

from config import BRAND_NAME, MAX_CONCURRENT_BUILDS, VERSION
from core import bot, logger
import handlers
from ui import ensure_banner


def _check_token():
    """Fail fast with a clear message when the token is wrong."""
    try:
        me = bot.get_me()
    except Exception as exc:
        logger.error("Telegram rejected BOT_TOKEN or could not be reached (%s).", type(exc).__name__)
        raise SystemExit(1)
    logger.info("Logged in as @%s", me.username)


def _shutdown(signum, frame):
    logger.info("Shutdown signal received; stopping…")
    bot.stop_polling()


def main():
    logger.info("%s v%s starting…", BRAND_NAME, VERSION)
    _check_token()
    try:
        bot.remove_webhook()  # polling cannot run while a webhook is active
    except Exception:
        logger.warning("Could not clear the webhook")
    ensure_banner()
    handlers.configure_bot_profile()
    handlers.start_janitor()
    signal.signal(signal.SIGTERM, _shutdown)
    logger.info("Ready. Up to %d concurrent builds.", MAX_CONCURRENT_BUILDS)
    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30,
        logger_level=logging.WARNING,
        allowed_updates=["message", "callback_query"],
    )
    logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
