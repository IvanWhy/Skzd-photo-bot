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
from fastapi import FastAPI, Request, Response
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

if not BOT_TOKEN: raise ValueError("BOT_TOKEN не найден!")
if not GEMINI_API_KEY: raise ValueError("GEMINI_API_KEY не найден!")
if not ADMIN_CHAT_ID: raise ValueError("ADMIN_CHAT_ID не найден!")
if not CHANNEL_ID: raise ValueError("CHANNEL_ID не найден!")

# Настройка Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3.6-flash') # или gemini-2.0-flash, если 3.6 недоступен

# Инициализация бота
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

# Хранилища
pending_photos = {}
pending_rejections = {}

# ==================== ПРОМПТ ДЛЯ ИИ (БЕЗ СМАЙЛИКОВ) ====================
AI_PROMPT = """Ты эксперт по городскому и железнодорожному транспорту, а также по фотографии. Проанализируй фото транспорта (трамвай, троллейбус, поезд, электровоз, тепловоз, МВПС и т.д.) и создай структурированное описание.

Входные данные от автора:
- Время: {time}
- Дата: {date}
- Место: {location}
- Транспорт/ПС: {train_info}

Твоя задача:
1. Определи тип транспортного средства (трамвай, троллейбус, электровоз, тепловоз, МВПС, вагон и т.д.)
2. Если видна серия и номер (бортовой или инвентарный) - укажи их
3. Оцени качество фотографии (резкость, освещение, композиция, баланс белого)
4. Создай краткое описание для фотогалереи (2-3 предложения)
Укажи недостатки фотографии (если они есть), по типу: неудачное освещение, неудачная композиция (предметы перекрывают транспорт), недоэкспонировано, неправильный баланс белого, неудачное кадрирование, чрезмерная обработка, посторонние предметы или люди в кадре, завал горизонта, смазано.

Формат ответа (СТРОГО БЕЗ СМАЙЛИКОВ И ЭМОДЗИ):
Тип: [тип транспорта]
Серия/Номер: [если видна]
Качество: [оценка 1-10 и краткий комментарий]
Описание: [краткое описание для публикации]

Отвечай на русском языке. Будь точен в определениях, расскажи обо всем как можно кратко."""

# ==================== СОСТОЯНИЯ (FSM) ====================
class PhotoForm(StatesGroup):
    waiting_photo = State()
    waiting_time = State()
    waiting_date = State()
    waiting_location = State()
    waiting_train_info = State()

class RejectForm(StatesGroup):
    waiting_reason = State()

# ==================== КЛАВИАТУРЫ ====================
def get_admin_keyboard(short_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Одобрить", callback_data=f"approve:{short_id}"),
            InlineKeyboardButton(text="Отклонить", callback_data=f"reject:{short_id}")
        ]
    ])

def get_skip_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Пропустить")]
    ], resize_keyboard=True)

def is_skip(text: str) -> bool:
    if not text:
        return True
    text_lower = text.lower().strip()
    return 'пропуст' in text_lower or text_lower == 'нет' or text_lower == '-'

# ==================== КОМАНДЫ ====================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.set_state(PhotoForm.waiting_photo)
    await message.answer(
        "Привет! Я бот-предложка для фотогалереи городского и железнодорожного транспорта.\n\n"
        "Как это работает:\n"
        "1. Отправь мне фото трамвая, троллейбуса или поезда\n"
        "2. Укажи время, дату, место и информацию о транспорте\n"
        "3. Админы рассмотрят и опубликуют в канале\n\n"
        "Отправь фото для начала:"
    )

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Заполнение отменено. Напиши /start", reply_markup=types.ReplyKeyboardRemove())

# ==================== ШАГ 1: ФОТО ====================
@dp.message(PhotoForm.waiting_photo, F.photo)
async def process_photo(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    await state.update_data(photo_id=photo.file_id)
    
    await message.answer(
        "Фото получено!\n\n"
        "Введи время (например: 14:30) или нажми 'Пропустить':",
        reply_markup=get_skip_keyboard()
    )
    await state.set_state(PhotoForm.waiting_time)

@dp.message(PhotoForm.waiting_photo)
async def invalid_photo(message: types.Message):
    await message.answer("Пожалуйста, отправь фото (изображение):")

# ==================== ШАГ 2: ВРЕМЯ ====================
@dp.message(PhotoForm.waiting_time)
async def process_time(message: types.Message, state: FSMContext):
    if is_skip(message.text):
        time_value = None
        time_display = "Пропущено"
    else:
        time_value = message.text.strip()
        time_display = time_value
    
    await state.update_data(time=time_value)
    await message.answer(
        f"Время: {time_display}\n\n"
        "Введи дату (например: 17.09.2026) или нажми 'Пропустить':",
        reply_markup=get_skip_keyboard()
    )
    await state.set_state(PhotoForm.waiting_date)

# ==================== ШАГ 3: ДАТА ====================
@dp.message(PhotoForm.waiting_date)
async def process_date(message: types.Message, state: FSMContext):
    if is_skip(message.text):
        date_value = None
        date_display = "Пропущено"
    else:
        date_value = message.text.strip()
        date_display = date_value
    
    await state.update_data(date=date_value)
    await message.answer(
        f"Дата: {date_display}\n\n"
        "Введи место/остановку/станцию/перегон (например: ост. Ленина, ст. Адлер) или нажми 'Пропустить':",
        reply_markup=get_skip_keyboard()
    )
    await state.set_state(PhotoForm.waiting_location)

# ==================== ШАГ 4: МЕСТО ====================
@dp.message(PhotoForm.waiting_location)
async def process_location(message: types.Message, state: FSMContext):
    if is_skip(message.text):
        location = None
        location_display = "Пропущено"
    else:
        location = message.text.strip()
        location_display = location
    
    await state.update_data(location=location)
    await message.answer(
        f"Место: {location_display}\n\n"
        "Введи информацию о транспорте (например: трамвай 71-623, троллейбус 1234, поезд ЭП20-001) или нажми 'Пропустить':",
        reply_markup=get_skip_keyboard()
    )
    await state.set_state(PhotoForm.waiting_train_info)

# ==================== ШАГ 5: ИНФОРМАЦИЯ О ТРАНСПОРТЕ ====================
@dp.message(PhotoForm.waiting_train_info)
async def process_train_info(message: types.Message, state: FSMContext):
    if is_skip(message.text):
        train_info = None
        train_display = "Пропущено"
    else:
        train_info = message.text.strip()
        train_display = train_info
    
    await state.update_data(train_info=train_info)
    data = await state.get_data()
    photo_id = data.get("photo_id")
    
    await message.answer("Анализирую и структурирую информацию...", reply_markup=types.ReplyKeyboardRemove())
    
    short_id = str(uuid.uuid4())[:8]
    
    ai_input = AI_PROMPT.format(
        time=data.get('time') or 'не указано',
        date=data.get('date') or 'не указана',
        location=data.get('location') or 'не указано',
        train_info=data.get('train_info') or 'не указано'
    )
    
    try:
        photo_file = await bot.download(photo_id)
        photo_bytes = photo_file.read()
        image = Image.open(io.BytesIO(photo_bytes))
        response = model.generate_content([ai_input, image])
        ai_description = response.text
    except Exception as e:
        logging.error(f"Ошибка анализа: {e}")
        ai_description = "Анализ недоступен"
    
    # Формируем описание БЕЗ смайликов
    description_parts = []
    if data.get('time'):
        description_parts.append(f"Время: {data['time']}")
    if data.get('date'):
        description_parts.append(f"Дата: {data['date']}")
    if data.get('location'):
        description_parts.append(f"Место: {data['location']}")
    if data.get('train_info'):
        description_parts.append(f"Транспорт: {data['train_info']}")
    
    description = "\n".join(description_parts) if description_parts else "Информация не указана"
    
    pending_photos[short_id] = {
        "photo_id": photo_id,
        "user_id": message.from_user.id,
        "username": message.from_user.username or message.from_user.first_name,
        "description": description,
        "ai_analysis": ai_description
    }
    
    await message.answer(
        "Информация принята!\n\n"
        f"Описание:\n{description}\n\n"
        "Отправлено на модерацию."
    )
    
    # Отправляем админам (без смайликов)
    admin_ids = [ADMIN_CHAT_ID] if isinstance(ADMIN_CHAT_ID, str) else ADMIN_CHAT_ID
    for admin_id in admin_ids:
        try:
            short_caption = (
                f"Новое фото на модерацию\n\n"
                f"Автор: @{pending_photos[short_id]['username']}\n\n"
                f"Описание:\n{description}"
            )
            
            photo_message = await bot.send_photo(
                chat_id=admin_id,
                photo=photo_id,
                caption=short_caption,
                reply_markup=get_admin_keyboard(short_id)
            )
            
            await bot.send_message(
                chat_id=admin_id,
                text=f"Анализ ИИ:\n{ai_description}",
                reply_to_message_id=photo_message.message_id
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
        await callback.answer("Фото уже обработано", show_alert=True)
        return
    
    photo_data = pending_photos.pop(short_id)
    
    try:
        caption = (
            f"Фото от @{photo_data['username']}\n\n"
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
    
    if MAIN_ADMIN_ID:
        try:
            admin_username = f"@{callback.from_user.username}" if callback.from_user.username else f"ID:{callback.from_user.id}"
            await bot.send_message(
                MAIN_ADMIN_ID,
                f"Фото одобрено\n\n"
                f"Автор: @{photo_data['username']}\n"
                f"Админ: {admin_username}\n"
                f"Время: {datetime.now().strftime('%H:%M')}",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Ошибка уведомления: {e}")
    
    try:
        await bot.send_message(
            photo_data['user_id'],
            "Поздравляем! Ваше фото опубликовано в канале!\n\nСпасибо за вклад!"
        )
    except:
        pass
    
    await callback.answer("Фото опубликовано!", show_alert=True)

@dp.callback_query(F.data.startswith("reject:"))
async def start_reject(callback: types.CallbackQuery, state: FSMContext):
    short_id = callback.data.split(":")[1]
    
    if short_id not in pending_photos:
        await callback.answer("Фото уже обработано", show_alert=True)
        return
    
    pending_rejections[short_id] = {
        "photo_data": pending_photos[short_id],
        "admin_id": callback.from_user.id,
        "admin_username": callback.from_user.username or callback.from_user.first_name
    }
    
    await callback.message.answer(
        "Напишите причину отклонения:\n\n"
        "Например: Низкое качество фото, не видно номер, дубликат\n\n"
        "Или нажмите 'Без причины' для отклонения без указания причины:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Без причины", callback_data=f"reject_no_reason:{short_id}")]
        ])
    )
    
    await state.set_state(RejectForm.waiting_reason)
    await callback.answer()

@dp.callback_query(F.data.startswith("reject_no_reason:"))
async def reject_no_reason(callback: types.CallbackQuery, state: FSMContext):
    short_id = callback.data.split(":")[1]
    
    if short_id not in pending_rejections:
        await callback.answer("Данные не найдены", show_alert=True)
        return
    
    await process_rejection(short_id, "Причина не указана", callback.from_user.id, state)
    await callback.answer("Фото отклонено", show_alert=True)

@dp.message(RejectForm.waiting_reason)
async def process_reject_reason(message: types.Message, state: FSMContext):
    short_id = None
    for sid, data in pending_rejections.items():
        if data["admin_id"] == message.from_user.id:
            short_id = sid
            break
    
    if not short_id:
        await message.answer("Нет активных отклонений. Напишите /cancel")
        await state.clear()
        return
    
    reason = message.text.strip()
    await process_rejection(short_id, reason, message.from_user.id, state)
    await message.answer(f"Фото отклонено с причиной: {reason}")

async def process_rejection(short_id: str, reason: str, admin_id: int, state: FSMContext):
    if short_id not in pending_rejections:
        return
    
    rejection_data = pending_rejections.pop(short_id)
    photo_data = rejection_data["photo_data"]
    pending_photos.pop(short_id, None)
    
    try:
        await bot.send_message(
            photo_data['user_id'],
            f"Фото отклонено\n\n"
            f"Причина: {reason}\n\n"
            "Попробуйте отправить другое фото с лучшим качеством."
        )
    except:
        pass
    
    if MAIN_ADMIN_ID:
        try:
            admin_username = rejection_data["admin_username"]
            await bot.send_message(
                MAIN_ADMIN_ID,
                f"Фото отклонено\n\n"
                f"Автор: @{photo_data['username']}\n"
                f"Отклонил: @{admin_username}\n"
                f"Причина: {reason}\n"
                f"Время: {datetime.now().strftime('%H:%M')}",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Ошибка уведомления: {e}")
    
    await state.clear()

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
                print(f"Webhook установлен: {WEBHOOK_URL}")
            else:
                print(f"Webhook уже установлен")
            return
        except Exception as e:
            if "Flood" in str(e):
                await asyncio.sleep((attempt + 1) * 2)
            else:
                print(f"Ошибка: {e}")
                await asyncio.sleep(2)
    print("Не удалось установить webhook")

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
async def root_get():
    return {"status": "ok"}

@app.head("/")
async def root_head():
    return Response(status_code=200, headers={"Content-Type": "application/json"})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
