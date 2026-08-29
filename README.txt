# Crypto Breakout Scanner V2

## What it does
Scans the most liquid Binance USDT perpetual contracts and scores:
- CVD proxy from taker-buy volume
- Volume expansion
- RSI momentum
- MACD
- VWMA
- Order-book bid/ask imbalance
- Open Interest change (from bot snapshots)
- Funding rate
- Recent forced-order/liquidation data where Binance exposes it
- 20-candle breakout

It sends Telegram alerts only when SCORE >= MIN_SCORE.

## Create Telegram bot
1. Open @BotFather
2. /newbot
3. Choose a name and username ending in bot
4. Copy the token. Never publish it.

## Run on a VPS (Ubuntu)
sudo apt update
sudo apt install python3 python3-pip unzip -y
unzip crypto_breakout_bot_v2.zip
cd crypto_breakout_bot_v2
python3 -m pip install -r requirements.txt
export TELEGRAM_TOKEN='YOUR_TOKEN'
python3 bot.py

Open the bot in Telegram, press Start, then /scan.

## Keep it running
For a simple test:
python3 bot.py

For 24/7 production, use a VPS process manager such as systemd. Example:
sudo nano /etc/systemd/system/crypto-bot.service

[Unit]
After=network.target

[Service]
WorkingDirectory=/root/crypto_breakout_bot_v2
Environment=TELEGRAM_TOKEN=YOUR_TOKEN
Environment=TIMEFRAME=15m
Environment=MIN_SCORE=7
ExecStart=/usr/bin/python3 /root/crypto_breakout_bot_v2/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target

Then:
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-bot
sudo systemctl status crypto-bot

## Important limitations
CVD is a candle-level taker-buy-volume proxy, not a full trade-by-trade CVD from every venue.
Order-book liquidity is a snapshot and can change quickly.
Liquidation availability depends on Binance's public endpoint behavior.
Open Interest change is measured between bot scans; restart resets its baseline.
The bot does NOT place trades.
