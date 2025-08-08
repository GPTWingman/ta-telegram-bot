# server.py
import os, json, re, logging, threading, asyncio
from collections import defaultdict

import requests
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from openai import OpenAI

# -------------------- Logging --------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wingman-bot")

# -------------------- Env Vars --------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
assert TELEGRAM_TOKEN, "Missing TELEGRAM_TOKEN env var"

ALERT_SECRET = os.environ.get("ALERT_SECRET")
assert ALERT_SECRET, "Missing ALERT_SECRET env var"

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# -------------------- Flask App --------------------
app = Flask(__name__)

# -------------------- Dedicated asyncio loop (for PTB) --------------------
LOOP = asyncio.new_event_loop()
def _loop_runner():
    asyncio.set_event_loop(LOOP)
    LOOP.run_forever()
threading.Thread(target=_loop_runner, daemon=True).start()

# -------------------- Telegram App (PTB v21) --------------------
tg_app = Application.builder().token(TELEGRAM_TOKEN).build()

_initialized = False
def ensure_initialized_once():
    """Initialize & start PTB on the dedicated loop exactly once."""
    global _initialized
    if not _initialized:
        logger.info("Initializing Telegram application on background loop…")
        fut = asyncio.run_coroutine_threadsafe(tg_app.initialize(), LOOP); fut.result()
        fut = asyncio.run_coroutine_threadsafe(tg_app.start(), LOOP);       fut.result()
        _initialized = True
        logger.info("Telegram application initialized & started.")

def _chat_id_or_raise() -> int:
    chat_id = os.environ.get("CHAT_ID")
    assert chat_id, "CHAT_ID is not set in environment"
    return int(chat_id)

# -------------------- OpenAI Client --------------------
client = OpenAI()  # uses OPENAI_API_KEY from env

# -------------------- Helpers --------------------
def _to_float_or_none(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try: return float(v)
        except: return None
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"na", "nan", ""}: return None
        s = re.sub(r"[,\s]", "", s)
        try: return float(s)
        except: return None
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

def send_telegram_text_http(chat_id: int, text: str):
    """Send Telegram message via HTTP API (sidesteps async loop issues for /tv)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": text})
        if not resp.ok:
            logger.error("Telegram HTTP send failed: %s %s", resp.status_code, resp.text)
    except Exception as e:
        logger.exception("Telegram HTTP send exception: %s", e)

# -------------------- Caches --------------------
LATEST = defaultdict(dict)   # LATEST["BINANCE:PYTHUSDT"]["240"] = payload dict
LAST_PAYLOAD = None

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

async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Analyze the latest cached TA using OpenAI and return an actionable plan."""
    try:
        args = context.args
        symbol = tf = None
        if len(args) >= 1: symbol = args[0].upper()
        if len(args) >= 2: tf = _norm_tf(args[1])

        # Select payload
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

        # Extract values
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
- Entry plan: immediate v
