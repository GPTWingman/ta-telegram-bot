# server.py — TradingView -> Telegram with venue 24h volume
import os, json, re, logging
import requests
from flask import Flask, request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wingman-volfix")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID        = os.environ.get("CHAT_ID")
ALERT_SECRET   = os.environ.get("ALERT_SECRET")

HTTP_TIMEOUT = 7

app = Flask(__name__)

# ----------------- helpers -----------------
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": int(CHAT_ID), "text": text}
    try:
        r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if not r.ok:
            logger.error("Telegram send failed: %s %s", r.status_code, r.text)
    except Exception as e:
        logger.exception("Telegram send exception: %s", e)

def _clean_num(v, decimals=6, allow_dash=True):
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
        return s

def _get(p, *keys):
    for k in keys:
        if k in p and p[k] is not None:
            return p[k]
    return None

def parse_tv_symbol(tv_symbol: str):
    """
    TradingView symbol format examples:
      'BINANCE:BTCUSDT', 'HTX:PYTHUSDT', 'BYBIT:SOLUSDT.P', 'OKX:BTC-USDT'
    We try to normalize venue and pair for APIs.
    Returns: venue(str|None), base(str|None), quote(str|None), raw_pair(str)
    """
    if not tv_symbol:
        return None, None, None, ""
    parts = tv_symbol.split(":")
    venue = parts[0].upper() if len(parts) >= 2 else None
    pair_raw = parts[-1]

    # Normalize common separators/suffixes
    pair_norm = pair_raw.replace("-", "").replace("/", "")
    # Strip common derivatives suffixes like .P
    pair_norm = pair_norm.replace(".P", "").replace("_PERP", "").replace("-PERP", "")

    # Guess base/quote by common quotes
    quotes = ["USDT", "USD", "USDC", "BTC", "ETH", "FDUSD", "BUSD", "EUR", "TRY"]
    base, quote = None, None
    for q in quotes:
        if pair_norm.endswith(q):
            base = pair_norm[:-len(q)]
            quote = q
            break
    return venue, base, quote, pair_norm

# --------------- venue volume fetchers ---------------
def fetch_binance_24h_quote_volume(symbol_pair: str):
    """
    symbol_pair: e.g., 'BTCUSDT'
    Returns (quote_volume_float or None)
    """
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr"
        r = requests.get(url, params={"symbol": symbol_pair.upper()}, timeout=HTTP_TIMEOUT)
        if r.ok:
            data = r.json()
            # Binance returns 'quoteVolume' (string) for spot
            qv = data.get("quoteVolume")
            return float(qv) if qv is not None else None
        logger.error("Binance 24hr failed %s %s", r.status_code, r.text)
    except Exception as e:
        logger.exception("Binance 24hr exception: %s", e)
    return None

def fetch_htx_24h_quote_and_base(symbol_pair_lower: str):
    """
    HTX/Huobi spot:
      GET /market/detail/merged?symbol=btcusdt
      tick.vol   -> quote volume (24h rolling)
      tick.amount-> base  volume (24h rolling)
    Returns (quote_volume_float or None, base_volume_float or None)
    """
    try:
        url = "https://api.huobi.pro/market/detail/merged"
        r = requests.get(url, params={"symbol": symbol_pair_lower}, timeout=HTTP_TIMEOUT)
        if r.ok:
            data = r.json()
            tick = data.get("tick") or {}
            vol_q = tick.get("vol")      # quote
            vol_b = tick.get("amount")   # base
            q = float(vol_q) if vol_q is not None else None
            b = float(vol_b) if vol_b is not None else None
            return q, b
        logger.error("HTX 24h failed %s %s", r.status_code, r.text)
    except Exception as e:
        logger.exception("HTX 24hr exception: %s", e)
    return None, None

def best_24h_volume(symbol_tv: str, payload: dict):
    """
    Try venue official 24h quote volume first, then TV 'vol24h_quote_tv', then TV 'volume'.
    Returns (value_float_or_None, source_label_str).
    """
    venue, base, quote, pair_norm = parse_tv_symbol(symbol_tv)

    # 1) Venue APIs
    if venue == "BINANCE" and base and quote:
        v = fetch_binance_24h_quote_volume(base + quote)
        if v is not None:
            return v, "exch(BINANCE, quote)"
    if venue in {"HTX", "HUOBI"} and base and quote:
        vq, vb = fetch_htx_24h_quote_and_base((base + quote).lower())
        if vq is not None:
            return vq, "exch(HTX, quote)"
        if vb is not None:
            # As a fallback we could convert base->quote by last price if desired.
            return vb, "exch(HTX, base)"

    # 2) TradingView “Vol(24h TV close)” from Pine (quote)
    tv_quote = _get(payload, "vol24h_quote_tv")
    if tv_quote is not None:
        try:
            return float(tv_quote), "tv(close, quote)"
        except:
            pass

    # 3) Whatever 'volume' the Pine sent (could be TF bar or D bar)
    v_any = _get(payload, "volume")
    try:
        return float(v_any), "tv(volume)"
    except:
        return None, "na"

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

# ----------------- routes -----------------
@app.get("/health")
def health():
    return "ok", 200

@app.get("/tv/test")
def tv_test():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return "Missing TELEGRAM_TOKEN or CHAT_ID", 500
    send_telegram("✅ Test OK\nService is alive.")
    return "ok", 200

@app.post("/tv")
def tv_webhook():
    try:
        raw = request.get_data(as_text=True) or ""
        logger.info("Incoming /tv: %s", raw[:800])

        try:
            payload = json.loads(raw)
        except Exception as e:
            return f"bad json: {e}", 400

        if not ALERT_SECRET or payload.get("secret") != ALERT_SECRET:
            return "unauthorized", 401

        symbol = _get(payload, "symbol") or "UNKNOWN"
        tf     = str(_get(payload, "signal_tf") or _get(payload, "timeframe") or "NA").upper()

        price  = _get(payload, "price")
        chg24  = _get(payload, "change_24h")

        rsi    = _get(payload, "rsi")
        ema20  = _get(payload, "ema20");  ema50  = _get(payload, "ema50")
        ema100 = _get(payload, "ema100"); ema200 = _get(payload, "ema200")
        sma200 = _get(payload, "sma200")

        macd   = _get(payload, "macd");   macds  = _get(payload, "macd_signal"); macdh = _get(payload, "macd_hist")
        adx    = _get(payload, "adx");    diplus = _get(payload, "di_plus");      dimin = _get(payload, "di_minus")

        bbu    = _get(payload, "bb_upper"); bbl = _get(payload, "bb_lower"); bbw = _get(payload, "bb_width")
        atr    = _get(payload, "atr");      obv = _get(payload, "obv")

        swh    = _get(payload, "swing_high");  swl = _get(payload, "swing_low")
        hiDate = _get(payload, "swing_high_date"); loDate = _get(payload, "swing_low_date")

        vol24_base_tv  = _get(payload, "vol24h_base_tv")
        vol24_quote_tv = _get(payload, "vol24h_quote_tv")

        note   = str(_get(payload, "note") or "")

        # New: venue 24h volume (quote) if available
        vol24_exch, vol_src = best_24h_volume(symbol, payload)

        # Build message
        lines = []
        lines.append("📡 TV Alert")
        lines.append(f"• Symbol: {symbol}  (Signal TF: {tf})")
        vol_line = f"• Price: {_clean_num(price, 6)}  | 24h: {_clean_num(chg24, 2)}%"

        if vol24_exch is not None:
            vol_line += f"  | Vol(24h exch): {_clean_num(vol24_exch, 0)}"
        elif vol24_quote_tv is not None or vol24_base_tv is not None:
            vol_line += f"  | Vol(24h TV close): {_clean_num(vol24_quote_tv or vol24_base_tv, 0)}"
        lines.append(vol_line + f"  [{vol_src}]")

        lines.append(f"• RSI(14): {_clean_num(rsi, 2)}  | ATR: {_clean_num(atr, 6)}")
        lines.append(f"• EMA20/50: {_clean_num(ema20,6)} / {_clean_num(ema50,6)}")
        lines.append(f"• EMA100/200: {_clean_num(ema100,6)} / {_clean_num(ema200,6)}  | SMA200: {_clean_num(sma200,6)}")
        lines.append(f"• MACD: {_clean_num(macd,6)}  Sig: {_clean_num(macds,6)}  Hist: {_clean_num(macdh,6)}")
        lines.append(f"• ADX/DI+/DI-: {_clean_num(adx,2)} / {_clean_num(diplus,2)} / {_clean_num(dimin,2)}  ({trend_read(adx,diplus,dimin)})")
        lines.append(f"• BB U/L: {_clean_num(bbu,6)} / {_clean_num(bbl,6)}  | Width: {_clean_num(bbw,6)}")
        lines.append(f"• Swing H/L: {_clean_num(swh,6)} / {_clean_num(swl,6)}")
                     #+ (f"  | Dates: {hiDate or '—'} / {loDate or '—'}" if (hiDate or loDate) else ""))

        if note:
            lines.append(f"• Note: {note}")

        msg = "\n".join(lines)

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
