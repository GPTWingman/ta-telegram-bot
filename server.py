import os
import logging
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler
import json, re, logging
from collections import defaultdict
from telegram.ext import CommandHandler, ContextTypes
from openai import OpenAI

# Initialize OpenAI client
client = OpenAI()  # Uses OPENAI_API_KEY from environment

# Cache for latest TA data
LATEST = defaultdict(dict)   # LATEST[symbol][timeframe] = payload dict
LAST_PAYLOAD = None

# Helper functions
def _to_float_or_none(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    if isinstance(v, str):
        s = v.strip().lower().replace(",", "")
        if s in {"na", "nan", ""}: return None
        try: return float(s)
        except: return None
    return None

def _norm_tf(x):
    s = str(x or "").upper().strip()
    return (s.replace("MIN","M").replace("HOUR","H")
             .replace("HOURS","H").replace("4HOURS","4H")
             .replace("1HOUR","1H") or "240")


# --- Logging (so Render shows stack traces) ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
assert TOKEN, "Missing TELEGRAM_TOKEN env var"

app = Flask(__name__)

symbol = payload.get("symbol", "UNKNOWN")
tf = str(payload.get("timeframe", "NA")).upper()

# Save latest TA to cache
LATEST[symbol][tf] = payload
global LAST_PAYLOAD
LAST_PAYLOAD = payload

# Build Telegram application (async)
tg_app = Application.builder().token(TOKEN).build()

# --- Command handlers ---
async def analyze_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        symbol = tf = None

        if len(args) >= 1:
            symbol = args[0].upper()
        if len(args) >= 2:
            tf = _norm_tf(args[1])

        # Pick which payload to analyze
        data = None
        if symbol and tf:
            data = LATEST.get(symbol, {}).get(tf)
        elif symbol:
            if "240" in LATEST.get(symbol, {}):
                data = LATEST[symbol]["240"]
            else:
                tfs = list(LATEST.get(symbol, {}).keys())
                data = LATEST[symbol][tfs[0]] if tfs else None
        else:
            data = LAST_PAYLOAD

        if not data:
            await update.message.reply_text(
                "No cached TradingView data yet for that request.\n"
                "Usage: /analyze <SYMBOL> [TF]\nExample: /analyze BINANCE:PYTHUSDT 240"
            )
            return

        # Extract fields
        sym   = data.get("symbol", "UNKNOWN")
        tf_in = str(data.get("timeframe", "NA"))
        price = _to_float_or_none(data.get("price"))
        rsi   = _to_float_or_none(data.get("rsi"))
        ema20 = _to_float_or_none(data.get("ema20") or data.get("ema_fast"))
        ema50 = _to_float_or_none(data.get("ema50") or data.get("ema_slow"))
        macd  = _to_float_or_none(data.get("macd"))
        macds = _to_float_or_none(data.get("macd_signal"))
        macdh = _to_float_or_none(data.get("macd_hist"))
        atr   = _to_float_or_none(data.get("atr"))

        # Prompt for analysis
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        prompt = f"""
You are Wingman, a pro crypto TA analyst. Analyze these indicators and return a concise, actionable plan.

Symbol: {sym}
Timeframe: {tf_in}
Price: {price}
RSI(14): {rsi}
EMA20: {ema20}
EMA50: {ema50}
MACD: {macd}
MACD Signal: {macds}
MACD Hist: {macdh}
ATR: {atr}

Tasks:
1) Market structure & trend (bullish/bearish/range) and confidence.
2) Entry plan: immediate vs pullback. Give exact levels.
3) Invalidation (stop-loss) based on structure/ATR.
4) TP ladder: 3–5 targets with rationale.
5) Risk note: key risks, what would invalidate the idea.
Keep it tight, clear, and in bullet points.
"""

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role":"system","content":"You are Wingman, a precise, risk-aware crypto TA analyst. Be concise and actionable."},
                {"role":"user","content":prompt}
            ],
            temperature=0.2,
        )

        text = resp.choices[0].message.content.strip()
        await update.message.reply_text(text)
    except Exception as e:
        logging.exception("analyze_cmd error")
        await update.message.reply_text(f"Error in /analyze: {e}")

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
tg_app.add_handler(CommandHandler("analyze", analyze_cmd))

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
