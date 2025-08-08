# server.py
import os, json, re, asyncio, logging
from collections import defaultdict

from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from openai import OpenAI

# -------------------- Logging --------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wingman-bot")

# -------------------- Env Vars --------------------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
assert TOKEN, "Missing TELEGRAM_TOKEN env var"

CHAT_ID_ENV = os.environ.get("CHAT_ID")  # string or None; validated at use-time
ALERT_SECRET = os.environ.get("ALERT_SECRET")
assert ALERT_SECRET, "Missing ALERT_SECRET env var"

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# -------------------- Flask App --------------------
app = Flask(__name__)

# -------------------- Telegram App --------------------
tg_app = Application.builder().token(TOKEN).build()

# Flag to ensure PTB app is started once
_initialized = False
def ensure_initialized_once():
    """Initialize & start PTB v21 Application exactly once."""
    global _initialized
    if not _initialized:
        logger.info("Initializing Telegram application…")
        asyncio.run(tg_app.initialize())
        asyncio.run(tg_app.start())
        _initialized = True
        logger.info("Telegram application initialized & started.")

# -------------------- OpenAI Client --------------------
# Uses OPENAI_API_KEY from env; will raise if missing when called
client = OpenAI()

# -------------------- Helpers --------------------
def _to_float_or_none(v):
    """Tolerant numeric parser: accepts numbers/strings, handles na/nan/commas."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            return float(v)
        except:
            return None
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"na", "nan", ""}:
            return None
        s = re.sub(r"[,\s]", "", s)
        try:
            return float(s)
        except:
            return None
    return None

def _norm_tf(x):
    s = str(x or "").upper().strip()
    return (s.replace("MIN","M")
             .replace("MINS","M")
             .replace("MINUTES","M")
             .replace("HOUR","H")
             .replace("HOURS","H")
             .replace("4HOURS","4H")
             .replace("1HOUR","1H")
             or "240")

def _chat_id_or_raise():
    chat_id = os.environ.get("CHAT_ID")
    assert chat_id, "CHAT_ID is not set in environment"
    return int(chat_id)

# -------------------- Command Handlers --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "✅ Wingman bot is alive.\n"
            "Commands:\n"
            "• /ping — quick test\n"
            "• /chatid — show this chat id\n"
            "• /analyze [SYMBOL] [TF] — analyze latest TA (e.g., /analyze BINANCE:PYTHUSDT 240)\n"
            "Tip: keep TradingView alerts running so I always have fresh TA cached."
        )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("🏓 Pong!")

async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(f"Your chat_id is: {update.message.chat_id}")

# Cache of latest TA by symbol & timeframe; and most recent payload overall
LATEST = defaultdict(dict)   # LATEST["BINANCE:PYTHUSDT"]["240"] = {payload}
LAST_PAYLOAD = None

async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Analyze the latest cached TA using OpenAI and return an actionable plan."""
    try:
        args = context.args
        symbol = tf = None

        if len(args) >= 1:
            symbol = args[0].upper()
        if len(args) >= 2:
            tf = _norm_tf(args[1])

        # Choose which payload to analyze
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
                "Usage: /analyze <SYMBOL> [TF]\n"
                "Example: /analyze BINANCE:PYTHUSDT 240\n"
                "Tip: create a TradingView alert for that symbol/timeframe and wait for the next bar close."
            )
            return

        # Extract values safely
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

        # Build the analysis prompt
        system_msg = "You are Wingman, a precise, risk-aware crypto TA analyst. Be concise and actionable."
        user_msg = f"""
Analyze the following technicals and provide a concise, high-conviction plan.

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

Return:
- Market structure & trend (bullish/bearish/range) + confidence (0–100%).
- Entry plan: immediate vs. pullback; give exact levels.
- Invalidation/stop-loss based on structure or ATR.
- TP ladder: 3–5 targets with reasoning.
- Risk notes: key risks and what invalidates the idea quickly.
Keep it tight with bullets; no fluff.
"""

        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg}
            ],
            temperature=0.2,
        )
        text = resp.choices[0].message.content.strip()
        await update.message.reply_text(text)
    except Exception as e:
        logger.exception("Error in /analyze")
        await update.message.reply_text(f"Error in /analyze: {e}")

# Register command handlers
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CommandHandler("ping", ping))
tg_app.add_handler(CommandHandler("chatid", chatid))
tg_app.add_handler(CommandHandler("analyze", analyze_cmd))

# -------------------- Routes --------------------
@app.get("/health")
def health():
    return "ok", 200

@app.post("/webhook")
def telegram_webhook():
    """Telegram will POST updates here (webhook)."""
    try:
        ensure_initialized_once()
        data = request.get_json(force=True, silent=True)
        if not data:
            return "no json", 400
        update = Update.de_json(data, tg_app.bot)
        asyncio.run(tg_app.process_update(update))
        return "ok", 200
    except Exception as e:
        logger.exception("Error handling /webhook")
        return f"error: {e}", 500

@app.get("/tv/test")
def tv_test():
    """Quick GET to verify CHAT_ID + bot send."""
    try:
        ensure_initialized_once()
        chat_id = _chat_id_or_raise()
        asyncio.run(tg_app.bot.send_message(chat_id=chat_id, text="✅ /tv/test reached OK"))
        return "ok", 200
    except Exception as e:
        logger.exception("Error in /tv/test")
        return f"error: {e}", 500

@app.post("/tv")
def tv_webhook():
    """
    TradingView Webhook endpoint.
    Expects a JSON body containing:
      - secret (must match ALERT_SECRET)
      - symbol, timeframe, price, rsi, ema20/ema50 or ema_fast/ema_slow, macd, macd_signal, macd_hist, atr, note
    """
    try:
        ensure_initialized_once()

        # Log partial body for debugging (safe)
        raw = request.get_data(as_text=True) or ""
        logger.info(f"/tv raw (trunc): {raw[:500]}")

        # Accept JSON only
        try:
            payload = json.loads(raw)
        except Exception as e:
            logger.exception("JSON parse error on /tv")
            return f"bad json: {e}", 400

        # Secret check
        if payload.get("secret") != ALERT_SECRET:
            logger.warning("Unauthorized /tv attempt (secret mismatch)")
            return "unauthorized", 401

        # Normalize & cache
        symbol = payload.get("symbol", "UNKNOWN")
        tf     = str(payload.get("timeframe", "NA")).upper()

        # Save to cache
        LATEST[symbol][tf] = payload
        global LAST_PAYLOAD
        LAST_PAYLOAD = payload

        # Prepare a concise confirmation to Telegram
        price  = _to_float_or_none(payload.get("price"))
        rsi    = _to_float_or_none(payload.get("rsi"))
        ema20  = _to_float_or_none(payload.get("ema20") or payload.get("ema_fast"))
        ema50  = _to_float_or_none(payload.get("ema50") or payload.get("ema_slow"))
        macd   = _to_float_or_none(payload.get("macd"))
        macds  = _to_float_or_none(payload.get("macd_signal"))
        macdh  = _to_float_or_none(payload.get("macd_hist"))
        atr    = _to_float_or_none(payload.get("atr"))
        note   = payload.get("note", "")

        msg = (
            "📡 TV Alert received\n"
            f"• {symbol} ({tf})\n"
            f"• Price: {price}\n"
            f"• RSI: {rsi} | EMA20: {ema20} | EMA50: {ema50}\n"
            f"• MACD: {macd}/{macds}/{macdh} | ATR: {atr}\n"
            f"{'• Note: ' + note if note else ''}\n\n"
            f"Tip: /analyze {symbol} {tf}"
        )

        chat_id = _chat_id_or_raise()
        asyncio.run(tg_app.bot.send_message(chat_id=chat_id, text=msg))
        return "ok", 200

    except Exception as e:
        logger.exception("Error in /tv")
        return f"error: {e}", 500

# -------------------- Entrypoint --------------------
if __name__ == "__main__":
    ensure_initialized_once()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
