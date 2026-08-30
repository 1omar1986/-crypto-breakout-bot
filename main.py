# Binance market endpoints
# Spot: public market data
SPOT_BASE = os.getenv("SPOT_BASE", "https://api.binance.com")
# USD-M Futures: public market data (OI/Funding/Liquidations)
FUTURES_BASE = os.getenv("FUTURES_BASE", "https://fapi.binance.com")

import os
import asyncio
import logging
import time
import io
from collections import defaultdict

from PIL import Image, ImageDraw, ImageFont

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

# Binance's public market-data mirror. It avoids the Futures API geo-restriction
# that returned HTTP 451 on the Pella server.
BASE = os.getenv("BINANCE_BASE", "https://data-api.binance.vision")
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
MIN_SCORE = float(os.getenv("MIN_SCORE", "6"))
SCAN_EVERY = int(os.getenv("SCAN_EVERY_SECONDS", "300"))
MAX_COINS = int(os.getenv("MAX_COINS", "0"))
COOLDOWN = int(os.getenv("COOLDOWN_MINUTES", "30"))
CHAT_ID = os.getenv("CHAT_ID", "").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("crypto-bot")
last_alert = {}
scan_lock = asyncio.Lock()

INTERVALS = {"1m","3m","5m","15m","30m","1h","2h","4h","6h","8h","12h","1d","3d","1w","1M"}

def ema(values, n):
    if not values:
        return []
    alpha = 2.0 / (n + 1)
    out = [float(values[0])]
    for x in values[1:]:
        out.append(alpha * float(x) + (1 - alpha) * out[-1])
    return out

def rsi(values, n=14):
    if len(values) < 2:
        return [50.0] * len(values)
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(values)):
        d = float(values[i]) - float(values[i-1])
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    alpha = 1.0 / n
    ag = gains[0]
    al = losses[0]
    out = [50.0]
    for i in range(1, len(values)):
        ag = alpha * gains[i] + (1-alpha) * ag
        al = alpha * losses[i] + (1-alpha) * al
        if al == 0:
            out.append(100.0 if ag > 0 else 50.0)
        else:
            rs = ag / al
            out.append(100.0 - 100.0/(1.0+rs))
    return out

def macd(values):
    e12 = ema(values, 12)
    e26 = ema(values, 26)
    line = [a-b for a,b in zip(e12,e26)]
    sig = ema(line, 9)
    hist = [a-b for a,b in zip(line,sig)]
    return line, sig, hist

def rolling_sum(values, n):
    out = [None] * len(values)
    s = 0.0
    for i,x in enumerate(values):
        s += float(x)
        if i >= n:
            s -= float(values[i-n])
        if i >= n-1:
            out[i] = s
    return out

def vwma(close, volume, n=20):
    pv = [float(c)*float(v) for c,v in zip(close,volume)]
    spv = rolling_sum(pv,n)
    sv = rolling_sum(volume,n)
    return [None if a is None or b in (None,0) else a/b for a,b in zip(spv,sv)]

def atr(high, low, close, n=14):
    tr = [None] * len(close)
    for i in range(len(close)):
        if i == 0:
            tr[i] = float(high[i]) - float(low[i])
        else:
            tr[i] = max(
                float(high[i])-float(low[i]),
                abs(float(high[i])-float(close[i-1])),
                abs(float(low[i])-float(close[i-1]))
            )
    rs = rolling_sum(tr,n)
    return [None if x is None else x/n for x in rs]

def cvd(taker_buy, volume):
    delta = [2.0*float(tb)-float(v) for tb,v in zip(taker_buy,volume)]
    total = 0.0
    out = []
    for d in delta:
        total += d
        out.append(total)
    return out, delta

async def get(session, path, params=None):
    async with session.get(BASE + path, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status >= 400:
            body = await r.text()
            raise RuntimeError(f"HTTP {r.status} {path}: {body[:300]}")
        return await r.json()

# Base assets that should NEVER be scanned/alerted.
# USDT remains the quote currency. BTC is intentionally allowed.
EXCLUDED_BASES = {
    "WBTC",
    "USDT", "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD",
    "USDE", "USDS", "USD1", "PYUSD", "EUR", "EURI", "AEUR",
    "USTC", "FRAX", "LUSD", "CRVUSD", "GUSD", "RLUSD", "XUSD",
    "USDD", "USDJ", "CUSD", "DOLA", "SUSD", "USDX"
}

async def symbols(session):
    info = await get(session, "/api/v3/exchangeInfo")
    return [
        s["symbol"] for s in info["symbols"]
        if s.get("status") == "TRADING"
        and s.get("quoteAsset") == "USDT"
        and s.get("baseAsset") not in EXCLUDED_BASES
        and s.get("isSpotTradingAllowed", True)
    ]

async def ticker24(session):
    x = await get(session, "/api/v3/ticker/24hr")
    return {a["symbol"]: a for a in x}

async def klines(session, symbol):
    x = await get(session, "/api/v3/klines",
                  {"symbol": symbol, "interval": TIMEFRAME, "limit": 150})
    # Binance kline:
    # 0 open time, 1 open, 2 high, 3 low, 4 close, 5 volume,
    # 6 close time, 7 quote volume, 8 trades, 9 taker-buy base volume...
    return {
        "open": [float(k[1]) for k in x],
        "high": [float(k[2]) for k in x],
        "low": [float(k[3]) for k in x],
        "close": [float(k[4]) for k in x],
        "volume": [float(k[5]) for k in x],
        "taker_buy": [float(k[9]) for k in x],
    }

async def orderbook(session, symbol):
    x = await get(session, "/api/v3/depth", {"symbol": symbol, "limit": 20})
    bids = sum(float(p)*float(q) for p,q in x["bids"])
    asks = sum(float(p)*float(q) for p,q in x["asks"])
    return (bids-asks)/(bids+asks) if bids+asks else 0.0

async def analyze(session, symbol, tick):
    d = await klines(session, symbol)
    if len(d["close"]) < 100:
        return None

    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
    R = rsi(c)
    M, MS, H = macd(c)
    V = vwma(c, v)
    A = atr(h, l, c)
    C, D = cvd(d["taker_buy"], v)

    # Use the last CLOSED candle for reliable signals.
    i, p = -2, -3
    if V[i] is None or V[p] is None or A[i] is None:
        return None

    price, prev = c[i], c[p]
    atr_now = A[i]
    if not atr_now or price <= 0:
        return None

    # Liquidity/participation: candle quote-volume proxy and trade count.
    quote_vol = v[i] * price
    base_quote = sum(v[-22:-2]) / max(1, len(v[-22:-2])) * price
    volratio = quote_vol / base_quote if base_quote else 0.0
    trades = int(tick.get("count", 0) or 0)
    quote24 = float(tick.get("quoteVolume", 0) or 0)
    change24 = float(tick.get("priceChangePercent", 0) or 0)

    rn, rp = R[i], R[p]
    cross = M[p] <= MS[p] and M[i] > MS[i]
    hist_up = H[i] > H[p]
    vbreak = prev <= V[p] and price > V[i]
    delt = D[i]
    cvdr = C[i] > C[p]

    # Previous swing high: high of the prior 20 CLOSED candles.
    prior_high = max(h[-22:-2])
    breakout = price > prior_high
    breakout_pct = (price / prior_high - 1) * 100 if prior_high else 0

    # Long sideways/consolidation detector: narrow range + falling ATR relative to price.
    look = 24
    recent_hi = max(h[-look-2:-2])
    recent_lo = min(l[-look-2:-2])
    range_pct = (recent_hi - recent_lo) / max(recent_lo, 1e-12) * 100
    atr_pct = atr_now / price * 100
    atr_old = A[-26] if len(A) >= 26 else atr_now
    compressed = range_pct <= 8.0 and atr_pct <= 2.5 and atr_now <= atr_old * 1.15

    # Current candle expansion out of a compressed range.
    expansion = (h[i] - l[i]) / max(price, 1e-12) * 100 >= max(1.2, atr_pct * 1.2)
    range_break = price > recent_hi and compressed

    # Spot order-book pressure: positive = bids dominate, negative = asks dominate.
    imb = await orderbook(session, symbol)

    # Real-money proxy: taker-buy volume / total volume. This is NOT a literal
    # identity of traders; it measures aggressive buy-side flow in the candle.
    taker_buy_ratio = d["taker_buy"][i] / v[i] if v[i] else 0.5
    buy_pressure = taker_buy_ratio >= 0.60 and delt > 0
    sell_pressure = taker_buy_ratio <= 0.40 and delt < 0

    score = 0.0
    why = []
    category = "WATCH"

    if volratio >= 4:
        score += 2.5; why.append(f"💰 سيولة/حجم {volratio:.1f}x")
    elif volratio >= 2.5:
        score += 2; why.append(f"💰 حجم {volratio:.1f}x")
    elif volratio >= 1.8:
        score += 1; why.append(f"💰 حجم {volratio:.1f}x")

    if buy_pressure:
        score += 1.5; why.append(f"🟢 شراء هجومي {taker_buy_ratio*100:.0f}%")
    elif sell_pressure:
        score -= 0.75; why.append(f"🔴 بيع هجومي {taker_buy_ratio*100:.0f}%")

    if rp < 50 <= rn:
        score += 1; why.append(f"RSI صاعد {rp:.0f}→{rn:.0f}")
    elif 52 <= rn <= 72 and rn > rp:
        score += 0.75; why.append(f"RSI يرتفع {rn:.0f}")

    if cross:
        score += 1.25; why.append("MACD bullish cross")
    elif hist_up:
        score += 0.5; why.append("MACD يتحسن")

    if vbreak:
        score += 0.75; why.append("اختراق VWMA")
    elif price > V[i] and V[i] > V[p]:
        score += 0.5; why.append("فوق VWMA صاعد")

    if cvdr and delt > 0:
        score += 1.25; why.append("CVD/تدفق شراء إيجابي")

    if breakout:
        score += 2; category = "BREAKOUT"
        why.append(f"🚀 كسر القمة السابقة {breakout_pct:+.2f}%")

    if compressed:
        score += 1; category = "PRE-BREAKOUT"
        why.append(f"📦 تجميع/عرضي {range_pct:.1f}%")

    if range_break:
        score += 1.5; category = "BREAKOUT"
        why.append("🚀 خروج من نطاق عرضي")

    if expansion:
        score += 0.75; why.append("⚡ توسع في الحركة")

    if imb >= 0.25:
        score += 1; why.append(f"🟢 طلبات شراء أعلى {imb*100:+.0f}%")
    elif imb <= -0.25:
        score -= 0.5; why.append(f"🔴 طلبات بيع أعلى {abs(imb)*100:.0f}%")

    # Only alert on bullish opportunity signals; avoid pure selling signals.
    if score < MIN_SCORE or sell_pressure and imb < 0:
        return None

    if score >= 8:
        category = "EXPLOSIVE"
    elif score >= 7 and category == "WATCH":
        category = "STRONG"

    score = min(score, 10.0)

    return {
        "symbol": symbol,
        "price": price,
        "score": score,
        "category": category,
        "rsi": rn,
        "vol": volratio,
        "delta": delt,
        "taker": taker_buy_ratio,
        "macdh": H[i],
        "vwma": V[i],
        "imb": imb,
        "change24": change24,
        "atr": atr_now,
        "range_pct": range_pct,
        "quote24": quote24,
        "trades24": trades,
        "why": why,
        "ohlcv": d,
    }

def fmt_price(x):
    if x >= 1000: return f"{x:,.2f}"
    if x >= 1: return f"{x:.3f}"
    return f"{x:.6f}"

def alert_text(a):
    cat = {
        "EXPLOSIVE": "🚨🔥 EXPLOSIVE MOVE",
        "BREAKOUT": "🚀 BREAKOUT",
        "PRE-BREAKOUT": "👀🔥 PRE-BREAKOUT",
        "STRONG": "🔥 STRONG BULLISH SIGNAL",
        "WATCH": "👀 WATCH",
    }.get(a["category"], "🔥 BULLISH SIGNAL")

    # Make the Telegram alert look like a compact market-alert post:
    # symbol/timeframe + signal + last price + the evidence behind it.
    reasons = "\n".join(f"• {x}" for x in a["why"][:8])
    signal = "MACD bullish crossover" if any("MACD bullish cross" in x for x in a["why"]) else cat
    return (
        f"<b>💎 ${a['symbol']} ({TIMEFRAME})</b> <b>{signal}</b> 🔥\n\n"
        f"<b>Last price:</b> {fmt_price(a['price'])}\n"
        f"<b>Score:</b> {a['score']:.1f}/10\n"
        f"<b>24h:</b> {a['change24']:+.2f}%\n\n"
        f"💰 <b>Volume:</b> {a['vol']:.2f}x average\n"
        f"🟢 <b>Aggressive buys:</b> {a['taker']*100:.0f}%\n"
        f"🧲 <b>CVD Delta:</b> {a['delta']:+.2f}\n"
        f"📈 <b>RSI:</b> {a['rsi']:.1f}\n"
        f"📉 <b>MACD hist:</b> {a['macdh']:+.6f}\n"
        f"📚 <b>Order book:</b> {a['imb']*100:+.1f}%\n"
        f"📦 <b>24-candle range:</b> {a['range_pct']:.1f}%\n\n"
        f"<b>🔎 Why it triggered:</b>\n{reasons}\n\n"
        f"<i>⚠️ Market signal only — not a guaranteed breakout or trading advice.</i>"
    )



def make_chart(a):
    """Create a lightweight candlestick + volume + MACD chart in memory."""
    d = a["ohlcv"]
    closes = d["close"][-61:-1]
    opens = d["open"][-61:-1]
    highs = d["high"][-61:-1]
    lows = d["low"][-61:-1]
    vols = d["volume"][-61:-1]
    _, _, hist = macd(d["close"][-80:-1])
    hist = hist[-60:]

    W, HGT = 1200, 760
    im = Image.new("RGB", (W, HGT), (13, 17, 25))
    dr = ImageDraw.Draw(im)
    try:
        bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)
        normal = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
    except Exception:
        bold = normal = small = ImageFont.load_default()

    title = f"${a['symbol']} ({TIMEFRAME})  {a['category']}"
    dr.text((35, 22), title, font=bold, fill=(245,245,245))
    dr.text((35, 62), f"Last price: {fmt_price(a['price'])}   Score: {a['score']:.1f}/10   24h: {a['change24']:+.2f}%", font=normal, fill=(180,190,205))

    left, right = 55, 1145
    top, price_bottom = 115, 455
    vol_top, vol_bottom = 475, 570
    macd_top, macd_bottom = 595, 720

    hi, lo = max(highs), min(lows)
    pad = (hi-lo)*0.06 or max(hi*0.01, 1e-8)
    hi += pad; lo -= pad
    def py(v): return price_bottom - (v-lo)/(hi-lo)*(price_bottom-top)
    n=len(closes); step=(right-left)/n; body=max(5, step*0.58)
    maxv=max(vols) or 1

    for k in range(5):
        y=top+k*(price_bottom-top)/4
        dr.line((left,y,right,y), fill=(34,40,52), width=1)
    for j in range(n):
        x=left+(j+0.5)*step
        o,c,hv,lv=opens[j],closes[j],highs[j],lows[j]
        up=c>=o
        stroke=(45,210,140) if up else (235,85,100)
        dr.line((x,py(lv),x,py(hv)), fill=stroke, width=2)
        y1,y2=py(o),py(c)
        if abs(y2-y1)<2: y2=y1+2
        dr.rectangle((x-body/2,min(y1,y2),x+body/2,max(y1,y2)), fill=stroke)

    # volume
    for j,v in enumerate(vols):
        x=left+(j+0.5)*step
        bh=(v/maxv)*(vol_bottom-vol_top)
        c=(45,210,140) if closes[j]>=opens[j] else (235,85,100)
        dr.rectangle((x-body/2,vol_bottom-bh,x+body/2,vol_bottom), fill=c)
    dr.text((left, vol_top-24), "VOLUME", font=small, fill=(125,140,160))

    # MACD histogram
    m=max(max(abs(x) for x in hist), 1e-9)
    mid=(macd_top+macd_bottom)//2
    dr.line((left,mid,right,mid), fill=(60,65,78), width=1)
    for j,xv in enumerate(hist):
        x=left+(j+0.5)*step
        bh=(xv/m)*(macd_bottom-macd_top)*0.43
        c=(45,190,145) if xv>=0 else (220,80,100)
        dr.rectangle((x-body/2,mid-bh,x+body/2,mid), fill=c) if bh>=0 else dr.rectangle((x-body/2,mid,x+body/2,mid-bh), fill=c)
    dr.text((left, macd_top-24), "MACD HISTOGRAM", font=small, fill=(125,140,160))
    dr.text((right-250, 62), "BINANCE SPOT", font=small, fill=(125,140,160))

    out=io.BytesIO()
    im.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out

def alert_keyboard(symbol):
    # Direct links make the alert behave like a compact channel post.
    tv_symbol = symbol
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 TradingView", url=f"https://www.tradingview.com/chart/?symbol=BINANCE:{tv_symbol}"),
         InlineKeyboardButton("🟡 Binance", url=f"https://www.binance.com/en/trade/{symbol}?type=spot")]
    ])

async def scan(app, chat_id):
    async with scan_lock:
        timeout = aiohttp.ClientTimeout(total=25)
        connector = aiohttp.TCPConnector(limit=12)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as s:
            syms = await symbols(s)
            t = await ticker24(s)
            syms = sorted(
                syms,
                key=lambda x: float(t.get(x, {}).get("quoteVolume", 0)),
                reverse=True
            )
            # MAX_COINS=0 means all Binance Spot USDT pairs.
            if MAX_COINS > 0:
                syms = syms[:MAX_COINS]

            # Analyze concurrently to finish each 2-minute cycle quickly.
            # Keep concurrency moderate to avoid public API rate-limit spikes.
            sem = asyncio.Semaphore(12)
            async def one(sym):
                async with sem:
                    try:
                        return await analyze(s, sym, t.get(sym, {}))
                    except Exception as e:
                        log.warning("%s: %s", sym, e)
                        return None
            results = await asyncio.gather(*(one(x) for x in syms))
            found = [x for x in results if x]
            found.sort(key=lambda x: x["score"], reverse=True)

            sent = 0
            now = time.time()
            for a in found[:7]:
                if now - last_alert.get(a["symbol"], 0) < COOLDOWN * 60:
                    continue
                last_alert[a["symbol"]] = now
                chart = make_chart(a)
                await app.bot.send_photo(
                    chat_id=chat_id,
                    photo=chart,
                    caption=alert_text(a),
                    parse_mode="HTML",
                    reply_markup=alert_keyboard(a["symbol"]),
                )
                sent += 1
            log.info("auto scan: %d candidates, %d alerts", len(found), sent)
            return len(found), sent

async def background(app):
    # CHAT_ID allows fully automatic alerts without requiring /scan or /start.
    # If absent, /start can still register the chat once.
    if CHAT_ID:
        app.bot_data["chat_id"] = int(CHAT_ID)
    while True:
        chat = app.bot_data.get("chat_id")
        if chat:
            try:
                await scan(app, chat)
            except Exception:
                log.exception("scan failed")
        else:
            log.warning("No CHAT_ID yet. Send /start once or set CHAT_ID in Pella.")
        await asyncio.sleep(SCAN_EVERY)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text(
        "✅ تم ربط البوت.\n\n"
        "/scan فحص فوري\n/status الإعدادات\n"
        "الفحص التلقائي يعمل كل دقيقتين."
    )

async def scan_cmd(update, context):
    context.application.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text("🔎 بدأ الفحص...")
    try:
        n, s = await scan(context.application, update.effective_chat.id)
        await update.message.reply_text(f"✅ انتهى الفحص: {n} إشارات مرشحة، أرسلت {s} تنبيه.")
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ: {e}")

async def status(update, context):
    await update.message.reply_text(
        f"⚙️ Settings\n"
        f"Timeframe: {TIMEFRAME}\n"
        f"Minimum score: {MIN_SCORE}/10\n"
        f"Scan: every {SCAN_EVERY}s\n"
        f"Coins: {'ALL' if MAX_COINS == 0 else MAX_COINS}\n"
        f"Cooldown: {COOLDOWN}m\n"
        "Data: Binance public Spot market data"
    )

async def post_init(app):
    asyncio.create_task(background(app))

def main():
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_TOKEN first.")
    if TIMEFRAME not in INTERVALS:
        raise SystemExit(f"Unsupported TIMEFRAME: {TIMEFRAME}")
    # Pella can have slow/intermittent outbound connections. Use longer Telegram
    # HTTP timeouts and retry application bootstrap instead of failing immediately.
    tg_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30,
        read_timeout=60,
        write_timeout=60,
        pool_timeout=30,
    )
    tg_updates_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30,
        read_timeout=90,
        write_timeout=60,
        pool_timeout=30,
    )
    app = (Application.builder()
           .token(TOKEN)
           .request(tg_request)
           .get_updates_request(tg_updates_request)
           .post_init(post_init)
           .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("help", status))
    app.run_polling(bootstrap_retries=-1)

if __name__ == "__main__":
    main()
