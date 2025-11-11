import asyncio, datetime as dt, csv, os, shutil
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, FSInputFile
)
import aiosqlite

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is missing")

# Используем персистентную папку Railway
if os.path.exists('/tmp'):
    DB = '/tmp/gym.db'
    BACKUP_DIR = '/tmp/backups'
else:
    DB = "gym.db"
    BACKUP_DIR = "backups"

# ---------- СИСТЕМА БЭКАПОВ ----------
def ensure_backup_dir():
    """Создает папку для бэкапов если её нет"""
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)

async def create_backup():
    """Создает бэкап базы данных"""
    ensure_backup_dir()
    if not os.path.exists(DB):
        return None
    
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"gym_backup_{timestamp}.db")
    
    try:
        # Копируем файл базы данных
        shutil.copy2(DB, backup_path)
        
        # Удаляем старые бэкапы (оставляем последние 5)
        backup_files = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("gym_backup_")])
        for old_backup in backup_files[:-5]:  # Оставляем 5 последних
            os.remove(os.path.join(BACKUP_DIR, old_backup))
        
        return backup_path
    except Exception as e:
        print(f"Ошибка создания бэкапа: {e}")
        return None

async def auto_backup():
    """Автоматическое создание бэкапа при значимых изменениях"""
    await create_backup()

# ---------- СХЕМА БД ----------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS members(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE,
  trainings_total INTEGER DEFAULT 12,
  remaining INTEGER DEFAULT 12,
  last_visit_at TEXT,
  vacation INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS visits(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id INTEGER,
  dt TEXT,
  status TEXT
);
"""

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

async def ensure_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

# ---------- ХЕЛПЕРЫ ----------
async def get_all_members(db):
    async with db.execute(
        "SELECT id, name, remaining, trainings_total, vacation FROM members ORDER BY name"
    ) as c:
        return await c.fetchall()

async def get_member_by_id(db, member_id: int):
    async with db.execute(
        "SELECT id, name, remaining, trainings_total, vacation FROM members WHERE id=?", (member_id,)
    ) as c:
        return await c.fetchone()

async def change_visit(db, member_id: int, came: bool):
    now = dt.datetime.utcnow().isoformat()
    row = await get_member_by_id(db, member_id)
    if not row:
        return None
    _id, _name, remaining, total, vacation = row

    status = "came" if came else "missed"
    await db.execute("INSERT INTO visits(member_id, dt, status) VALUES(?,?,?)", (member_id, now, status))

    if came and not vacation:
        new_remaining = max(remaining - 1, 0)
        await db.execute(
            "UPDATE members SET remaining=?, last_visit_at=? WHERE id=?",
            (new_remaining, now, member_id),
        )

    await db.commit()
    
    # Автобэкап при посещении
    await auto_backup()
    
    return True

async def undo_last(db, member_id: int):
    async with db.execute(
        "SELECT id, status FROM visits WHERE member_id=? ORDER BY id DESC LIMIT 1", (member_id,)
    ) as c:
        last = await c.fetchone()
    if not last:
        return None, "Нет записей для отмены."
    visit_id, status = last

    _id, name, remaining, total, vacation = await get_member_by_id(db, member_id)

    if status == "came":
        new_remaining = min(remaining + 1, total)
        await db.execute("UPDATE members SET remaining=? WHERE id=?", (new_remaining, member_id))

    await db.execute("DELETE FROM visits WHERE id=?", (visit_id,))
    await db.commit()
    
    # Автобэкап при отмене
    await auto_backup()
    
    return name, None

async def renew_trainings(db, member_id: int, new_total=None):
    row = await get_member_by_id(db, member_id)
    if not row:
        return None
    _id, _name, _rem, total, _vac = row
    trainings = new_total if new_total is not None else total
    await db.execute(
        "UPDATE members SET trainings_total=?, remaining=? WHERE id=?",
        (trainings, trainings, member_id),
    )
    await db.commit()
    
    # Автобэкап при продлении
    await auto_backup()
    
    return trainings

# ---------- КЛАВИАТУРЫ ----------
def members_keyboard(members):
    rows = [[InlineKeyboardButton(text=name, callback_data=f"member_{member_id}")]
            for member_id, name, rem, total, vac in members]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def actions_keyboard(member_id: int, vacation: int):
    vac_mark = "🏖 выключить" if vacation else "🏖 отпуск"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Посетил(а)", callback_data=f"act_came_{member_id}")],
        [InlineKeyboardButton(text="❌ Пропустил(а)", callback_data=f"act_miss_{member_id}")],
        [InlineKeyboardButton(text="💰 Оплата", callback_data=f"act_renew_{member_id}")],
        [InlineKeyboardButton(text="🔄 Отменить последнее", callback_data=f"act_undo_{member_id}")],
        [InlineKeyboardButton(text=vac_mark, callback_data=f"act_vac_{member_id}")],
        [InlineKeyboardButton(text="⬅️ назад ко всем", callback_data="back_to_list")]
    ])

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
        "/export — выгрузить журнал посещений\n"
        "/backup — создать бэкап базы данных"
    )

@dp.message(Command("backup"))
async def cmd_backup(m: Message):
    """Создать и отправить бэкап базы"""
    try:
        await m.answer("🔄 Создаю бэкап...")
        
        # Просто отправляем текущую базу
        if os.path.exists(DB):
            await m.answer_document(
                FSInputFile(DB),
                caption=f"🔐 Бэкап базы {dt.datetime.now().strftime('%d.%m.%Y %H:%M')}"
            )
            await m.answer("✅ Бэкап успешно создан!")
        else:
            await m.answer("❌ Файл базы данных не найден")
            
    except Exception as e:
        await m.answer(f"❌ Ошибка: {str(e)}")

@dp.message(Command("add"))
async def add(m: Message):
    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer("Формат: /add Имя [кол-во тренировок]. Пример: /add Роман 12")
    name = parts[1]
    trainings = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 12
    await ensure_db()
    async with aiosqlite.connect(DB) as db:
        try:
            await db.execute(
                "INSERT INTO members(name, trainings_total, remaining) VALUES(?,?,?)",
                (name, trainings, trainings),
            )
            await db.commit()
            await m.answer(f"Добавлен {name}, {trainings} тренировок.")
            
            # Автобэкап при добавлении ученика
            await auto_backup()
            
        except Exception:
            await m.answer(f"{name} уже есть в списке.")

# ... остальные команды (visit, status, list, renew, export) остаются без изменений ...
# [ВСТАВЬТЕ СЮДА ВАШИ СТАРЫЕ КОМАНДЫ ИЗ ПРЕДЫДУЩЕГО КОДА]

# ---------- ЗАПУСК ----------
async def main():
    await ensure_db()
    # Создаем начальный бэкап при запуске
    await create_backup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
