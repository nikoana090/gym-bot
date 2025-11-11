import asyncio, datetime as dt, csv, os
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, FSInputFile
)
import psycopg2
from psycopg2.extras import RealDictCursor
import threading

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is missing")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var is missing")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------- ПОДКЛЮЧЕНИЕ К БАЗЕ ----------
def get_connection():
    return psycopg2.connect(DATABASE_URL)

def execute_query(query, params=None, fetch=False):
    """Универсальная функция для выполнения запросов"""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            if fetch:
                return cur.fetchall()
            elif query.strip().upper().startswith('SELECT'):
                return cur.fetchone()
            conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# ---------- СХЕМА БД ----------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS members(
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE,
    trainings_total INTEGER DEFAULT 12,
    remaining INTEGER DEFAULT 12,
    last_visit_at TIMESTAMP,
    vacation BOOLEAN DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS visits(
    id SERIAL PRIMARY KEY,
    member_id INTEGER REFERENCES members(id),
    dt TIMESTAMP DEFAULT NOW(),
    status TEXT
);
"""

async def ensure_db():
    """Создает таблицы если их нет"""
    execute_query(CREATE_SQL)

# ---------- ХЕЛПЕРЫ ----------
async def get_all_members():
    result = execute_query(
        "SELECT id, name, remaining, trainings_total, vacation FROM members ORDER BY name",
        fetch=True
    )
    return result

async def get_member_by_id(member_id: int):
    return execute_query(
        "SELECT id, name, remaining, trainings_total, vacation FROM members WHERE id=%s", 
        (member_id,)
    )

async def change_visit(member_id: int, came: bool):
    """Записать посещение"""
    member = await get_member_by_id(member_id)
    if not member:
        return None

    status = "came" if came else "missed"
    execute_query(
        "INSERT INTO visits(member_id, status) VALUES(%s, %s)", 
        (member_id, status)
    )

    if came and not member['vacation']:
        new_remaining = max(member['remaining'] - 1, 0)
        execute_query(
            "UPDATE members SET remaining=%s, last_visit_at=NOW() WHERE id=%s",
            (new_remaining, member_id)
        )
    
    return True

# ... остальные функции адаптируем аналогично ...

# ---------- КОМАНДЫ ----------
@dp.message(Command("start"))
async def start(m: Message):
    await ensure_db()
    await m.answer(
        "Привет! Я отмечаю посещения и тренировки 💪\n\n"
        "Команды:\n"
        "/add Имя [кол-во тренировок]\n"
        "/visit — отметить посещение (кнопки)\n"
        "/status Имя — остаток\n"
        "/list — список всех\n"
        "/renew Имя [кол-во] — продлить тренировки\n"
        "/export — выгрузить журнал посещений"
    )

@dp.message(Command("add"))
async def add(m: Message):
    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer("Формат: /add Имя [кол-во тренировок]. Пример: /add Роман 12")
    
    name = parts[1]
    trainings = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 12
    
    try:
        execute_query(
            "INSERT INTO members(name, trainings_total, remaining) VALUES(%s, %s, %s)",
            (name, trainings, trainings)
        )
        await m.answer(f"Добавлен {name}, {trainings} тренировок.")
    except psycopg2.IntegrityError:
        await m.answer(f"{name} уже есть в списке.")

# ... остальные команды ...

# ---------- ЗАПУСК ----------
async def main():
    await ensure_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
