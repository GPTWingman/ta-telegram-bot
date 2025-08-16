# server.py — Simple & stable TradingView -> Telegram forwarder
# Features: robust JSON parsing, tick-accurate formatting, external 24h volume (CG/CMC),
# dominance fallback, safe Telegram chunking/logging.

import os
import json
import re
import time
import logging
from decimal import Decimal, ROUND_HALF_UP

import requests
from flask import Flask, request

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wingman-simple")

# ---------- Env vars ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")   # BotFather token
CHAT_ID        = os.environ.get("CHAT_ID")          # integer chat id
ALERT_SECRET   = os.environ.get("ALERT_SECRET")     # must match Pine "secret"

# External 24h volume source
VOLUME_SOURCE  = os.environ.get("VOLUME_SOURCE", "coingecko").lower()  # "coingecko" or "cmc"
CMC_API_KEY    = os.environ.get("CMC_API_KEY", "")

# Simple caches
_VOL_CACHE: dict = {}   # key -> (value, units, ts)
_ID_CACHE: dict  = {}   # base_symbol -> coingecko_id
_CACHE_TTL       = int(os.environ.get("CACHE_TTL", "300"))  # seconds

# ---------- Flask ----------
app = Flask(_name_)

# ---------- Utilities ----------
def _clean_num(v, decimals=6, allow_dash=True):
    """Format number/string cleanly; return '—' for NA."""
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
        return s

def _abbr(v):
    """Abbreviate big numbers (K/M/B/T)."""
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

def _pick(fmt_val, raw_val, decimals=6):
    """Prefer a preformatted string from Pine; else format the raw."""
    if isinstance(fmt_val, str) and fmt_val.strip():
        return fmt_val
    return _clean_num(raw_val, decimals)

def _format_price(price_raw, price_fmt, tick_str):
    """Prefer Pine's tick-accurate string; else quantize to tick; else 8dp fallback."""
    if isinstance(price_fmt, str) and price_fmt.strip():
        return price_fmt
    try:
        if tick_str:
            q = Decimal(str(tick_str))
            p = Decimal(str(price_raw))
            return format(p.quantize(q, rounding=ROUND_HALF_UP), 'f')
    except Exception:
        pass
    return _clean_num(price_raw, 8)

# ---------- External 24h volume helpers ----------
def parse_base_from_tv_symbol(tv_symbol: str) -> str:
    """Extract base asset from 'VENUE:BTCUSDT', 'HTX:PYTHUSDT', 'SOLUSD.P', etc."""
    if not tv_symbol:
        return ""
    s = tv_symbol.upper().split(":")[-1]
    s = s.replace(".P", "").replace("_PERP", "").replace("-PERP", "")
    QUOTES = ["USDT","USD","USDC","FDUSD","BUSD","TUSD","EUR","AUD","BTC","ETH","JPY","KRW"]
    for q in QUOTES:
        if s.endswith(q) and len(s) > len(q):
            return s[:-len(q)]
    m = re.match(r"([A-Z]+)", s)
    return m.group(1) if m else s

def cg_resolve_id(base_symbol: str):
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
        candidates = [c for c in items if c.get("symbol","").lower() == base_symbol.lower()]
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

# Optional dominance fallback (if Pine didn't send)
def get_global_dominance():
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

# ---------- Telegram ----------
def send_telegram(text: str):
    """Send a message to Telegram, chunking if >4096 chars and logging verbosely."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.error("Missing TELEGRAM_TOKEN or CHAT_ID")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    MAXLEN = 3900  # keep margin under Telegram 4096 limit
    chunks = [text[i:i + MAXLEN] for i in range(0, len(text), MAXLEN)] or [""]

    all_ok = True
    for idx, chunk in enumerate(chunks, start=1):
        payload = {"chat_id": int(CHAT_ID), "text": chunk}
        try:
            r = requests.post(url, json=payload, timeout=15)
            logger.info("Telegram POST (%d/%d): %s", idx, len(chunks), r.status_code)
            if not r.ok:
                logger.error("Telegram send failed: %s %s", r.status_code, r.text[:1000])
                all_ok = False
            else:
                logger.info("Telegram OK body: %s", r.text[:500])
        except Exception as e:
            logger.exception("Telegram send exception: %s", e)
            all_ok = False
    return all_ok

# ---------- Routes ----------
@app.get("/health")
def health():
    return "ok", 200

@app.get("/tv/test")
def tv_test():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return "Missing TELEGRAM_TOKEN or CHAT_ID", 500
    ok = send_telegram("✅ Test OK\nService is alive.")
    return ("ok", 200) if ok else ("telegram_failed", 502)

@app.post("/tv")
def tv_webhook():
    try:
        raw = request.get_data(as_text=True) or ""
        logger.info("Incoming /tv: %s", raw[:500])

        # Robust JSON parse (auto-fix common issues)
        try:
            payload = json.loads(raw)
        except Exception as e1:
            candidate = raw.strip().lstrip("\ufeff")
            fixed = None
            if "}" in candidate:
                candidate2 = candidate[: candidate.rfind("}") + 1]
                try:
                    fixed = json.loads(candidate2)
                except Exception:
                    fixed = None
            if fixed is not None:
                logger.warning("JSON fixed by trimming after last '}'")
                payload = fixed
            else:
                logger.error("bad json parse: %s\n--- first 300 ---\n%r\n--- last 300 ---\n%r",
                             e1, raw[:300], raw[-300:])
                return "bad json", 400

        # Secret check
        if not ALERT_SECRET or payload.get("secret") != ALERT_SECRET:
            logger.warning("unauthorized /tv attempt")
            return "unauthorized", 401

        # -------- Extract fields (raw + *_fmt + tick) --------
        symbol    = _get(payload, "symbol") or "UNKNOWN"
        signal_tf = _get(payload, "signal_tf")
        chg24     = _get(payload, "change_24h")

        price_raw = _get(payload, "price")
        price_fmt = _get(payload, "price_fmt")
        tick_str  = _get(payload, "tick")

        rsi       = _get(payload, "rsi")

        ema20     = _get(payload, "ema20");      ema20_fmt  = _get(payload, "ema20_fmt")
        ema50     = _get(payload, "ema50");      ema50_fmt  = _get(payload, "ema50_fmt")
        ema100    = _get(payload, "ema100");     ema100_fmt = _get(payload, "ema100_fmt")
        ema200    = _get(payload, "ema200");     ema200_fmt = _get(payload, "ema200_fmt")
        sma200    = _get(payload, "sma200");     sma200_fmt = _get(payload, "sma200_fmt")

        macd      = _get(payload, "macd");       macd_fmt   = _get(payload, "macd_fmt")
        macds     = _get(payload, "macd_signal");macds_fmt  = _get(payload, "macd_signal_fmt")
        macdh     = _get(payload, "macd_hist");  macdh_fmt  = _get(payload, "macd_hist_fmt")

        adx       = _get(payload, "adx")
        diplus    = _get(payload, "di_plus")
        dimin     = _get(payload, "di_minus")

        bbu       = _get(payload, "bb_upper");   bbu_fmt    = _get(payload, "bb_upper_fmt")
        bbl       = _get(payload, "bb_lower");   bbl_fmt    = _get(payload, "bb_lower_fmt")
        bbw       = _get(payload, "bb_width");   bbw_fmt    = _get(payload, "bb_width_fmt")

        atr       = _get(payload, "atr");        atr_fmt    = _get(payload, "atr_fmt")
        obv       = _get(payload, "obv")

        swh       = _get(payload, "swing_high"); swh_fmt    = _get(payload, "swing_high_fmt")
        swl       = _get(payload, "swing_low");  swl_fmt    = _get(payload, "swing_low_fmt")

        btc_dom   = _get(payload, "btc_dom")
        alt_dom   = _get(payload, "alt_dom")
        if btc_dom is None or alt_dom is None:
            # optional fallback to CG global if Pine didn't send
            cg_btc, cg_alt = get_global_dominance()
            if btc_dom is None:
                btc_dom = cg_btc
            if alt_dom is None:
                alt_dom = cg_alt

        # External 24h volume (USD)
        ext_vol, ext_units = get_external_volume_24h(symbol)
        vol_line = f"Vol(24h ext): {_abbr(ext_vol)} {ext_units}" if ext_vol is not None else "Vol(24h ext): —"

        # Trend label
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

        # Price display (tick-aware)
        price_display = _format_price(price_raw, price_fmt, tick_str)

        # -------- Build Telegram message --------
        msg = (
            "📡 TV Alert\n"
            f"• Symbol: {symbol}  (Signal TF: {signal_tf})\n"
            f"• Price: {price_display}  | 24h: {_clean_num(chg24, 2)}%  | {vol_line}\n"
            f"• BTC Dom: {_clean_num(btc_dom, 2)}%  |  Alt Dom(ex-BTC): {_clean_num(alt_dom, 2)}%\n"
            f"• RSI(14): {_clean_num(rsi, 2)}  | ATR: {_pick(atr_fmt, atr, 6)}\n"
            f"• EMA20/50: {_pick(ema20_fmt, ema20, 6)} / {_pick(ema50_fmt, ema50, 6)}\n"
            f"• EMA100/200: {_pick(ema100_fmt, ema100, 6)} / {_pick(ema200_fmt, ema200, 6)}  | SMA200: {_pick(sma200_fmt, sma200, 6)}\n"
            f"• MACD: {_pick(macd_fmt, macd, 6)}  Sig: {_pick(macds_fmt, macds, 6)}  Hist: {_pick(macdh_fmt, macdh, 6)}\n"
            f"• ADX/DI+/DI-: {_clean_num(adx,2)} / {_clean_num(diplus,2)} / {_clean_num(dimin,2)}  ({trend_read(adx,diplus,dimin)})\n"
            f"• BB U/L: {_pick(bbu_fmt, bbu, 6)} / {_pick(bbl_fmt, bbl, 6)}  | Width: {_pick(bbw_fmt, bbw, 6)}\n"
            f"• Swing H/L: {_pick(swh_fmt, swh, 6)} / {_pick(swl_fmt, swl, 6)}\n"
            f"{'• Note: ' + str(_get(payload, 'note')) if _get(payload, 'note') else ''}"
        )

        # -------- Send to Telegram --------
        logger.info("Telegram message preview (len=%d): %s", len(msg), msg[:500])
        ok = send_telegram(msg)
        if not ok:
            return "telegram_failed", 502
        return "ok", 200

    except Exception as e:
        logger.exception("Error in /tv")
        return f"error: {e}", 500

# ---------- Main ----------
if _name_ == "_main_":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
