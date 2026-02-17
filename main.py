import asyncio
import logging
import os
import aiohttp
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Хранилище пользователей
active_users = set()

# Состояние рынка
state = {
    "last_bitget_time": 0,
    "last_bitget_price": 0.0,
    "last_prediction": None,
}

# --- API ФУНКЦИИ ---

async def fetch_bitget_kline(session):
    """Получает свечи с Bitget."""
    url = "https://api.bitget.com/api/v2/spot/market/candles"
    params = {"symbol": "BTCUSDT", "granularity": "5m", "limit": "2"}
    try:
        async with session.get(url, params=params, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data['code'] == "00000" and data['data']:
                    return data['data'][1] 
    except Exception as e:
        logging.error(f"Bitget Error: {e}")
    return None

async def fetch_polymarket_active_event(session):
    """
    Ищет на Polymarket активный рынок "BTC Up or Down".
    Polymarket API: ищем по ключевому слову или тегу.
    Рынки создаются автоматически, нам нужно найти 'condition_id' или 'slug' текущего.
    """
    # Полимаркет использует GraphQL или поисковое API.
    # Эндпоинт для поиска: https://gamma-api.polymarket.com/events
    # Ищем событие, которое содержит "BTC" и "5m" и активно сейчас.
    url = "https://gamma-api.polymarket.com/events?slug=btc-updown-5m"
    
    # Так как slug меняется ( btc-updown-5m-TIMESTAMP ), лучше искать через текстовый поиск
    search_url = "https://gamma-api.polymarket.com/events?_s=created_at&_o=desc&limit=5"
    
    try:
        async with session.get(search_url, timeout=5) as resp:
            if resp.status == 200:
                events = await resp.json()
                for event in events:
                    # Ищем в названии ключевые слова
                    if "BTC" in event.get('title', '') and "5m" in event.get('title', ''):
                        # Возвращаем slug (ссылку) и ID рынка
                        return {
                            "slug": event.get('slug'),
                            "title": event.get('title')
                        }
    except Exception as e:
        logging.error(f"Polymarket Search Error: {e}")
    return None

async def fetch_polymarket_prices(session, slug):
    """
    Получает цены "Yes" для рынка Up/Down.
    Polymarket CLOB API: https://clob.polymarket.com/markets/{slug}
    """
    if not slug: return None
    url = f"https://clob.polymarket.com/markets/{slug}"
    try:
        async with session.get(url, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                # tokens[0] - обычно Yes, tokens[1] - No (или Up/Down)
                # Нужно парсить outcome prices
                tokens = data.get('tokens', [])
                if len(tokens) >= 2:
                    # Получаем цену "Up" и "Down"
                    # Это упрощенная логика, так как структура ответа может меняться
                    return {
                        "up_price": tokens[0].get('price'), 
                        "down_price": tokens[1].get('price')
                    }
    except Exception as e:
        logging.error(f"Polymarket Price Error: {e}")
    return None

# --- ЛОГИКА РАССЫЛКИ ---

async def broadcast_signal(text):
    if not active_users: return
    tasks = []
    for user_id in active_users:
        tasks.append(bot.send_message(user_id, text, parse_mode="HTML", disable_web_page_preview=True))
    await asyncio.gather(*tasks, return_exceptions=True)

# --- ГЛАВНЫЙ ЦИКЛ ---

async def market_watcher():
    async with aiohttp.ClientSession() as session:
        while True:
            now = datetime.now(timezone.utc)
            ts_now = now.timestamp()
            mod = ts_now % 300 
            time_to_close = 300 - mod
            
            # Спим до последних 30 секунд
            if time_to_close > 35:
                await asyncio.sleep(10)
                continue
            
            # Активная фаза
            try:
                candle = await fetch_bitget_kline(session)
                if candle:
                    close_ts = int(candle[0])
                    close_price = float(candle[4])
                    
                    # Если обнаружена новая свеча
                    if close_ts != state['last_bitget_time']:
                        logging.info(f"Новая свеча Bitget: {close_price}")
                        state['last_bitget_time'] = close_ts
                        
                        prev_price = state['last_bitget_price']
                        
                        if prev_price > 0:
                            # 1. Формируем прогноз
                            if close_price > prev_price:
                                prediction_text = "⬆️ ВЫШЕ (UP)"
                                pm_outcome = "Up"
                            else:
                                prediction_text = "⬇️ НИЖЕ (DOWN)"
                                pm_outcome = "Down"
                            
                            state['last_prediction'] = prediction_text
                            time_str = datetime.fromtimestamp(close_ts/1000, tz=timezone.utc).strftime('%H:%M:%S')
                            
                            # 2. Ищем активный рынок на Polymarket
                            pm_event = await fetch_polymarket_active_event(session)
                            link = "https://polymarket.com"
                            market_status = "❌ Рынок не найден на Polymarket"
                            
                            if pm_event:
                                link = f"https://polymarket.com/event/{pm_event['slug']}"
                                market_status = f"✅ <a href='{link}'>Рынок найден на Polymarket</a>"
                                
                                # (Опционально) Можно тут же спарсить текущие шансы
                                # pm_prices = await fetch_polymarket_prices(session, pm_event['slug'])
                            
                            # 3. Отправляем сигнал
                            msg = (
                                f"⚡️ <b>СИГНАЛ 5M (BITGET)</b>\n\n"
                                f"⏰ Закрытие: <b>{time_str} UTC</b>\n"
                                f"📉 Цена: <b>{close_price}</b>\n\n"
                                f"🔮 <b>ПРОГНОЗ:</b> {prediction_text}\n\n"
                                f"{market_status}"
                            )
                            
                            await broadcast_signal(msg)
                            
                            # 4. Через 15 сек проверяем Polymarket
                            asyncio.create_task(check_pm_delayed(link, prediction_text, close_price))
                        
                        state['last_bitget_price'] = close_price
                        
            except Exception as e:
                logging.error(f"Loop Error: {e}")
            
            await asyncio.sleep(2)

async def check_pm_delayed(link, prediction, fast_price):
    """Проверяет Polymarket с задержкой."""
    await asyncio.sleep(15)
    
    # В реальном боте тут можно еще раз дернуть API Polymarket, 
    # чтобы увидеть, какие шансы стали, но главное - мы уже дали ссылку.
    msg = (
        f"⏳ <b>Проверка задержки</b>\n"
        f"Прошло 15 сек с момента сигнала.\n"
        f"Ваш прогноз: {prediction}\n"
        f"<a href='{link}'>Проверить результат на Polymarket</a>"
    )
    await broadcast_signal(msg)

# --- ХЕНДЛЕРЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.chat.id
    if user_id not in active_users:
        active_users.add(user_id)
        await message.answer(
            "✅ <b>Вы подписаны!</b>\n\n"
            "Бот следит за 5М свечами BTC.\n"
            "Как только свеча закрывается на Bitget, я:\n"
            "1. Дам прогноз (Выше/Ниже).\n"
            "2. Найду ссылку на текущий рынок Polymarket.\n"
            "3. Пришлю всё быстрее, чем обновится график на сайте.",
            parse_mode="HTML"
        )
    else:
        await message.answer("Вы уже подписаны.")

@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    active_users.discard(message.chat.id)
    await message.answer("Рассылка остановлена.")

async def on_startup(dispatcher):
    asyncio.create_task(market_watcher())

if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot, on_startup=on_startup))
