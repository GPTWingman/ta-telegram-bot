# server.py — Simple & stable TradingView -> Telegram forwarder
# Sync-only. No async/event loops.

import os, json, re, time, logging
import requests
from flask import Flask, request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wingman-simple")

# ===== Env vars (set these on Render) =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")    # from BotFather
CHAT_ID        = os.environ.get("CHAT_ID")           # your chat id number
ALERT_SECRET   = os.environ.get("ALERT_SECRET")      # must match TradingView alert "secret"

# External 24h volume source
VOLUME_SOURCE  = os.environ.get("VOLUME_SOURCE", "coingecko").lower()  # "coingecko" or "cmc"
CMC_API_KEY    = os.environ.get("CMC_API_KEY", "")

# Simple caches to avoid rate limits
_VOL_CACHE: dict = {}   # key -> (value, units, ts)
_ID_CACHE:  dict = {}   # base_symbol -> coingecko_id
_CACHE_TTL  = int(os.environ.get("CACHE_TTL", "300"))  # seconds

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
    send_telegram("✅ Test OK\nService is alive.")
    return "ok", 200

# ----- Telegram helper -----
def send_telegram(text: str):
    """Send a message to Telegram using the Bot API (plain HTTP)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": int(CHAT_ID), "text": text}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.ok:
            logger.info("Telegram OK: %s", r.text[:200])
        else:
            logger.error("Telegram send failed: %s %s", r.status_code, r.text[:500])
    except Exception as e:
        logger.exception("Telegram send exception: %s", e)

# ----- Utilities -----
def _clean_num(v, decimals=6, allow_dash=True):
    """Format numbers safely (handles 'na', None, strings)."""
    if v is None:
        return "—" if allow_dash else ""
    if isinstance(v, (int, float)):
        try:
            return f"{float(v):.{decimals}f}"
        except Exception:
            return "—" if allow_dash else ""
    s = str(v).strip().lower()
    if s in {"na", "nan", ""}:
        return "—" if allow_dash else ""
    s = re.sub(r"[,\s]", "", s)
    try:
        return f"{float(s):.{decimals}f}"
    except Exception:
        return s  # last resort: show raw

def _abbr(v):
    """Abbreviate big numbers: 1_234 -> 1.23K, 1_234_567 -> 1.23M. '—' if NA."""
    try:
        n = float(v)
    except Exception:
        return "—"
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000_000_000:
        return f"{sign}{n/1_000_000_000_000:.2f}T"
    if n >= 1_000_000_000:
        return f"{sign}{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{sign}{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{sign}{n/1_000:.2f}K"
    return f"{sign}{n:.0f}"

def _get(p, *keys):
    for k in keys:
        if k in p and p[k] is not None:
            return p[k]
    return None

# ---- Symbol parsing & external data helpers -------------------------------
def parse_base_from_tv_symbol(tv_symbol: str) -> str:
    """
    Extract base asset from TV symbol like 'BINANCE:BTCUSDT', 'HTX:PYTHUSDT', 'SOLUSD.P'
    """
    if not tv_symbol:
        return ""
    s = tv_symbol.upper().split(":")[-1]  # drop venue prefix
    s = s.replace(".P", "").replace("_PERP", "").replace("-PERP", "")
    QUOTES = ["USDT", "USD", "USDC", "FDUSD", "BUSD", "TUSD", "EUR", "AUD", "BTC", "ETH", "JPY", "KRW"]
    for q in QUOTES:
        if s.endswith(q) and len(s) > len(q):
            return s[:-len(q)]
    m = re.match(r"([A-Z]+)", s)
    return m.group(1) if m else s

def cg_resolve_id(base_symbol: str):
    """Resolve a symbol like 'BTC' -> 'bitcoin' using CoinGecko /search (cached)."""
    if base_symbol in _ID_CACHE:
        return _ID_CACHE[base_symbol]
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/search",
            params={"query": base_symbol},
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("coins", [])
        # Prefer exact symbol match, else best ranked
        candidates = [c for c in items if c.get("symbol", "").lower() == base_symbol.lower()]
        if not candidates:
            candidates = items
        if not candidates:
            return None
        best = sorted(candidates, key=lambda c: (c.get("market_cap_rank") or 1e9))[0]
        coin_id = best.get("id")
        if coin_id:
            _ID_CACHE[base_symbol] = coin_id
            return coin_id
    except Exception as e:
        logger.warning("cg_resolve_id error for %s: %s", base_symbol, e)
    return None

def get_coingecko_volume_24h_by_symbol(tv_symbol: str):
    base = parse_base_from_tv_symbol(tv_symbol)
    if not base:
        return None, None
    key = ("cg", base)
    now = time.time()
    if key in _VOL_CACHE and now - _VOL_CACHE[key][2] < _CACHE_TTL:
        v, u, _ = _VOL_CACHE[key]
        return v, u
    coin_id = cg_resolve_id(base)
    if not coin_id:
        return None, None
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "ids": coin_id, "sparkline": "false"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return None, None
        vol_usd = data[0].get("total_volume")
        _VOL_CACHE[key] = (vol_usd, "USD", now)
        return vol_usd, "USD"
    except Exception as e:
        logger.warning("get_coingecko_volume_24h_by_symbol error: %s", e)
        return None, None

def get_cmc_volume_24h_by_symbol(tv_symbol: str):
    if not CMC_API_KEY:
        return None, None
    base = parse_base_from_tv_symbol(tv_symbol)
    if not base:
        return None, None
    key = ("cmc", base)
    now = time.time()
    if key in _VOL_CACHE and now - _VOL_CACHE[key][2] < _CACHE_TTL:
        v, u, _ = _VOL_CACHE[key]
        return v, u
    try:
        r = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
            params={"symbol": base, "convert": "USD"},
            headers={"X-CMC_PRO_API_KEY": CMC_API_KEY},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json().get("data", {}).get(base)
        if not data:
            return None, None
        quote = data.get("quote", {}).get("USD", {})
        vol_usd = quote.get("volume_24h")
        _VOL_CACHE[key] = (vol_usd, "USD", now)
        return vol_usd, "USD"
    except Exception as e:
        logger.warning("get_cmc_volume_24h_by_symbol error: %s", e)
        return None, None

def get_external_volume_24h(tv_symbol: str):
    """Try preferred source, then fallback."""
    if VOLUME_SOURCE == "cmc" and CMC_API_KEY:
        v, u = get_cmc_volume_24h_by_symbol(tv_symbol)
        if v is not None:
            return v, u
        return get_coingecko_volume_24h_by_symbol(tv_symbol)
    else:
        v, u = get_coingecko_volume_24h_by_symbol(tv_symbol)
        if v is not None:
            return v, u
        if CMC_API_KEY:
            return get_cmc_volume_24h_by_symbol(tv_symbol)
        return None, None

def get_global_dominance():
    """Fallback for BTC & Alt dominance from CoinGecko /global."""
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
        r.raise_for_status()
        data = r.json().get("data", {})
        m = data.get("market_cap_percentage", {})
        btc = m.get("btc")
        alt = 100 - btc if isinstance(btc, (int, float)) else None
        return btc, alt
    except Exception as e:
        logger.warning("get_global_dominance error: %s", e)
        return None, None

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
            logger.warning("unauthorized /tv attempt")
            return "unauthorized", 401

        # Extract fields (everything is optional; we format what we have)
        symbol    = _get(payload, "symbol") or "UNKNOWN"   # Pine sends syminfo.tickerid
        signal_tf = _get(payload, "signal_tf")

        price  = _get(payload, "price")
        chg24  = _get(payload, "change_24h")

        # Local/chart volume sent by Pine (for fallback or comparison)
        vol_local = _get(payload, "volume")
        vol_mode  = _get(payload, "vol_mode")

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

        swh    = _get(payload, "swing_high", "swing_high_last")
        swl    = _get(payload, "swing_low",  "swing_low_last")

        # Dominance (prefer values from Pine, else fallback to CG global)
        btc_dom = _get(payload, "btc_dom")
        alt_dom = _get(payload, "alt_dom")
        if btc_dom is None or alt_dom is None:
            cg_btc, cg_alt = get_global_dominance()
            if btc_dom is None: btc_dom = cg_btc
            if alt_dom is None: alt_dom = cg_alt

        note   = str(_get(payload, "note") or "")

        # ---- External 24h volume (USD) ----
        ext_vol, ext_units = get_external_volume_24h(symbol)

        # Quick trend read from ADX/DI (nice to have)
        def trend_read(adx_val, di_p, di_m):
            try:
                adx_f = float(adx_val); dip = float(di_p); dim = float(di_m)
            except Exception:
                return "—"
            if adx_f >= 25:
                return "Strong Bull" if dip > dim else "Strong Bear"
            if adx_f >= 18:
                return "Mild Bull" if dip > dim else "Mild Bear"
            return "Range/Weak"

        # Prefer external 24h volume; fallback to Pine's local choice
        if ext_vol is not None:
            vol_line = f"Vol(24h ext): {_abbr(ext_vol)} {ext_units}"
        else:
            label = f"Vol({vol_mode or 'TF'})"
            vol_line = f"{label}: {_clean_num(vol_local, 0)}"

        # Build Telegram message
        msg = (
            "📡 TV Alert\n"
            f"• Symbol: {symbol}  (Signal TF: {signal_tf})\n"
            f"• Price: {_clean_num(price, 6)}  | 24h: {_clean_num(chg24, 2)}%  | {vol_line}\n"
            f"• BTC Dom: {_clean_num(btc_dom, 2)}%  |  Alt Dom(ex-BTC): {_clean_num(alt_dom, 2)}%\n"
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

        logger.info("About to send Telegram for %s (TF %s)", symbol, signal_tf)
        send_telegram(msg)
        return "ok", 200

    except Exception as e:
        logger.exception("Error in /tv")
        return f"error: {e}", 500


if __name__ == "__main__":
    # Render sets PORT env var. Defaults to 8000 locally.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
