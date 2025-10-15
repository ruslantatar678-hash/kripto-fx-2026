import os, sys, asyncio, csv, io, math, random
from datetime import datetime, timezone
from dotenv import load_dotenv
import aiohttp
import pandas as pd
import numpy as np

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import F

# load local .env if present (Render uses environment variables)
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
ALPHAVANTAGE_API_KEY = os.getenv('ALPHAVANTAGE_API_KEY', '').strip()
FX_DEFAULT = os.getenv('FX_DEFAULT', 'EUR/USD')

# available pairs list (AlphaVantage expects from_symbol and to_symbol)
PAIRS = [
    'EUR/USD', 'GBP/USD', 'USD/JPY', 'USD/CHF', 'USD/CAD', 'AUD/USD'
]

# simple CSV log path
LOG_CSV = 'signals_log.csv'
if not os.path.exists(LOG_CSV):
    with open(LOG_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['time_utc','chat_id','pair','direction','price','horizon_min','strength','rsi','ma5','ma14'])

# safety checks
if not TELEGRAM_BOT_TOKEN:
    print('❌ ERROR: TELEGRAM_BOT_TOKEN is not set. Add it to environment variables.', flush=True)
    sys.exit(1)
if not ALPHAVANTAGE_API_KEY:
    print('❌ ERROR: ALPHAVANTAGE_API_KEY is not set. Add it to environment variables.', flush=True)
    sys.exit(1)

print('✅ TELEGRAM token length:', len(TELEGRAM_BOT_TOKEN), flush=True)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# user state: remember selected pair per chat
user_pair = {}  # chat_id -> pair string like 'EUR/USD'

def split_pair(pair_str):
    base, quote = pair_str.split('/')
    return base.strip(), quote.strip()

async def fetch_fx_intraday_csv(from_symbol, to_symbol, api_key, interval='1min'):
    url = 'https://www.alphavantage.co/query'
    params = {
        'function': 'FX_INTRADAY',
        'from_symbol': from_symbol,
        'to_symbol': to_symbol,
        'interval': interval,
        'datatype': 'csv',
        'outputsize': 'compact',
        'apikey': api_key
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=30) as resp:
            text = await resp.text()
            if text.strip().startswith('{') or 'Note' in text or 'Error' in text:
                raise RuntimeError('AlphaVantage error or rate limit: ' + text[:200])
            df = pd.read_csv(io.StringIO(text))
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df.sort_values('timestamp', inplace=True)
            return df

def compute_indicators_from_series(close_series):
    # close_series: pandas Series sorted by time ascending
    close = close_series.astype(float)
    ma5 = close.rolling(window=5).mean().iloc[-1] if len(close) >=5 else float('nan')
    ma14 = close.rolling(window=14).mean().iloc[-1] if len(close) >=14 else float('nan')
    # RSI
    delta = close.diff().dropna()
    if len(delta) < 1:
        rsi_val = float('nan')
    else:
        up = delta.clip(lower=0)
        down = -1 * delta.clip(upper=0)
        roll_up = up.ewm(span=14, adjust=False).mean()
        roll_down = down.ewm(span=14, adjust=False).mean()
        rs = roll_up / (roll_down.replace(0, float('nan')))
        rsi = 100 - (100 / (1 + rs))
        rsi_val = float(rsi.iloc[-1]) if not rsi.isna().all() else float('nan')
    return rsi_val, float(ma5) if not math.isnan(ma5) else float('nan'), float(ma14) if not math.isnan(ma14) else float('nan')

def determine_signal(rsi, ma5, ma14, last_close):
    # Determine direction by RSI and MA crossover, and strength
    direction = None
    strength = 'low'
    horizon = 2  # default minutes
    # MA trend
    if not math.isnan(ma5) and not math.isnan(ma14):
        if ma5 > ma14:
            ma_trend = 'up'
        elif ma5 < ma14:
            ma_trend = 'down'
        else:
            ma_trend = 'flat'
    else:
        ma_trend = 'flat'
    # RSI rules
    if not math.isnan(rsi):
        if rsi < 25:
            direction = 'BUY'
            strength = 'high'
            horizon = 5
        elif rsi < 35:
            direction = 'BUY'
            strength = 'medium'
            horizon = 3
        elif rsi > 75:
            direction = 'SELL'
            strength = 'high'
            horizon = 5
        elif rsi > 65:
            direction = 'SELL'
            strength = 'medium'
            horizon = 3
    # MA confirmation
    if direction == 'BUY' and ma_trend == 'up':
        if strength == 'low':
            strength = 'medium'; horizon = 3
        elif strength == 'medium':
            strength = 'high'; horizon = max(horizon,4)
    if direction == 'SELL' and ma_trend == 'down':
        if strength == 'low':
            strength = 'medium'; horizon = 3
        elif strength == 'medium':
            strength = 'high'; horizon = max(horizon,4)
    # fallback: if no RSI signal, use MA trend
    if direction is None and ma_trend != 'flat':
        direction = 'BUY' if ma_trend == 'up' else 'SELL'
        strength = 'low'
        horizon = 2
    # if still none, neutral
    if direction is None:
        direction = 'NEUTRAL'
        strength = 'low'
        horizon = 2
    return direction, strength, int(horizon)

def format_signal_message(pair, direction, price, horizon, strength, rsi, ma5, ma14):
    emoji = '🔼' if direction == 'BUY' else ('🔽' if direction == 'SELL' else '⚪️')
    pair_line = f"💹 {pair}"
    dir_line = f"{emoji} Сигнал: {direction}"
    ind_line = f"📊 RSI: {rsi:.2f} | MA5: {ma5:.5f} | MA14: {ma14:.5f}"
    price_line = f"💰 Цена: {price:.5f}"
    horizon_line = f"⏱ Рекомендуемое время: {horizon} мин"
    strength_line = f"🎯 Сила сигнала: {strength}"
    return '\n'.join([pair_line, dir_line, ind_line, price_line, horizon_line, strength_line])

@dp.message(Command('start'))
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📊 Получить рандомный сигнал', callback_data='get_random')],
        [InlineKeyboardButton(text='🔁 Выбрать и запомнить пару', callback_data='choose_pair')],
        [InlineKeyboardButton(text='📁 Получить логи', callback_data='get_logs')],
    ])
    await message.answer('Привет! Нажми, чтобы получить сигнал (рандомная пара) или выбрать пару для запоминания.', reply_markup=kb)

@dp.callback_query(F.data == 'get_random')
async def cb_get_random(call):
    chat_id = call.message.chat.id
    pair = random.choice(PAIRS)
    user_pair[chat_id] = pair  # remember chosen random pair
    await call.message.answer(f'Выбрана пара: {pair} — собираю данные...')
    try:
        base, quote = split_pair(pair)
        df = await fetch_fx_intraday_csv(base, quote, ALPHAVANTAGE_API_KEY)
        # use last up to 30 minutes of 1-min bars (AlphaVantage compact gives last 100)
        closes = df['close'].astype(float)
        rsi, ma5, ma14 = compute_indicators_from_series(closes)
        last_price = float(closes.iloc[-1])
        direction, strength, horizon = determine_signal(rsi, ma5, ma14, last_price)
        msg = format_signal_message(pair, direction, last_price, horizon, strength, rsi if not math.isnan(rsi) else 0.0, ma5 if not math.isnan(ma5) else 0.0, ma14 if not math.isnan(ma14) else 0.0)
        await call.message.answer(msg)
        # log
        with open(LOG_CSV, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([datetime.utcnow().isoformat(), chat_id, pair, direction, f'{last_price:.6f}', horizon, strength, f'{rsi:.4f}', f'{ma5:.6f}', f'{ma14:.6f}'])
    except Exception as e:
        await call.message.answer(f'Ошибка при получении данных: {e}')

@dp.callback_query(F.data == 'choose_pair')
async def cb_choose_pair(call):
   kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text=p, callback_data=f"pair_{p.replace('/', '_')}")] for p in PAIRS
])
    await call.message.answer('Выбери пару для запоминания:', reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith('pair_'))
async def cb_pair_selected(call):
    chat_id = call.message.chat.id
    pair = call.data.replace('pair_', '').replace('_','/')
    user_pair[chat_id] = pair
    await call.message.answer(f'Пара {pair} сохранена. Теперь по кнопке будет использоваться она.')

@dp.callback_query(F.data == 'get_logs')
async def cb_get_logs(call):
    if os.path.exists(LOG_CSV):
        await call.message.answer_document(open(LOG_CSV, 'rb'))
    else:
        await call.message.answer('Логов пока нет.')

async def main():
    print('🚀 Bot started polling', flush=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print('Bot stopped', flush=True)
