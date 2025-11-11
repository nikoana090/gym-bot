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
else:
    DB = "gym.db"

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
        [InlineKeyboardButton(text="✏️ Изменить пакет", callback_data=f"act_edit_{member_id}")],
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
        "/edit Имя [кол-во] — изменить пакет\n"
        "/export — выгрузить журнал посещений\n"
        "/backup — создать бэкап базы данных"
    )

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
        except Exception:
            await m.answer(f"{name} уже есть в списке.")

@dp.message(Command("visit"))
async def visit(m: Message):
    await ensure_db()
    async with aiosqlite.connect(DB) as db:
        members = await get_all_members(db)
    if not members:
        return await m.answer("Пока нет учеников. Добавьте: /add Имя 12")
    await m.answer("Кого отмечаем сегодня?", reply_markup=members_keyboard(members))

@dp.message(Command("list"))
async def cmd_list(m: Message):
    await ensure_db()
    async with aiosqlite.connect(DB) as db:
        members = await get_all_members(db)
    if not members:
        return await m.answer("Список пуст. /add Имя 12")
    def line(name, rem, total, vac):
        tail = " 🏖" if vac else ""
        return f"{name} — {rem}/{total}{tail}"
    lines = [line(name, rem, total, vac) for _id, name, rem, total, vac in members]
    await m.answer("Список учеников:\n" + "\n".join(lines))

@dp.message(Command("status"))
async def status(m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer("Формат: /status Имя")
    name = parts[1]
    await ensure_db()
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT remaining, trainings_total, vacation FROM members WHERE name=?", (name,)
        ) as c:
            row = await c.fetchone()
    if not row:
        return await m.answer("Ученика не нашёл. /add Имя 12")
    remaining, total, vacation = row
    vac = " (🏖 отпуск)" if vacation else ""
    await m.answer(f"{name}: осталось {remaining} из {total} тренировок{vac}")

@dp.message(Command("renew"))
async def cmd_renew(m: Message):
    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer("Формат: /renew Имя [кол-во тренировок]")
    name = parts[1]
    trainings = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
    await ensure_db()
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT id FROM members WHERE name=?", (name,)) as c:
            row = await c.fetchone()
        if not row:
            return await m.answer("Такого ученика нет. /add Имя [кол-во]")
        member_id = row[0]
        new_total = await renew_trainings(db, member_id, trainings)
        await m.answer(f"🔁 Продлены тренировки: {name} — {new_total} занятий.")

@dp.message(Command("edit"))
async def cmd_edit(m: Message):
    """Изменить данные ученика"""
    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer(
            "Формат: /edit Имя [новое_кол-во_тренировок]\n"
            "Примеры:\n"
            "/edit Роман 20 - изменить пакет на 20 тренировок\n"
            "/edit Роман - показать текущие данные"
        )
    
    name = parts[1]
    new_trainings = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
    
    await ensure_db()
    async with aiosqlite.connect(DB) as db:
        # Получаем текущие данные ученика
        async with db.execute(
            "SELECT id, name, remaining, trainings_total FROM members WHERE name=?", (name,)
        ) as c:
            row = await c.fetchone()
        
        if not row:
            return await m.answer(f"❌ Ученик '{name}' не найден")
        
        member_id, current_name, current_remaining, current_total = row
        
        if new_trainings is not None:
            # Обновляем общее количество тренировок
            await db.execute(
                "UPDATE members SET trainings_total=?, remaining=? WHERE id=?",
                (new_trainings, new_trainings, member_id)
            )
            await db.commit()
            await m.answer(
                f"✅ Обновлено: {name}\n"
                f"📊 Было: {current_total} тренировок\n"
                f"📊 Стало: {new_trainings} тренировок\n"
                f"💫 Остаток обновлен до: {new_trainings}"
            )
        else:
            # Показываем текущие данные
            await m.answer(
                f"📊 {name}:\n"
                f"• Всего тренировок: {current_total}\n"
                f"• Осталось: {current_remaining}\n"
                f"• Использовано: {current_total - current_remaining}\n\n"
                f"Чтобы изменить: /edit {name} [новое_число]"
            )

@dp.message(Command("export"))
async def cmd_export(m: Message):
    await ensure_db()
    async with aiosqlite.connect(DB) as db:
        async with db.execute("""
            SELECT members.name, visits.dt, visits.status
            FROM visits
            JOIN members ON members.id = visits.member_id
            ORDER BY visits.dt DESC
        """) as c:
            rows = await c.fetchall()
    path = "visits.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Имя", "Дата (UTC)", "Статус"])
        for name, dt_iso, status in rows:
            writer.writerow([name, dt_iso, "Посетил(а)" if status=="came" else "Пропустил(а)"])
    await m.answer_document(FSInputFile(path), caption="Экспорт журнала посещений")

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

# ---------- ОБРАБОТЧИКИ КНОПОК ----------
@dp.callback_query(lambda c: c.data.startswith(("member_", "act_", "back_to_list")))
async def handle_member_and_actions(cb: CallbackQuery):
    await ensure_db()
    async with aiosqlite.connect(DB) as db:
        # Назад к списку
        if cb.data == "back_to_list":
            members = await get_all_members(db)
            return await cb.message.edit_text("Кого отмечаем сегодня?", reply_markup=members_keyboard(members))

        # Открыли подменю по ученику
        if cb.data.startswith("member_"):
            member_id = int(cb.data.split("_", 1)[1])
            row = await get_member_by_id(db, member_id)
            if not row:
                return await cb.answer("Не нашёл ученика", show_alert=True)
            _id, name, rem, total, vac = row
            text = f"Выбран: {name} — {rem}/{total} тренировок" + (" 🏖" if vac else "")
            return await cb.message.edit_text(text, reply_markup=actions_keyboard(member_id, vac))

        # Действия из подменю
        if cb.data.startswith("act_"):
            _, action, member_id_s = cb.data.split("_", 2)
            member_id = int(member_id_s)

            row = await get_member_by_id(db, member_id)
            if not row:
                return await cb.answer("Не нашёл ученика", show_alert=True)
            _id, name, rem, total, vac = row

            if action in ("came", "miss"):
                came = action == "came"
                await change_visit(db, member_id, came)
                _id, name, rem, total, vac = await get_member_by_id(db, member_id)
                msg = f"{'✅ Посетил(а)' if came else '❌ Пропустил(а)'}: {name}. Осталось {rem}/{total}"
                if came and not vac and rem in (2, 1):
                    msg += f"\n⚠️ Осталось {rem} {'тренировка' if rem==1 else 'тренировки'}!"
                if came and not vac and rem == 0:
                    msg += "\n⛔ Тренировки закончились!"
                if vac:
                    msg += "\n🏖 В отпуске - тренировки не списаны."
                await cb.message.answer(msg)

            elif action == "renew":
                await renew_trainings(db, member_id, None)
                _id, name, rem, total, vac = await get_member_by_id(db, member_id)
                await cb.message.answer(f"💰 Продлены тренировки: {name} — {total} занятий.")

            elif action == "edit":
                await cb.message.answer(
                    f"✏️ Редактирование: {name}\n"
                    f"Текущий пакет: {total} тренировок\n\n"
                    f"Отправьте команду:\n"
                    f"/edit {name} [новое_число]"
                )

            elif action == "undo":
                name2, err = await undo_last(db, member_id)
                if err:
                    await cb.message.answer(f"🔄 {err}")
                else:
                    _id, _nm, rem, total, vac = await get_member_by_id(db, member_id)
                    await cb.message.answer(f"🔄 Отмена: {name2}. Текущий остаток {rem}/{total}.")

            elif action == "vac":
                new_vac = 0 if vac else 1
                await db.execute("UPDATE members SET vacation=? WHERE id=?", (new_vac, member_id))
                await db.commit()
                await cb.message.answer(f"🏖 Отпуск для {name}: {'включён' if new_vac else 'выключен'}.")

            # Остаёмся в подменю выбранного ученика
            _id, name, rem, total, vac = await get_member_by_id(db, member_id)
            text = f"Выбран: {name} — {rem}/{total} тренировок" + (" 🏖" if vac else "")
            await cb.message.edit_text(text, reply_markup=actions_keyboard(member_id, vac))

    await cb.answer()

# ---------- ЗАПУСК ----------
async def main():
    await ensure_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
