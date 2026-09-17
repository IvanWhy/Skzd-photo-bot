import os
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import google.generativeai as genai
from PIL import Image
import io
import uvicorn

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", 0))

if not BOT_TOKEN: raise ValueError("❌ BOT_TOKEN не найден!")
if not GEMINI_API_KEY: raise ValueError("❌ GEMINI_API_KEY не найден!")
if not ADMIN_CHAT_ID: raise ValueError(" ADMIN_CHAT_ID не найден!")
if not CHANNEL_ID: raise ValueError("❌ CHANNEL_ID не найден!")

# Настройка Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Инициализация бота
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

# Хранилище фото на модерации
pending_photos = {}

# ==================== КЛАВИАТУРЫ ====================
def get_admin_keyboard(photo_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{photo_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{photo_id}")
        ]
    ])

# ==================== КОМАНДЫ ====================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот-предложка для фотогалереи.\n\n"
        "📸 <b>Как это работает:</b>\n"
        "1. Отправь мне фото локомотива или поезда\n"
        "2. ИИ проанализирует качество фото\n"
        "3. Админы рассмотрят и опубликуют в канале\n\n"
        "Просто отправь фото!"
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 <b>Помощь:</b>\n\n"
        "/start - Запустить бота\n"
        "/help - Показать справку\n\n"
        "Просто отправь фото локомотива или поезда!"
    )

# ==================== ОБРАБОТКА ФОТО ====================
@dp.message(F.photo)
async def handle_photo(message: types.Message):
    # Получаем фото (берем самое большое)
    photo = message.photo[-1]
    
    await message.answer("⏳ Анализирую фото...")
    
    try:
        # Скачиваем фото
        photo_file = await bot.download(photo)
        photo_bytes = photo_file.read()
        
        # Анализируем через Gemini
        image = Image.open(io.BytesIO(photo_bytes))
        
        prompt = """Проанализируй эту фотографию железнодорожного транспорта. Оцени:
1. Что изображено (тип локомотива/поезда, если видно номер или серию)
2. Качество фотографии (резкость, освещение, композиция, баланс белого)
3. Подходит ли для публикации в фотогалерее (оцени по шкале 1-10)

Дай краткий анализ на русском языке (3-4 предложения). Будь объективен."""
        
        response = model.generate_content([prompt, image])
        ai_analysis = response.text
        
    except Exception as e:
        logging.error(f"Ошибка анализа фото: {e}")
        ai_analysis = "⚠️ Не удалось проанализировать фото"
    
    # Сохраняем фото для модерации
    photo_id = photo.file_id
    pending_photos[photo_id] = {
        "user_id": message.from_user.id,
        "username": message.from_user.username or message.from_user.first_name,
        "caption": message.caption or "",
        "ai_analysis": ai_analysis
    }
    
    # Отправляем пользователю подтверждение
    await message.answer(
        "✅ <b>Фото принято!</b>\n\n"
        f"🤖 <b>Анализ ИИ:</b>\n{ai_analysis}\n\n"
        "📬 Фото отправлено на модерацию админам."
    )
    
    # Отправляем админам на модерацию
    admin_ids = [ADMIN_CHAT_ID] if isinstance(ADMIN_CHAT_ID, str) else ADMIN_CHAT_ID
    for admin_id in admin_ids:
        try:
            caption = (
                f"📸 <b>Новое фото на модерацию</b>\n\n"
                f"👤 <b>Автор:</b> @{pending_photos[photo_id]['username']}\n"
                f"🤖 <b>Анализ ИИ:</b>\n{ai_analysis}"
            )
            if pending_photos[photo_id]['caption']:
                caption += f"\n\n📝 <b>Описание от автора:</b>\n{pending_photos[photo_id]['caption']}"
            
            await bot.send_photo(
                chat_id=admin_id,
                photo=photo_id,
                caption=caption,
                reply_markup=get_admin_keyboard(photo_id)
            )
        except Exception as e:
            logging.error(f"Ошибка отправки админу {admin_id}: {e}")

# ==================== ДЕЙСТВИЯ АДМИНОВ ====================
@dp.callback_query(F.data.startswith("approve:"))
async def approve_photo(callback: types.CallbackQuery):
    photo_id = callback.data.split(":")[1]
    
    if photo_id not in pending_photos:
        await callback.answer("⚠️ Фото уже обработано", show_alert=True)
        return
    
    photo_data = pending_photos.pop(photo_id)
    
    # Публикуем в канал
    try:
        caption = (
            f"📸 <b>Фото от @{photo_data['username']}</b>\n\n"
            f" <b>Анализ ИИ:</b>\n{photo_data['ai_analysis']}"
        )
        if photo_data['caption']:
            caption += f"\n\n📝 <b>Описание:</b>\n{photo_data['caption']}"
        
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=photo_id,
            caption=caption
        )
    except Exception as e:
        logging.error(f"Ошибка публикации в канал: {e}")
    
    # Уведомляем главного админа
    if MAIN_ADMIN_ID:
        try:
            admin_username = f"@{callback.from_user.username}" if callback.from_user.username else f"ID:{callback.from_user.id}"
            await bot.send_message(
                MAIN_ADMIN_ID,
                f"✅ <b>Фото одобрено и опубликовано</b>\n\n"
                f"👤 <b>Автор:</b> @{photo_data['username']}\n"
                f"👮 <b>Админ:</b> {admin_username}\n"
                f"⏰ <b>Время:</b> {__import__('datetime').datetime.now().strftime('%H:%M')}",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Ошибка уведомления: {e}")
    
    # Уведомляем пользователя
    try:
        await bot.send_message(
            photo_data['user_id'],
            "✅ <b>Поздравляем!</b> Ваше фото одобрено и опубликовано в канале!\n\n"
            "Спасибо за вклад в фотогалерею! 🚂📸"
        )
    except:
        pass
    
    await callback.answer("✅ Фото опубликовано!", show_alert=True)

@dp.callback_query(F.data.startswith("reject:"))
async def reject_photo(callback: types.CallbackQuery):
    photo_id = callback.data.split(":")[1]
    
    if photo_id not in pending_photos:
        await callback.answer("⚠️ Фото уже обработано", show_alert=True)
        return
    
    photo_data = pending_photos.pop(photo_id)
    
    # Уведомляем пользователя
    try:
        await bot.send_message(
            photo_data['user_id'],
            "❌ <b>Фото отклонено</b>\n\n"
            "К сожалению, ваше фото не прошло модерацию.\n"
            "Попробуйте отправить другое фото с лучшим качеством."
        )
    except:
        pass
    
    await callback.answer("❌ Фото отклонено", show_alert=True)

# ==================== WEBHOOK ДЛЯ VERCEL ====================
app = FastAPI()
WEBHOOK_URL = "https://skzd-photo-bot-ten.vercel.app/webhook"

@app.on_event("startup")
async def on_startup():
    try:
        await bot.set_webhook(url=WEBHOOK_URL, allowed_updates=dp.resolve_used_update_types())
        print("✅ Webhook установлен!")
    except Exception as e:
        print(f"❌ Ошибка установки webhook: {e}")

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()
    print("👋 Webhook удален!")

webhook_installed = False

@app.post("/webhook")
async def webhook(request: Request):
    global webhook_installed
    if not webhook_installed:
        try:
            info = await bot.get_webhook_info()
            if info.url != WEBHOOK_URL:
                await bot.set_webhook(url=WEBHOOK_URL, allowed_updates=dp.resolve_used_update_types())
                print("✅ Webhook обновлен!")
            webhook_installed = True
        except Exception as e:
            print(f"❌ Ошибка: {e}")
    try:
        update = types.Update(**await request.json())
        await dp.feed_update(bot, update)
        return JSONResponse({"ok": True})
    except Exception as e:
        print(f"Error: {e}")
        return JSONResponse({"ok": False}, status_code=500)

from aiogram.exceptions import TelegramBadRequest

@dp.errors()
async def errors_handler(event: types.ErrorEvent):
    if isinstance(event.exception, TelegramBadRequest):
        if "message is not modified" in str(event.exception): return True
        if "query is too old" in str(event.exception): return True
    return False

@app.get("/")
@app.head("/")
async def root():
    return {"message": "Photo Bot is running! 📸"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
