"""
Wheelhouse Telegram Bot — WheelhouseCape_bot

Runs as a separate systemd service on the same Beelink as the Flask app.
Polls Telegram, forwards authorized messages to the internal bot advisor
endpoint, and relays the response back to the user.

Deployment: /opt/wheelhouse-bot/bot.py (see WHEELHOUSE_CLAUDE_CODE_BRIEF.md)
"""

import os
import logging
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
BOT_SECRET_KEY = os.environ['BOT_SECRET_KEY']
WHEELHOUSE_URL = os.environ.get('WHEELHOUSE_URL', 'http://127.0.0.1:8090')
ALLOWED_IDS = {
    chat_id.strip()
    for chat_id in os.environ.get('ALLOWED_CHAT_IDS', '').split(',')
    if chat_id.strip()
}
MAX_HISTORY = 10  # number of exchanges (user+assistant pairs) kept per user

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger('wheelhouse-bot')

# Per-user conversation history: {chat_id: [{"role": ..., "content": ...}, ...]}
conversations = {}


def is_authorized(chat_id: int) -> bool:
    return str(chat_id) in ALLOWED_IDS


def call_advisor(chat_id: int, user_message: str) -> str:
    """Call the Wheelhouse bot advisor endpoint and update history on success."""
    history = conversations.get(chat_id, [])
    try:
        resp = requests.post(
            f'{WHEELHOUSE_URL}/api/bot/advisor',
            headers={
                'Content-Type': 'application/json',
                'X-Bot-Key': BOT_SECRET_KEY,
            },
            json={'messages': history, 'message': user_message},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        reply = data.get('reply', 'No response from advisor.')
    except Exception as e:
        logger.error(f'Advisor call failed: {e}')
        return 'Sorry, could not reach the Wheelhouse advisor. Try again in a moment.'

    # Update history only on successful call
    history = history + [
        {'role': 'user', 'content': user_message},
        {'role': 'assistant', 'content': reply},
    ]
    if len(history) > MAX_HISTORY * 2:
        history = history[-(MAX_HISTORY * 2):]
    conversations[chat_id] = history

    return reply


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        logger.info(f'Rejected /start from unauthorized chat_id={chat_id}')
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return
    conversations[chat_id] = []
    await update.message.reply_text(
        "\u2693 Wheelhouse Advisor ready.\n\n"
        "Ask me about conditions, tides, currents, weather, or where to fish. "
        "I have live NOAA, NWS, and buoy data.\n\n"
        "Try: \"Leaving Ryder's Cove tomorrow at 6 AM — what's the plan?\"\n\n"
        "/reset — clear conversation history\n"
        "/status — check bot status"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        return
    conversations[chat_id] = []
    await update.message.reply_text("Conversation cleared. Fresh start.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        return
    history_len = len(conversations.get(chat_id, [])) // 2
    await update.message.reply_text(
        f"\u2693 Wheelhouse bot is running. {history_len} exchanges in current session."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        logger.info(f'Rejected message from unauthorized chat_id={chat_id}')
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return

    user_message = update.message.text
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    reply = call_advisor(chat_id, user_message)

    # Telegram has a 4096 char limit per message — split if needed
    if len(reply) <= 4096:
        await update.message.reply_text(reply)
    else:
        for i in range(0, len(reply), 4096):
            await update.message.reply_text(reply[i:i + 4096])


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('reset', reset))
    app.add_handler(CommandHandler('status', status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info('Wheelhouse bot started')
    app.run_polling()


if __name__ == '__main__':
    main()
