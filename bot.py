import os
import asyncio
import re
import logging
from functools import wraps

import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.types.callback_query import CallbackQuery
from datetime import datetime


logging.basicConfig(level=logging.DEBUG,
                    format="%(asctime)s - %(levelname)s - %(message)s")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL",
                         "postgresql://myuser:mypassword@localhost:5432/mydb")

bot = Bot(token=TOKEN,
          default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


async def init_db():
    """Створює пул з'єднань до PostgreSQL."""
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    dp["db_pool"] = db_pool
    logging.info("База даних успішно підключена.")


async def is_curator(user_id: int) -> bool:
    """Перевіряє чи є користувач у таблиці """
    db_pool = dp.get("db_pool")
    if not db_pool:
        raise Exception("Немає з'єднання з БД")

    async with db_pool.acquire() as conn:
        curator_exists = await conn.fetchval(
            "SELECT 1 FROM curators WHERE user_id = $1", user_id
        )
        # Якщо користувача знайдено в таблиці curators,
        # повертаємо True, інакше False
        return curator_exists is not None


def curator_only(handler):
    """Декоратор для перевірки, чи є користувач куратором."""
    @wraps(handler)
    async def wrapper(callback_query: CallbackQuery, *args, **kwargs):
        user_id = callback_query.from_user.id
        if not await is_curator(user_id):
            await callback_query.answer("❌ Ви не є куратором цього чату!",
                                        show_alert=True)
            return
        return await handler(callback_query, *args, **kwargs)
    return wrapper


async def update_status(callback_query: CallbackQuery,
                        new_status: str,
                        action: str):
    """Оновлює статус заявки в чаті та записує в БД."""
    db_pool = dp.get("db_pool")
    if not db_pool:
        await callback_query.answer("⚠ Помилка сервера: немає з'єднання з БД")
        return

    request_id = int(callback_query.data.split("_")[1])

    async with db_pool.acquire() as conn:
        curator_name_row = await conn.fetchrow(
            "SELECT username FROM curators WHERE user_id = $1",
            callback_query.from_user.id)

    curator_name = curator_name_row["username"]

    current_time = datetime.now().strftime('%H:%M:%S')

    # Оновлюємо текст повідомлення
    old_text = callback_query.message.text or callback_query.message.caption
    new_text = re.sub(
        r"Статус:.*",
        f"Статус: {new_status}\nКуратор: {curator_name}\nВремя: {current_time}",
        old_text,
        flags=re.DOTALL
    )

    # Редагуємо текст повідомлення
    await bot.edit_message_text(
        chat_id=callback_query.message.chat.id,
        message_id=callback_query.message.message_id,
        text=new_text,
        reply_markup=callback_query.message.reply_markup
    )

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO curator_logs (request_id, curator_id, action, timestamp) VALUES ($1, $2, $3, NOW())",
            request_id, callback_query.from_user.id, action
        )

    await callback_query.answer(f"Ви змінили статус на: {new_status}")


@dp.message(Command("start"))
async def start(message: types.Message):
    """Команда /start."""
    await message.answer("Привіт! Надішли текст, і я створю гілку обговорення.")


@dp.message()
async def handle_message(message: types.Message):
    """Обробляє вхідні повідомлення і створює гілку обговорення."""
    if message.from_user.is_bot: # Щоб не слухав свої повідомлення
        return

    if message.text is None:
        return

    if message.text.startswith("\u200b") or message.text.strip() == "":
        return

    db_pool = dp.get("db_pool")
    if not db_pool:
        return await message.answer("⚠ Помилка сервера: немає з'єднання з БД")


    # Перевірка, якщо це повідомлення з головної гілки
    if message.message_thread_id is None:
        # Це повідомлення з головної гілки, створюємо новий запит
        last_message = await db_pool.fetchval(
            "SELECT message FROM curator_messages WHERE sender_id=$1 ORDER BY timestamp DESC LIMIT 1",
            message.from_user.id
        )

        # Створення гілки
        forum_topic = await bot.create_forum_topic(
            chat_id=message.chat.id,
            name=f"Запит від {message.from_user.full_name}"
        )

        text_forum = (
            f"<b>Новий запит!</b>\n\n"
            f"<b>Текст:</b> {message.text}\n"
            f"<b>Час:</b> {datetime.now().strftime('%H:%M:%S')}\n"
            f"<b>Статус:</b> ⏳ Очікує обробки"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Взяти в роботу",
                        callback_data=f"take_{forum_topic.message_thread_id}"),
                    InlineKeyboardButton(
                        text="⏸ Поставити на утримання",
                        callback_data=f"hold_{forum_topic.message_thread_id}")
                ],
                [
                    InlineKeyboardButton(
                        text="🔄 Переназначити куратора",
                        callback_data=f"reassign_{forum_topic.message_thread_id}"),
                    InlineKeyboardButton(
                        text="❌ Завершити діалог",
                        callback_data=f"close_{forum_topic.message_thread_id}")
                ]
            ]
        )

        sent_message = await bot.send_message(
            chat_id=message.chat.id,
            message_thread_id=forum_topic.message_thread_id,
            text=text_forum,
            reply_markup=keyboard
        )

        # Запис у БД
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO curator_messages (request_id, sender_id, sender_role, message) VALUES ($1, $2, 'student', $3)",
                forum_topic.message_thread_id,
                message.from_user.id,
                message.text
            )

        await message.answer("✅ Ваш запит надіслано кураторам.")
    else:
        # Тут обробляються повідомлення з другорядних гілок

        # Логування повідомлень з другорядних гілок у БД
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO curator_messages (request_id, sender_id, sender_role, message) VALUES ($1, $2, $3, $4)",
                message.message_thread_id,
                message.from_user.id,
                "curator" if await is_curator(message.from_user.id) else "student",
                message.text
            )


@dp.callback_query(lambda c: c.data.startswith("take_"))
@curator_only
async def take_request(callback_query: CallbackQuery):
    """Куратор бере запит у роботу."""
    db_pool = dp.get("db_pool")
    if not db_pool:
        return await callback_query.answer(
            "⚠ Помилка сервера: немає з'єднання з БД"
        )

    request_id = int(callback_query.data.split("_")[1])
    curator_id = callback_query.from_user.id
    current_time = datetime.now()

    async with db_pool.acquire() as conn:
        # Перевіряємо, чи був уже призначений куратор для цього запиту
        assigned_curator_id = await conn.fetchval(
            "SELECT curator_id FROM curator_logs WHERE request_id = $1 AND action = 'take' ORDER BY timestamp DESC LIMIT 1",
            request_id
        )

        if assigned_curator_id != curator_id and assigned_curator_id is not None:
            # Якщо куратор уже був призначений, повертаємо повідомлення про це
            return await callback_query.answer(
                "❌ Цей запит уже взяв у роботу інший куратор.",
                show_alert=True
            )

        created_at = await conn.fetchval(
            "SELECT timestamp FROM curator_messages WHERE request_id=$1 ORDER BY timestamp ASC LIMIT 1",
            request_id
        )

        response_time = (current_time - created_at).seconds if created_at else "N/A"

    await update_status(callback_query,
                        f"🟢 В роботі\n⏱ Час реакції: {response_time} сек.",
                        "take")


@dp.callback_query(lambda c: c.data.startswith("hold_"))
@curator_only
async def hold_request(callback_query: CallbackQuery):
    """Куратор ставить заявку на утримання."""
    db_pool = dp.get("db_pool")
    if not db_pool:
        return await callback_query.answer(
            "⚠ Помилка сервера: немає з'єднання з БД"
        )

    request_id = int(callback_query.data.split("_")[1])
    curator_id = callback_query.from_user.id

    async with db_pool.acquire() as conn:
        # Отримуємо ID куратора, який прийняв запит
        assigned_curator_id = await conn.fetchval(
            """
            SELECT curator_id FROM curator_logs
            WHERE request_id = $1
            ORDER BY timestamp DESC LIMIT 1
            """,
            request_id
        )

    if curator_id != assigned_curator_id:
        return await callback_query.answer(
            "❌ Ви не можете утримати цей запит, оскільки його прийняв інший куратор.",
            show_alert=True
        )

    await update_status(callback_query,
                        "🟡 Утримано",
                        "hold")

    # Відправляємо повідомлення в особисті (якщо у нього був з ним діалог)
    await bot.send_message(
        callback_query.from_user.id,
        "Запит поставлено на утримання, очікуйте подальших інструкцій."
    )


@dp.callback_query(lambda c: c.data.startswith("close_"))
@curator_only
async def close_request(callback_query: CallbackQuery):
    """Куратор закриває запит, якщо він його прийняв."""
    db_pool = dp.get("db_pool")
    if not db_pool:
        return await callback_query.answer(
            "⚠ Помилка сервера: немає з'єднання з БД")

    request_id = int(callback_query.data.split("_")[1])
    curator_id = callback_query.from_user.id

    async with db_pool.acquire() as conn:
        # Отримуємо ID куратора, який прийняв запит
        assigned_curator_id = await conn.fetchval(
            """
            SELECT curator_id FROM curator_logs
            WHERE request_id = $1
            ORDER BY timestamp DESC LIMIT 1
            """,
            request_id
        )

    if curator_id != assigned_curator_id:
        return await callback_query.answer(
            "❌ Ви не можете закрити цей запит, оскільки його прийняв інший куратор.",
            show_alert=True
        )

    await update_status(callback_query,
                        "❌ Закрито",
                        "close")

    # Закриваємо гілку форуму
    try:
        await bot.close_forum_topic(
            chat_id=callback_query.message.chat.id,
            message_thread_id=request_id
        )
        await callback_query.answer("Гілку обговорення успішно закрито.")
    except Exception as e:
        await callback_query.answer("Сталася помилка під час закриття гілки.")
        logging.error(f"Помилка під час закриття гілки: {e}")

    # Надсилання повідомлення про закриття гілки
    await callback_query.message.answer(
        "Гілку обговорення закрито куратором."
    )


@dp.callback_query(
    lambda c: c.data.startswith("reassign_") and not c.data.startswith("reassign_to_")
)
@curator_only
async def reassign_request(callback_query: CallbackQuery):
    """Куратор перепризначає запит на іншого куратора."""
    db_pool = dp.get("db_pool")
    if not db_pool:
        return await callback_query.answer(
            "⚠ Помилка сервера: немає з'єднання з БД")

    async with db_pool.acquire() as conn:
        # Отримуємо ID куратора, який прийняв запит
        assigned_curator_id = await conn.fetchval(
            """
            SELECT curator_id FROM curator_logs
            WHERE request_id = $1
            ORDER BY timestamp DESC LIMIT 1
            """,
            int(callback_query.data.split("_")[1])
        )

    if callback_query.from_user.id != assigned_curator_id:
        return await callback_query.answer(
            "❌ Ви не можете перепризначити цей запит, оскільки його прийняв інший куратор.",
            show_alert=True
        )

    try:
        data_parts = callback_query.data.split("_")
        if len(data_parts) != 2:  # Тільки два елементи для reassign_{request_id}
            return await callback_query.answer(
                "⚠ Помилка: Неправильний формат даних."
            )
        request_id = int(data_parts[1])
    except (IndexError, ValueError):
        return await callback_query.answer(
            "⚠ Помилка: Неправильний формат даних."
        )

    # Отримуємо список усіх кураторів із таблиці curators
    async with db_pool.acquire() as conn:
        curators = await conn.fetch("SELECT user_id, username FROM curators")

    # Створюємо кнопки для кожного куратора, використовуючи username як текст
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=str(curator['username']),
                    callback_data=f"reassign_to_{curator['user_id']}_{request_id}"
                )
            ]
            for curator in curators
        ]
    )

    # Надсилаємо повідомлення з вибором куратора
    await callback_query.message.edit_text(
        "Виберіть нового куратора для перепризначення запиту:",
        reply_markup=keyboard
    )


@dp.callback_query(lambda c: c.data.startswith("reassign_to_"))
@curator_only
async def handle_reassign(callback_query: CallbackQuery):
    """Перепризначаємо запит на обраного куратора."""
    db_pool = dp.get("db_pool")
    if not db_pool:
        return await callback_query.answer(
            "⚠ Помилка сервера: немає з'єднання з БД")

    try:
        _, __, new_curator_id, request_id = callback_query.data.split("_")
        new_curator_id = int(new_curator_id)
        request_id = int(request_id)
    except (ValueError, IndexError):
        return await callback_query.answer(
            "⚠ Помилка: Неправильний формат даних."
        )

    # Отримуємо інформацію про нового куратора
    async with db_pool.acquire() as conn:
        new_curator_row = await conn.fetchrow(
            "SELECT username FROM curators WHERE user_id = $1",
            new_curator_id)

    new_curator_name = new_curator_row['username']

    if not new_curator_name:
        return await callback_query.answer("⚠ Помилка: Куратора не знайдено.")

    # Створюємо нове повідомлення з оновленим куратором
    text = (
        f"<b>Запит перепризначено!</b>\n\n"
        f"<b>Статус:</b> 🟢 В роботі\n"
        f"<b>Куратор:</b> {new_curator_name}\n"
        f"<b>Час:</b> {datetime.now().strftime('%H:%M:%S')}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Взяти в роботу",
                                     callback_data=f"take_{request_id}"),
                InlineKeyboardButton(text="⏸ Поставити на утримання",
                                     callback_data=f"hold_{request_id}")
            ],
            [
                InlineKeyboardButton(text="🔄 Переназначити куратора",
                                     callback_data=f"reassign_{request_id}"),
                InlineKeyboardButton(text="❌ Завершити діалог",
                                     callback_data=f"close_{request_id}")
            ]
        ]
    )

    # Оновлюємо повідомлення
    await bot.edit_message_text(
        chat_id=callback_query.message.chat.id,
        message_id=callback_query.message.message_id,
        text=text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    # Перепризначаємо нового куратора запиту
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE curator_logs
            SET curator_id = $1, timestamp = NOW()
            WHERE request_id = $2 AND action = 'take'
            """,
            new_curator_id, request_id
        )

    await callback_query.answer("✅ Запит успішно перепризначено!")


async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
