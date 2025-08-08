# server.py
import os, json, re, logging, threading, asyncio
from collections import defaultdict

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

# -------------------- Dedicated asyncio loop (fixes 'Event loop is closed') --------------------
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
        logging.info("Initializing Telegram application on background loop…")
        fut = asyncio.run_coroutine_threadsafe(tg_app.initialize(), LOOP)
        fut.result()
        fut = asyncio.run_coroutine_threadsafe(tg_app.start(), LOOP)
        fut.result()
        _initialized = True
        logging.info("Telegram application initialized & started.")

def _chat_id_or_raise() -> int:
    chat_id = os.environ.get("CHAT_ID")
    assert chat_id, "CHAT_ID is not set in environment"
    return int(chat_id)

# -------------------- OpenAI Client --------------------
# Uses OPENAI_API_KEY from environment
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

# -------------------- Caches --------------------
# Latest TA by symbol & timeframe; and most recent payload overall
LATEST = defaultdict(dict)   # LATEST["BINANCE:PYTHUSDT"]["240"] = payload dict
LAST_PAYLOAD = None

# -------------------- Command Handlers --------------------
async def start(update: Update, context: ContextTypes.DEFAUL
