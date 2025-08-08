import os
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler

TOKEN = os.environ.get("TELEGRAM_TOKEN")
app = Flask(__name__)

# We create the Telegram app once and reuse it
tg_app = Application.builder().token(TOKEN).build()

async def start(update: Update, _):
    await update.message.reply_text("✅ Bot is alive. Send /ping to test.")

async def ping(update: Update, _):
    await update.message.reply_text("🏓 Pong!")

tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CommandHandler("ping", ping))

@app.get("/health")
def health():
    return "ok", 200

# Telegram will POST updates here (webhook)
@app.post("/webhook")
def webhook():
    data = request.get_json(force=True, silent=True)
    if not data:
        return "no json", 400
    update = Update.de_json(data, tg_app.bot)
    import asyncio
    asyncio.run(tg_app.process_update(update))
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
