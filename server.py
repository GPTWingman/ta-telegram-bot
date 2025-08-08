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

import json
from telegram.constants import ParseMode

@app.get("/tv/test")
def tv_test():
    try:
        ensure_initialized_once()
        chat_id = int(os.environ["CHAT_ID"])
        asyncio.run(tg_app.bot.send_message(chat_id=chat_id, text="✅ /tv/test reached OK"))
        return "ok", 200
    except Exception as e:
        app.logger.exception("Error in /tv/test")
        return f"error: {e}", 500


@app.post("/tv")
def tv_webhook():
    try:
        ensure_initialized_once()
        payload = request.get_json(force=True, silent=True)
        if not payload:
            return "no json", 400

        # Basic auth via shared secret inside the JSON
        secret = os.environ.get("ALERT_SECRET")
        if not secret or payload.get("secret") != secret:
            return "unauthorized", 401

        # Extract fields (tolerant to missing keys)
        symbol = payload.get("symbol", "UNKNOWN")
        tf = payload.get("timeframe", "NA")
        price = payload.get("price")
        rsi = payload.get("rsi")
        macd = payload.get("macd")
        macdsig = payload.get("macd_signal")
        macdhist = payload.get("macd_hist")
        ema20 = payload.get("ema20")
        ema50 = payload.get("ema50")
        atr = payload.get("atr")
        note = payload.get("note", "")

        msg = (
            f"📡 *TV Alert*\n"
            f"• Symbol: *{symbol}*  ({tf})\n"
            f"• Price: `{price}`\n"
            f"• RSI(14): `{rsi}`\n"
            f"• MACD: `{macd}`  Sig: `{macdsig}`  Hist: `{macdhist}`\n"
            f"• EMA20: `{ema20}`  EMA50: `{ema50}`\n"
            f"• ATR: `{atr}`\n"
            f"{'• Note: ' + note if note else ''}"
        )

        chat_id = int(os.environ["CHAT_ID"])
        asyncio.run(tg_app.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN))
        return "ok", 200
    except Exception as e:
        app.logger.exception("Error in /tv")
        return f"error: {e}", 500

# --- Local dev runner (not used on Render) ---
if __name__ == "__main__":
    ensure_initialized_once()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
