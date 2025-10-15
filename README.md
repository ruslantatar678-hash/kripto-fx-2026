Forex Random-Pair Signal Bot (Alpha Vantage)

Features:
- Press '📊 Получить рандомный сигнал' to get a signal for a random FX pair.
- Optionally choose and remember a pair (Choose & Save).
- Signals computed using RSI + MA5/MA14. Bot recommends 2-5 min horizon.
- Logs saved to signals_log.csv

Deploy:
- Fill TELEGRAM_BOT_TOKEN and ALPHAVANTAGE_API_KEY as environment variables (Render or local .env).
- Build Docker or run locally.

Local run example:
1. pip install -r requirements.txt
2. copy .env.example -> .env, fill keys
3. python bot.py
