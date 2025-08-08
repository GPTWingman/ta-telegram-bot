import os
import logging
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler

# --- Logging (so Render shows stack traces) ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
assert TOKEN, "Missing TELEGRAM_TOKEN env var"

app = Flask(__name__)

# Build Telegram application (async)
tg_app = Application.builder().token(TOKEN).build()

# --- Command handlers ---
async def start(update: Update, _):
    if update.message:
        await update.message.reply_text("✅ Bot is alive. Send /ping to test.")

async def ping(update: Update, _):
    if update.message:
        await update.message.reply_text("🏓 Pong!")

async def chatid(update: Update, _):
    if update.message:
        await update.message.reply_text(f"Your chat_id is: {update.message.chat_id}")

tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CommandHandler("ping", ping))
tg_app.add_handler(CommandHandler("chatid", chatid))

# --- Initialize & start the Telegram app once at startup ---
# PTB v21 requires initialize() before using process_update()
_initialized = False
def ensure_initialized_once():
    global _initialized
    if not _initialized:
        logger.info("Initializing Telegram application…")
        asyncio.run(tg_app.initialize())
        asyncio.run(tg_app.start())
        _initialized = True
        logger.info("Telegram application initialized & started.")

# --- Health check ---
@app.get("/health")
def health():
    return "ok", 200

# --- Telegram webhook endpoint ---
@app.post("/webhook")
def webhook():
    try:
        ensure_initialized_once()
        data = request.get_json(force=True, silent=True)
        if not data:
            return "no json", 400
        update = Update.de_json(data, tg_app.bot)
        asyncio.run(tg_app.process_update(update))
        return "ok", 200
    except Exception as e:
        logger.exception("Error handling webhook")
        return f"error: {e}", 500

# --- Local dev runner (not used on Render) ---
if __name__ == "__main__":
    ensure_initialized_once()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
