# server.py — Simple & stable TradingView -> Telegram forwarder
# No async. No event loops. Just works.

import os, json, re, logging
import requests
from flask import Flask, request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wingman-simple")

# ===== Env vars (set these on Render) =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")   # from BotFather
CHAT_ID        = os.environ.get("CHAT_ID")          # your chat id number
ALERT_SECRET   = os.environ.get("ALERT_SECRET")     # must match TradingView alert "secret"

app = Flask(__name__)

# ----- Basic routes -----
@app.get("/health")
def health():
    return "ok", 200

@app.get("/tv/test")
def tv_test():
    """Send a simple test message to Telegram (good for sanity checks)."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return "Missing TELEGRAM_TOKEN or CHAT_ID", 500
    send_telegram(f"✅ Test OK\nService is alive.")
    return "ok", 200

# ----- Helper functions -----
def send_telegram(text: str):
    """Send a message to Telegram using the Bot API (plain HTTP)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": int(CHAT_ID), "text": text}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok:
            logger.error("Telegram send failed: %s %s", r.status_code, r.text)
    except Exception as e:
        logger.exception("Telegram send exception: %s", e)

def _clean_num(v, decimals=6, allow_dash=True):
    """Turn strings like 'na', '', ' 1,234.5 ' into nice numbers; or '—'."""
    if v is None:
        return "—" if allow_dash else ""
    if isinstance(v, (int, float)):
        try:
            return f"{float(v):.{decimals}f}"
        except:
            return "—" if allow_dash else ""
    s = str(v).strip().lower()
    if s in {"na", "nan", ""}:
        return "—" if allow_dash else ""
    s = re.sub(r"[,\s]", "", s)
    try:
        return f"{float(s):.{decimals}f}"
    except:
        return s  # last resort: show raw

def _get(p, *keys):
    for k in keys:
        if k in p and p[k] is not None:
            return p[k]
    return None

# ----- TradingView webhook -----
@app.post("/tv")
def tv_webhook():
    """
    Accepts a JSON body from TradingView.
    We compare 'secret', format all TA fields, and push to Telegram.
    """
    try:
        raw = request.get_data(as_text=True) or ""
        logger.info("Incoming /tv: %s", raw[:500])

        # Parse JSON (return 400 if invalid)
        try:
            payload = json.loads(raw)
        except Exception as e:
            return f"bad json: {e}", 400

        # Secret check (return 401 if wrong)
        if not ALERT_SECRET or payload.get("secret") != ALERT_SECRET:
            return "unauthorized", 401

        # Extract fields (everything is optional; we format what we have)
        symbol = _get(payload, "symbol") or "UNKNOWN"
        tf     = str(_get(payload, "timeframe") or "NA").upper()

        price  = _get(payload, "price")
        vol    = _get(payload, "volume")

        rsi    = _get(payload, "rsi")

        ema20  = _get(payload, "ema20", "ema_fast")
        ema50  = _get(payload, "ema50", "ema_slow")
        ema100 = _get(payload, "ema100")
        ema200 = _get(payload, "ema200")
        sma200 = _get(payload, "sma200")

        macd   = _get(payload, "macd")
        macds  = _get(payload, "macd_signal")
        macdh  = _get(payload, "macd_hist")

        adx    = _get(payload, "adx")
        diplus = _get(payload, "di_plus")
        dimin  = _get(payload, "di_minus")

        bbu    = _get(payload, "bb_upper")
        bbl    = _get(payload, "bb_lower")
        bbw    = _get(payload, "bb_width")

        atr    = _get(payload, "atr")
        obv    = _get(payload, "obv")

        swh    = _get(payload, "swing_high")
        swl    = _get(payload, "swing_low")

        note   = str(_get(payload, "note") or "")

        # Quick trend read from ADX/DI (nice to have)
        def trend_read(adx_val, di_p, di_m):
            try:
                adx_f = float(adx_val); dip = float(di_p); dim = float(di_m)
            except:
                return "—"
            if adx_f >= 25:
                return "Strong Bull" if dip > dim else "Strong Bear"
            if adx_f >= 18:
                return "Mild Bull" if dip > dim else "Mild Bear"
            return "Range/Weak"

        # Build Telegram message (compact, all the goodies)
        msg = (
            "📡 TV Alert\n"
            f"• Symbol: {symbol}  ({tf})\n"
            f"• Price: {_clean_num(price, 6)}  | Vol: {_clean_num(vol, 0)}\n"
            f"• RSI(14): {_clean_num(rsi, 2)}  | ATR: {_clean_num(atr, 6)}\n"
            f"• EMA20/50: {_clean_num(ema20,6)} / {_clean_num(ema50,6)}\n"
            f"• EMA100/200: {_clean_num(ema100,6)} / {_clean_num(ema200,6)}  | SMA200: {_clean_num(sma200,6)}\n"
            f"• MACD: {_clean_num(macd,6)}  Sig: {_clean_num(macds,6)}  Hist: {_clean_num(macdh,6)}\n"
            f"• ADX/DI+/DI-: {_clean_num(adx,2)} / {_clean_num(diplus,2)} / {_clean_num(dimin,2)}  ({trend_read(adx,diplus,dimin)})\n"
            f"• BB U/L: {_clean_num(bbu,6)} / {_clean_num(bbl,6)}  | Width: {_clean_num(bbw,6)}\n"
            f"• Swing H/L: {_clean_num(swh,6)} / {_clean_num(swl,6)}\n"
            f"{'• Note: ' + note if note else ''}"
        )

        # Send to Telegram (plain HTTP)
        if not TELEGRAM_TOKEN or not CHAT_ID:
            logger.error("Missing TELEGRAM_TOKEN or CHAT_ID")
            return "server misconfigured", 500

        send_telegram(msg)
        return "ok", 200

    except Exception as e:
        logger.exception("Error in /tv")
        return f"error: {e}", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
