# server.py — TradingView -> Telegram with 24h Volume preference:
# 1) CoinGecko, 2) Venue (Binance/Coinbase/HTX/Bybit/Bitunix), 3) TV fallbacks.
import os, json, re, time, logging
import requests
from flask import Flask, request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wingman-vol-order")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID        = os.environ.get("CHAT_ID")
ALERT_SECRET   = os.environ.get("ALERT_SECRET")

HTTP_TIMEOUT = 8
CACHE_TTL    = int(os.environ.get("CACHE_TTL", "300"))  # seconds

# Simple caches
_ID_CACHE  = {}  # {base_symbol: coingecko_id}
_VOL_CACHE = {}  # {key_tuple: (value, ts)}

app = Flask(__name__)

# ----------------- Telegram -----------------
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": int(CHAT_ID), "text": text}
    try:
        r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if not r.ok:
            logger.error("Telegram send failed: %s %s", r.status_code, r.text[:500])
    except Exception as e:
        logger.exception("Telegram send exception: %s", e)

# ----------------- Utils -----------------
def _clean_num(v, decimals=6, allow_dash=True):
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
    try:
        n = float(v)
    except Exception:
        return "—"
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000_000_000: return f"{sign}{n/1_000_000_000_000:.2f}T"
    if n >= 1_000_000_000:     return f"{sign}{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:         return f"{sign}{n/1_000_000:.2f}M"
    if n >= 1_000:             return f"{sign}{n/1_000:.2f}K"
    return f"{sign}{n:.0f}"

def _get(p, *keys):
    for k in keys:
        if k in p and p[k] is not None:
            return p[k]
    return None

def parse_tv_symbol(tv_symbol: str):
    """
    TradingView formats: 'BINANCE:BTCUSDT', 'COINBASE:BTCUSD', 'HTX:PYTHUSDT',
                         'BYBIT:SOLUSDT.P', 'BITUNIX:BTCUSDT', etc.
    Returns: venue, base, quote, normalized_pair
    """
    if not tv_symbol:
        return None, None, None, ""
    parts = tv_symbol.split(":")
    venue = parts[0].upper() if len(parts) >= 2 else None
    pair_raw = parts[-1]

    # Normalize: remove dash/slash, strip common derivatives suffixes
    pair_norm = pair_raw.replace("-", "").replace("/", "")
    pair_norm = pair_norm.replace(".P", "").replace("_PERP", "").replace("-PERP", "")

    # Guess base/quote
    QUOTES = ["USDT", "USD", "USDC", "FDUSD", "BUSD", "TUSD", "EUR", "TRY", "BTC", "ETH"]
    base, quote = None, None
    for q in QUOTES:
        if pair_norm.endswith(q) and len(pair_norm) > len(q):
            base = pair_norm[:-len(q)]
            quote = q
            break
    return venue, base, quote, pair_norm

# ----------------- CoinGecko (primary) -----------------
def cg_resolve_id(base_symbol: str):
    if not base_symbol:
        return None
    if base_symbol in _ID_CACHE:
        return _ID_CACHE[base_symbol]
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search",
                         params={"query": base_symbol}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        coins = r.json().get("coins", [])
        # Prefer exact symbol match; fall back to first by market cap rank
        exact = [c for c in coins if c.get("symbol","").lower() == base_symbol.lower()]
        cand = exact or coins
        if not cand:
            return None
        best = sorted(cand, key=lambda c: (c.get("market_cap_rank") or 1e9))[0]
        coin_id = best.get("id")
        if coin_id:
            _ID_CACHE[base_symbol] = coin_id
            return coin_id
    except Exception as e:
        logger.warning("cg_resolve_id error for %s: %s", base_symbol, e)
    return None

def get_coingecko_volume_24h_by_symbol(tv_symbol: str):
    venue, base, quote, _ = parse_tv_symbol(tv_symbol)
    if not base:
        return None
    cache_key = ("cg", base.lower())
    now = time.time()
    if cache_key in _VOL_CACHE and now - _VOL_CACHE[cache_key][1] < CACHE_TTL:
        return _VOL_CACHE[cache_key][0]
    coin_id = cg_resolve_id(base)
    if not coin_id:
        return None
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "ids": coin_id, "sparkline": "false"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        arr = r.json()
        if not arr:
            return None
        vol_usd = arr[0].get("total_volume")
        if isinstance(vol_usd, (int, float)):
            _VOL_CACHE[cache_key] = (vol_usd, now)
            return vol_usd
    except Exception as e:
        logger.warning("get_coingecko_volume_24h_by_symbol error: %s", e)
    return None

# ----------------- Venue APIs (secondary) -----------------
def fetch_binance_24h_quote_volume(pair_no_dash: str):
    # e.g., BTCUSDT
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr",
                         params={"symbol": pair_no_dash.upper()},
                         timeout=HTTP_TIMEOUT)
        if r.ok:
            qv = r.json().get("quoteVolume")
            return float(qv) if qv is not None else None
    except Exception as e:
        logger.warning("Binance 24hr error: %s", e)
    return None

def fetch_htx_24h_quote_volume(pair_lower: str):
    # e.g., btcusdt
    try:
        r = requests.get("https://api.huobi.pro/market/detail/merged",
                         params={"symbol": pair_lower}, timeout=HTTP_TIMEOUT)
        if r.ok:
            tick = (r.json() or {}).get("tick") or {}
            vol_q = tick.get("vol")  # quote volume (24h)
            return float(vol_q) if vol_q is not None else None
    except Exception as e:
        logger.warning("HTX 24hr error: %s", e)
    return None

def fetch_coinbase_24h_quote_volume(base: str, quote: str):
    # Coinbase /stats returns 24h base 'volume'; convert to quote using last price for USD/USDT/USDC pairs.
    product = f"{base}-{quote}"
    try:
        s = requests.get(f"https://api.exchange.coinbase.com/products/{product}/stats",
                         timeout=HTTP_TIMEOUT)
        if not s.ok:
            return None
        stats = s.json()
        vol_base = stats.get("volume")
        last = stats.get("last")
        if vol_base is None or last is None:
            return None
        return float(vol_base) * float(last)
    except Exception as e:
        logger.warning("Coinbase stats error: %s", e)
    return None

def fetch_bybit_24h_quote_volume(pair_no_dash: str):
    # Try spot first, then linear (USDT perp); both return turnover24h in quote currency.
    try:
        r = requests.get("https://api.bybit.com/v5/market/tickers",
                         params={"category": "spot", "symbol": pair_no_dash.upper()},
                         timeout=HTTP_TIMEOUT)
        if r.ok:
            arr = (r.json() or {}).get("result", {}).get("list", [])
            if arr:
                t = arr[0].get("turnover24h")
                return float(t) if t is not None else None
    except Exception as e:
        logger.warning("Bybit spot error: %s", e)
    try:
        r = requests.get("https://api.bybit.com/v5/market/tickers",
                         params={"category": "linear", "symbol": pair_no_dash.upper()},
                         timeout=HTTP_TIMEOUT)
        if r.ok:
            arr = (r.json() or {}).get("result", {}).get("list", [])
            if arr:
                t = arr[0].get("turnover24h")
                return float(t) if t is not None else None
    except Exception as e:
        logger.warning("Bybit linear error: %s", e)
    return None

def fetch_bitunix_24h_quote_volume(pair_no_dash: str):
    # TODO: Bitunix public endpoint not confirmed; return None to fall back cleanly.
    logger.info("Bitunix 24h volume not implemented; skipping.")
    return None

def get_venue_volume_24h(tv_symbol: str):
    venue, base, quote, pair_norm = parse_tv_symbol(tv_symbol)
    if not venue or not base or not quote:
        return None, None
    # Venue-specific
    if venue == "BINANCE":
        v = fetch_binance_24h_quote_volume(base + quote)
        return v, "exch:BINANCE" if v is not None else (None, None)
    if venue in {"HTX", "HUOBI"}:
        v = fetch_htx_24h_quote_volume((base + quote).lower())
        return v, "exch:HTX" if v is not None else (None, None)
    if venue == "COINBASE":
        # Coinbase uses dash product ids
        v = fetch_coinbase_24h_quote_volume(base, quote)
        return v, "exch:COINBASE" if v is not None else (None, None)
    if venue == "BYBIT":
        v = fetch_bybit_24h_quote_volume(base + quote)
        return v, "exch:BYBIT" if v is not None else (None, None)
    if venue == "BITUNIX":
        v = fetch_bitunix_24h_quote_volume(base + quote)
        return v, "exch:BITUNIX" if v is not None else (None, None)
    return None, None

# ----------------- Trend label -----------------
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

# ----------------- Routes -----------------
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

        # --- Extract core fields ---
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

        # TV-provided volume fields (fallbacks)
        vol_tv_close_quote = _get(payload, "vol24h_quote_tv")
        vol_tv_close_base  = _get(payload, "vol24h_base_tv")
        vol_local          = _get(payload, "volume")
        vol_mode           = _get(payload, "vol_mode")

        # --- 24h Volume selection ---
        # 1) CoinGecko (USD)
        vol_cg = get_coingecko_volume_24h_by_symbol(symbol)
        vol_value = None
        vol_src   = None
        if isinstance(vol_cg, (int, float)):
            vol_value = vol_cg
            vol_src   = "cg"

        # 2) Venue (if CoinGecko missing)
        if vol_value is None:
            v_exch, src = get_venue_volume_24h(symbol)
            if v_exch is not None:
                vol_value = v_exch
                vol_src   = src

        # 3) TV fallbacks (close-quote -> close-base -> local)
        if vol_value is None and vol_tv_close_quote is not None:
            try:
                vol_value = float(vol_tv_close_quote)
                vol_src   = "tv:close-quote"
            except: pass
        if vol_value is None and vol_tv_close_base is not None:
            try:
                vol_value = float(vol_tv_close_base)
                vol_src   = "tv:close-base"
            except: pass
        if vol_value is None and vol_local is not None:
            try:
                vol_value = float(vol_local)
                vol_src   = f"tv:{vol_mode or 'volume'}"
            except: pass

        # Extract BTC & Alt dominance
        btc_dom = _get(payload, "btc_dom")
        alt_dom = _get(payload, "alt_dom")
        
        # --- Build Telegram message ---
        lines = []
        lines.append("📡 TV Alert")
        lines.append(f"• Symbol: {symbol}  (Signal TF: {tf})")

        vol_display = f"{_abbr(vol_value)}" if vol_value is not None else "—"
        lines.append(f"• Price: {_clean_num(price, 6)}  | 24h: {_clean_num(chg24, 2)}%  | Vol(24h): {vol_display}  [{vol_src or 'na'}]")
        lines.append(f"• BTC Dom: {_clean_num(btc_dom, 2)}%  |  Alt Dom(ex-BTC): {_clean_num(alt_dom, 2)}%")
        lines.append(f"• RSI(14): {_clean_num(rsi, 2)}  | ATR: {_clean_num(atr, 6)}")
        lines.append(f"• EMA20/50: {_clean_num(ema20,6)} / {_clean_num(ema50,6)}")
        lines.append(f"• EMA100/200: {_clean_num(ema100,6)} / {_clean_num(ema200,6)}  | SMA200: {_clean_num(sma200,6)}")
        lines.append(f"• MACD: {_clean_num(macd,6)}  Sig: {_clean_num(macds,6)}  Hist: {_clean_num(macdh,6)}")
        lines.append(f"• ADX/DI+/DI-: {_clean_num(adx,2)} / {_clean_num(diplus,2)} / {_clean_num(dimin,2)}  ({trend_read(adx,diplus,dimin)})")
        lines.append(f"• BB U/L: {_clean_num(bbu,6)} / {_clean_num(bbl,6)}  | Width: {_clean_num(bbw,6)}")
        if swh is not None or swl is not None or hiDate or loDate:
            lines.append(f"• Swing H/L: {_clean_num(swh,6)} / {_clean_num(swl,6)}")
                         #+ (f"  | Dates: {hiDate or '—'} / {loDate or '—'}" if (hiDate or loDate) else ""))

        note = _get(payload, "note")
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

# ----------------- Boot -----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
