import os
import asyncio
import logging
import time
from collections import defaultdict

import aiohttp
import numpy as np
import pandas as pd
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BASE = "https://fapi.binance.com"
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
MIN_SCORE = float(os.getenv("MIN_SCORE", "7"))
SCAN_EVERY = int(os.getenv("SCAN_EVERY_SECONDS", "60"))
MAX_COINS = int(os.getenv("MAX_COINS", "100"))
COOLDOWN = int(os.getenv("COOLDOWN_MINUTES", "30"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("crypto-bot")
last_alert = {}
scan_lock = asyncio.Lock()

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    au = up.ewm(alpha=1/n, adjust=False).mean()
    ad = dn.ewm(alpha=1/n, adjust=False).mean()
    rs = au / ad.replace(0, np.nan)
    return (100 - 100/(1+rs)).fillna(50)

def macd(s):
    m = ema(s, 12) - ema(s, 26)
    sig = ema(m, 9)
    return m, sig, m-sig

def vwma(close, volume, n=20):
    return (close*volume).rolling(n).sum()/volume.rolling(n).sum()

def atr(df, n=14):
    tr = pd.concat([
        df.high-df.low,
        (df.high-df.close.shift()).abs(),
        (df.low-df.close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def cvd(df):
    delta = 2*df.taker_buy - df.volume
    return delta.cumsum(), delta

async def get(session, path, params=None):
    async with session.get(BASE+path, params=params, timeout=15) as r:
        r.raise_for_status()
        return await r.json()

async def symbols(session):
    info = await get(session, "/fapi/v1/exchangeInfo")
    return [s["symbol"] for s in info["symbols"]
            if s["status"]=="TRADING" and s["quoteAsset"]=="USDT"
            and s["contractType"]=="PERPETUAL"]

async def ticker24(session):
    x = await get(session, "/fapi/v1/ticker/24hr")
    return {a["symbol"]: a for a in x}

async def klines(session, symbol):
    x = await get(session, "/fapi/v1/klines",
                  {"symbol":symbol, "interval":TIMEFRAME, "limit":150})
    cols=["open_time","open","high","low","close","volume","close_time",
          "quote_volume","trades","taker_buy","taker_buy_quote","ignore"]
    df=pd.DataFrame(x,columns=cols)
    for c in ["open","high","low","close","volume","quote_volume","taker_buy"]:
        df[c]=pd.to_numeric(df[c])
    return df

async def orderbook(session, symbol):
    x=await get(session,"/fapi/v1/depth",{"symbol":symbol,"limit":20})
    bids=sum(float(p)*float(q) for p,q in x["bids"])
    asks=sum(float(p)*float(q) for p,q in x["asks"])
    imb=(bids-asks)/(bids+asks) if bids+asks else 0
    return imb

async def oi(session, symbol):
    x=await get(session,"/fapi/v1/openInterest",{"symbol":symbol})
    return float(x["openInterest"])

async def funding(session, symbol):
    x=await get(session,"/fapi/v1/premiumIndex",{"symbol":symbol})
    return float(x["lastFundingRate"])

async def liquidations(session, symbol):
    # Recent forced orders available from Binance's public endpoint where supported.
    try:
        x=await get(session,"/fapi/v1/allForceOrders",
                    {"symbol":symbol,"limit":100})
        long_liq=short_liq=0.0
        for z in x:
            qty=float(z.get("origQty",0)); price=float(z.get("averagePrice") or z.get("price") or 0)
            val=qty*price
            # SELL liquidation = long liquidated; BUY = short liquidated
            if z.get("side")=="SELL": long_liq += val
            elif z.get("side")=="BUY": short_liq += val
        return long_liq, short_liq
    except Exception:
        return None, None

async def analyze(session, symbol, tick):
    df=await klines(session,symbol)
    if len(df)<80: return None

    # Use the last CLOSED candle, not the currently forming candle.
    i=-2; p=-3
    c=df.close; v=df.volume
    R=rsi(c); M,MS,H=macd(c); V=vwma(c,v); A=atr(df)
    C,D=cvd(df)

    price=float(c.iloc[i]); prev=float(c.iloc[p])
    volratio=float(v.iloc[i]/v.iloc[-22:-2].mean())
    rn,rp=float(R.iloc[i]),float(R.iloc[p])
    cross=float(M.iloc[p])<=float(MS.iloc[p]) and float(M.iloc[i])>float(MS.iloc[i])
    hist_up=float(H.iloc[i])>float(H.iloc[p])
    vbreak=prev<=float(V.iloc[p]) and price>float(V.iloc[i])
    cvdr=float(C.iloc[i])>float(C.iloc[p])
    delt=float(D.iloc[i])
    breakout=price>float(df.high.iloc[-22:-2].max())
    imb=await orderbook(session,symbol)
    oi_now=await oi(session,symbol)
    fund=await funding(session,symbol)
    ll,sl=await liquidations(session,symbol)

    score=0.0; why=[]
    if volratio>=3: score+=2; why.append(f"Volume {volratio:.1f}x")
    elif volratio>=2: score+=1.5; why.append(f"Volume {volratio:.1f}x")
    elif volratio>=1.5: score+=0.75; why.append(f"Volume {volratio:.1f}x")

    if rp<50<=rn: score+=1; why.append(f"RSI {rp:.0f}->{rn:.0f}")
    elif 55<=rn<=70 and rn>rp: score+=0.75; why.append(f"RSI rising {rn:.0f}")

    if cross: score+=1.5; why.append("MACD bullish cross")
    elif hist_up and H.iloc[i]>0: score+=0.75; why.append("MACD histogram rising")

    if vbreak: score+=1; why.append("VWMA breakout")
    elif price>V.iloc[i] and V.iloc[i]>V.iloc[p]: score+=0.5; why.append("Above rising VWMA")

    if cvdr and delt>0: score+=1.5; why.append("CVD buying pressure")
    elif cvdr: score+=0.75; why.append("CVD rising")

    if breakout: score+=1; why.append("20-candle breakout")

    if imb>0.15: score+=0.75; why.append(f"Bid imbalance {imb*100:+.0f}%")

    # OI is a snapshot, so don't score its change unless a previous snapshot exists.
    key=f"oi:{symbol}"
    old=getattr(analyze,"oi_cache",{}).get(key)
    if not hasattr(analyze,"oi_cache"): analyze.oi_cache={}
    analyze.oi_cache[key]=oi_now
    oi_change=(oi_now-old)/old*100 if old else None
    if oi_change is not None and oi_change>2 and delt>0:
        score+=0.75; why.append(f"OI +{oi_change:.1f}%")

    if fund>0.0015:
        score-=0.5; why.append("Funding crowded")
    elif fund<0.0008 and fund>=0:
        why.append("Funding not crowded")

    liq_note=""
    if ll is not None:
        if sl>ll*1.3:
            score+=0.5; why.append("Short liquidations dominant")
        liq_note=f"Long liq ${ll:,.0f} | Short liq ${sl:,.0f}"

    if score<MIN_SCORE: return None
    t=tick.get(symbol,{})
    return dict(symbol=symbol,price=price,score=score,rsi=rn,vol=volratio,
                delta=delt,macdh=float(H.iloc[i]),vwma=float(V.iloc[i]),
                imb=imb,oi_change=oi_change,funding=fund,ll=ll,sl=sl,
                change24=float(t.get("priceChangePercent",0)),atr=float(A.iloc[i]),
                why=why,liq_note=liq_note)

def fmt_price(x):
    if x>=1000:return f"{x:,.2f}"
    if x>=1:return f"{x:.3f}"
    return f"{x:.6f}"

def alert_text(a):
    return (
        "🚨🔥 BREAKOUT ALERT\n\n"
        f"💎 {a['symbol']} | {TIMEFRAME}\n"
        f"🔥 SCORE: {a['score']:.1f}/10\n"
        f"💰 Price: {fmt_price(a['price'])}\n"
        f"📊 24h: {a['change24']:+.2f}%\n\n"
        f"📈 RSI: {a['rsi']:.1f}\n"
        f"📦 Volume: {a['vol']:.2f}x average\n"
        f"🧲 CVD delta: {a['delta']:+.2f}\n"
        f"📉 MACD hist: {a['macdh']:+.6f}\n"
        f"〽️ VWMA: {fmt_price(a['vwma'])}\n"
        f"💧 Orderbook imbalance: {a['imb']*100:+.1f}%\n"
        f"💵 Funding: {a['funding']*100:.4f}%\n"
        f"📈 OI change: {('—' if a['oi_change'] is None else f'{a['oi_change']:+.2f}%')}\n"
        f"🧨 Liquidations: {a['liq_note'] or 'unavailable'}\n\n"
        "🔎 Reasons:\n" + "\n".join("• "+x for x in a["why"]) +
        "\n\n⚠️ Automated market filter, not financial advice."
    )

async def scan(app, chat_id):
    async with scan_lock:
        async with aiohttp.ClientSession() as s:
            syms=await symbols(s); t=await ticker24(s)
            syms=sorted(syms,key=lambda x:float(t.get(x,{}).get("quoteVolume",0)),reverse=True)[:MAX_COINS]
            found=[]
            for sym in syms:
                try:
                    a=await analyze(s,sym,t)
                    if a: found.append(a)
                except Exception as e:
                    log.warning("%s: %s",sym,e)
                await asyncio.sleep(.04)
            found.sort(key=lambda x:x["score"],reverse=True)
            sent=0; now=time.time()
            for a in found[:5]:
                if now-last_alert.get(a["symbol"],0)<COOLDOWN*60: continue
                last_alert[a["symbol"]]=now
                await app.bot.send_message(chat_id=chat_id,text=alert_text(a))
                sent+=1
            return len(found),sent

async def background(app):
    while True:
        chat=app.bot_data.get("chat_id")
        if chat:
            try: await scan(app,chat)
            except Exception: log.exception("scan failed")
        await asyncio.sleep(SCAN_EVERY)

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["chat_id"]=update.effective_chat.id
    await update.message.reply_text(
        "✅ تم ربط البوت.\n\n"
        "/scan فحص فوري\n/status الإعدادات\n"
        "سأرسل التنبيهات تلقائيًا عند تجاوز Score المحدد."
    )

async def scan_cmd(update,context):
    context.application.bot_data["chat_id"]=update.effective_chat.id
    await update.message.reply_text("🔎 بدأ الفحص...")
    try:
        n,s=await scan(context.application,update.effective_chat.id)
        await update.message.reply_text(f"✅ انتهى الفحص: {n} إشارات مرشحة، أرسلت {s} تنبيه.")
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ: {e}")

async def status(update,context):
    await update.message.reply_text(
        f"⚙️ Settings\n"
        f"Timeframe: {TIMEFRAME}\nMinimum score: {MIN_SCORE}/10\n"
        f"Scan: every {SCAN_EVERY}s\nCoins: {MAX_COINS}\nCooldown: {COOLDOWN}m"
    )

async def post_init(app):
    asyncio.create_task(background(app))

def main():
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_TOKEN first.")
    app=Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("scan",scan_cmd))
    app.add_handler(CommandHandler("status",status))
    app.add_handler(CommandHandler("help",status))
    app.run_polling()

if __name__=="__main__":
    main()
