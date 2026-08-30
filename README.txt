Crypto Breakout Bot V10 — Auto scan every 2 minutes + chart alerts

- يفحص تلقائياً كل دقيقتين بدون الحاجة إلى /scan.
- BTC مسموح الآن.
- يستبعد العملات المستقرة كأصل أساسي (USDT/USDC/FDUSD وغيرها).
- يفحص جميع أزواج Binance Spot المتداولة مقابل USDT عندما تكون MAX_COINS=0، وليس فقط أعلى 50 عملة.
- لذلك تشمل القائمة العملات الجديدة/الأقل حجماً ما دامت Binance تعرضها كزوج USDT متداولاً.
- يرسل صورة شارت خفيفة: شموع + Volume + MACD histogram.
- شروط التنبيه نفسها: ارتفاع الحجم/السيولة، شراء هجومي، CVD، Order Book، RSI، MACD، VWMA، كسر القمة السابقة، الخروج من التذبذب، والتوسع في الحركة.
- يميز PRE-BREAKOUT و BREAKOUT و EXPLOSIVE.
- يستخدم Binance Spot public market data عبر data-api.binance.vision لتجنب مشكلة HTTP 451 التي ظهرت على Futures API.
- لا يحتاج Binance API key لأنه لا ينفذ صفقات.
- ضع TELEGRAM_TOKEN و CHAT_ID في Environment Variables في Pella.
- TIMEFRAME الافتراضي 5m. يمكنك تغييره إلى 15m إذا أردت إشارات أهدأ.
- SCAN_EVERY_SECONDS=120 = فحص كل دقيقتين.
- MAX_COINS=0 = جميع العملات المتاحة في أزواج USDT المتداولة على Binance Spot.
- لا يفحص عملات DEX-only أو العملات التي لا تظهر في Binance Spot exchangeInfo.
- عملات Binance Alpha التي أصبحت لها زوج USDT متداول على Binance Spot تدخل تلقائياً؛ أما Alpha/DEX-only فلا تدخل.
- MIN_SCORE=6.0.
- COOLDOWN_MINUTES=30 لمنع تكرار نفس العملة بسرعة.

ملاحظة: "السيولة الحقيقية" هنا تعني مؤشرات سوقية عامة مثل حجم التداول، حجم الاقتباس، taker-buy/CVD ودفتر الأوامر؛ لا يمكن لواجهة Binance العامة تحديد هوية المتداولين أو ضمان أن السيولة غير متلاعب بها.

FUTURES_BASE=https://fapi.binance.com
SPOT_BASE=https://api.binance.com
