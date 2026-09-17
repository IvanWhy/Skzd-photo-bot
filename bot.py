import os
import logging
import uuid
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
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
if not ADMIN_CHAT_ID: raise ValueError("❌ ADMIN_CHAT_ID не найден!")
if not CHANNEL_ID: raise ValueError("❌ CHANNEL_ID не найден!")

# Настройка Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3.6-flash')

# Инициализация бота
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

# Хранилище фото
pending_photos = {}

# ==================== СОСТОЯНИЯ (FSM) ====================
class PhotoForm(StatesGroup):
    waiting_photo = State()
    waiting_time = State()
    waiting_date = State()
    waiting_location = State()
    waiting_train_info = State()

# ==================== КЛАВИАТУРЫ ====================
def get_admin_keyboard(short_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{short_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{short_id}")
        ]
    ])

def get_skip_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="⏭️ Пропустить")]
    ], resize_keyboard=True)

# ==================== КОМАНДЫ ====================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.set_state(PhotoForm.waiting_photo)
    await message.answer(
        "👋 Привет! Я бот-предложка для фотогалереи.\n\n"
        "📸 <b>Как это работает:</b>\n"
        "1. Отправь мне фото локомотива или поезда\n"
        "2. Укажи время, дату, место и информацию о поезде\n"
        "3. ИИ структурирует информацию\n"
        "4. Админы рассмотрят и опубликуют в канале\n\n"
        "📷 <b>Отправь фото для начала:</b>"
    )

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Заполнение отменено. Напиши /start", reply_markup=types.ReplyKeyboardRemove())

# ==================== ШАГ 1: ФОТО ====================
@dp.message(PhotoForm.waiting_photo, F.photo)
async def process_photo(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    
    # Сохраняем фото
    await state.update_data(photo_id=photo.file_id)
    
    await message.answer(
        "✅ Фото получено!\n\n"
        "🕐 <b>Введи время</b> (например: 14:30) или нажми '⏭️ Пропустить':",
        reply_markup=get_skip_keyboard()
    )
    await state.set_state(PhotoForm.waiting_time)

@dp.message(PhotoForm.waiting_photo)
async def invalid_photo(message: types.Message):
    await message.answer("❌ Пожалуйста, отправь фото (изображение):")

# ==================== ШАГ 2: ВРЕМЯ ====================
@dp.message(PhotoForm.waiting_time)
async def process_time(message: types.Message, state: FSMContext):
    if message.text == "⏭️ Пропустить":
        time_value = None
    else:
        time_value = message.text.strip()
    
    await state.update_data(time=time_value)
    
    await message.answer(
        f"{'✅ Время: ' + time_value if time_value else '⏭️ Время пропущено'}\n\n"
        "📅 <b>Введи дату</b> (например: 17.09.2026) или нажми '⏭️ Пропустить':",
        reply_markup=get_skip_keyboard()
    )
    await state.set_state(PhotoForm.waiting_date)

# ==================== ШАГ 3: ДАТА ====================
@dp.message(PhotoForm.waiting_date)
async def process_date(message: types.Message, state: FSMContext):
    if message.text == "️ Пропустить":
        date_value = None
    else:
        date_value = message.text.strip()
    
    await state.update_data(date=date_value)
    
    await message.answer(
        f"{'✅ Дата: ' + date_value if date_value else '⏭️ Дата пропущена'}\n\n"
        "📍 <b>Введи место/станцию/перегон</b> (например: ст. Адлер, перегон Каяла-Пасюк) или нажми '⏭️ Пропустить':",
        reply_markup=get_skip_keyboard()
    )
    await state.set_state(PhotoForm.waiting_location)

# ==================== ШАГ 4: МЕСТО ====================
@dp.message(PhotoForm.waiting_location)
async def process_location(message: types.Message, state: FSMContext):
    if message.text == "⏭️ Пропустить":
        location = None
    else:
        location = message.text.strip()
    
    await state.update_data(location=location)
    
    await message.answer(
        f"{'✅ Место: ' + location if location else '⏭️ Место пропущено'}\n\n"
        "🚆 <b>Введи информацию о поезде/ПС</b> (например: ЭП20-001, поезд 104 Москва-Адлер) или нажми '⏭️ Пропустить':",
        reply_markup=get_skip_keyboard()
    )
    await state.set_state(PhotoForm.waiting_train_info)

# ==================== ШАГ 5: ИНФОРМАЦИЯ О ПОЕЗДЕ ====================
@dp.message(PhotoForm.waiting_train_info)
async def process_train_info(message: types.Message, state: FSMContext):
    if message.text == "⏭️ Пропустить":
        train_info = None
    else:
        train_info = message.text.strip()
    
    await state.update_data(train_info=train_info)
    
    # Получаем все данные
    data = await state.get_data()
    photo_id = data.get("photo_id")
    
    await message.answer("⏳ Анализирую и структурирую информацию...", reply_markup=types.ReplyKeyboardRemove())
    
    # Генерируем короткий ID
    short_id = str(uuid.uuid4())[:8]
    
    # Формируем текст для ИИ
    ai_input = f"""Фото железнодорожного транспорта.

Информация от автора:
- Время: {data.get('time') or 'не указано'}
- Дата: {data.get('date') or 'не указана'}
- Место: {data.get('location') or 'не указано'}
- Поезд/ПС: {data.get('train_info') or 'не указано'}

Проанализируй фото и создай структурированное описание:
1. Что изображено (тип, серия, номер если видно)
2. Качество фото (кратко)
3. Создай красивое описание для фотогалереи на основе предоставленной информации

Опиши кратко, на русском языке."""
    
    try:
        # Скачиваем и анализируем фото
        photo_file = await bot.download(photo_id)
        photo_bytes = photo_file.read()
        image = Image.open(io.BytesIO(photo_bytes))
        
        response = model.generate_content([ai_input, image])
        ai_description = response.text
        
    except Exception as e:
        logging.error(f"Ошибка анализа: {e}")
        ai_description = "⚠️ Анализ недоступен"
    
    # Формируем описание для публикации
    description_parts = []
    if data.get('time'):
        description_parts.append(f"🕐 Время: {data['time']}")
    if data.get('date'):
        description_parts.append(f" Дата: {data['date']}")
    if data.get('location'):
        description_parts.append(f"📍 Место: {data['location']}")
    if data.get('train_info'):
        description_parts.append(f"🚆 Поезд/ПС: {data['train_info']}")
    
    description = "\n".join(description_parts) if description_parts else "ℹ️ Информация не указана"
    
    # Сохраняем данные
    pending_photos[short_id] = {
        "photo_id": photo_id,
        "user_id": message.from_user.id,
        "username": message.from_user.username or message.from_user.first_name,
        "description": description,
        "ai_analysis": ai_description
    }
    
    # Отправляем пользователю
    await message.answer(
        "✅ <b>Информация принята!</b>\n\n"
        f"📝 <b>Описание:</b>\n{description}\n\n"
        f"🤖 <b>Анализ ИИ:</b>\n{ai_description}\n\n"
        " Отправлено на модерацию."
    )
    
    # Отправляем админам
    admin_ids = [ADMIN_CHAT_ID] if isinstance(ADMIN_CHAT_ID, str) else ADMIN_CHAT_ID
    for admin_id in admin_ids:
        try:
            caption = (
                f"📸 <b>Новое фото на модерацию</b>\n\n"
                f"👤 <b>Автор:</b> @{pending_photos[short_id]['username']}\n\n"
                f"📝 <b>Описание:</b>\n{description}\n\n"
                f" <b>Анализ ИИ:</b>\n{ai_description}"
            )
            
            await bot.send_photo(
                chat_id=admin_id,
                photo=photo_id,
                caption=caption,
                reply_markup=get_admin_keyboard(short_id)
            )
            logging.info(f"Фото отправлено админу {admin_id}")
        except Exception as e:
            logging.error(f"Ошибка отправки админу: {e}")
    
    await state.clear()

# ==================== ДЕЙСТВИЯ АДМИНОВ ====================
@dp.callback_query(F.data.startswith("approve:"))
async def approve_photo(callback: types.CallbackQuery):
    short_id = callback.data.split(":")[1]
    
    if short_id not in pending_photos:
        await callback.answer("️ Фото уже обработано", show_alert=True)
        return
    
    photo_data = pending_photos.pop(short_id)
    
    # Публикуем в канал (ТОЛЬКО автор и описание, без анализа ИИ)
    try:
        caption = (
            f"📸 <b>Фото от @{photo_data['username']}</b>\n\n"
            f"{photo_data['description']}"
        )
        
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=photo_data["photo_id"],
            caption=caption
        )
        logging.info(f"Фото опубликовано в канале {CHANNEL_ID}")
    except Exception as e:
        logging.error(f"Ошибка публикации: {e}")
    
    # Уведомляем главного админа
    if MAIN_ADMIN_ID:
        try:
            admin_username = f"@{callback.from_user.username}" if callback.from_user.username else f"ID:{callback.from_user.id}"
            await bot.send_message(
                MAIN_ADMIN_ID,
                f"✅ <b>Фото одобрено</b>\n\n"
                f"👤 <b>Автор:</b> @{photo_data['username']}\n"
                f"👮 <b>Админ:</b> {admin_username}\n"
                f"⏰ <b>Время:</b> {datetime.now().strftime('%H:%M')}",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Ошибка уведомления: {e}")
    
    # Уведомляем пользователя
    try:
        await bot.send_message(
            photo_data['user_id'],
            "✅ <b>Поздравляем!</b> Ваше фото опубликовано в канале!\n\n"
            "Спасибо за вклад! 🚂📸"
        )
    except:
        pass
    
    await callback.answer("✅ Фото опубликовано!", show_alert=True)

@dp.callback_query(F.data.startswith("reject:"))
async def reject_photo(callback: types.CallbackQuery):
    short_id = callback.data.split(":")[1]
    
    if short_id not in pending_photos:
        await callback.answer("⚠️ Фото уже обработано", show_alert=True)
        return
    
    photo_data = pending_photos.pop(short_id)
    
    try:
        await bot.send_message(
            photo_data['user_id'],
            "❌ <b>Фото отклонено</b>\n\n"
            "Попробуй отправить другое фото."
        )
    except:
        pass
    
    await callback.answer("❌ Фото отклонено", show_alert=True)

# ==================== WEBHOOK ====================
app = FastAPI()
WEBHOOK_URL = "https://skzd-photo-bot-ten.vercel.app/webhook"

@app.on_event("startup")
async def on_startup():
    import asyncio
    
    max_retries = 5
    for attempt in range(max_retries):
        try:
            webhook_info = await bot.get_webhook_info()
            if webhook_info.url != WEBHOOK_URL:
                await bot.set_webhook(url=WEBHOOK_URL, allowed_updates=dp.resolve_used_update_types())
                print(f"✅ Webhook установлен: {WEBHOOK_URL}")
            else:
                print(f"✅ Webhook уже установлен")
            return
        except Exception as e:
            if "Flood" in str(e):
                await asyncio.sleep((attempt + 1) * 2)
            else:
                print(f"❌ Ошибка: {e}")
                await asyncio.sleep(2)
    print("❌ Не удалось установить webhook")

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()

@app.post("/webhook")
async def webhook(request: Request):
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
async def root():
    return {"message": "Photo Bot is running! 📸"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
