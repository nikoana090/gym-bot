import asyncio, datetime as dt, csv, os, shutil
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, FSInputFile
)
import aiosqlite

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is missing")

# Персистентный каталог для БД (создай Volume в Railway и примонтируй, напр., в /data)
DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.exists("/data") else ".")
os.makedirs(DATA_DIR, exist_ok=True)

DB = os.path.join(DATA_DIR, "gym.db")

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
        "/add Имя [кол-во]\n"
        "/visit — отметить посещение (кнопки)\n"
        "/status Имя — остаток\n"
        "/list — список всех\n"
        "/renew Имя [кол-во] — продлить тренировки\n"
        "/edit Имя [кол-во] — изменить пакет\n"
        "/backup — создать бэкап базы (.db)\n"
        "/export — выгрузить журнал посещений (CSV)\n"
        "/dbpath — показать путь к БД\n"
        "/restore — восстановить базу (пришлите .db с подписью /restore)"
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
    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer(
            "Формат: /edit Имя [новое_кол-во]\n"
            "Примеры:\n"
            "/edit Роман 20 - изменить пакет на 20\n"
            "/edit Роман - показать текущие данные"
        )
    name = parts[1]
    new_trainings = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
    await ensure_db()
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT id, name, remaining, trainings_total FROM members WHERE name=?", (name,)
        ) as c:
            row = await c.fetchone()
        if not row:
            return await m.answer(f"❌ Ученик '{name}' не найден")
        member_id, current_name, current_remaining, current_total = row
        if new_trainings is not None:
            await db.execute(
                "UPDATE members SET trainings_total=?, remaining=? WHERE id=?",
                (new_trainings, new_trainings, member_id)
            )
            await db.commit()
            await m.answer(
                f"✅ Обновлено: {name}\n"
                f"📊 Было: {current_total}\n"
                f"📊 Стало: {new_trainings}\n"
                f"💫 Остаток обновлён до: {new_trainings}"
            )
        else:
            await m.answer(
                f"📊 {name}:\n"
                f"• Всего тренировок: {current_total}\n"
                f"• Осталось: {current_remaining}\n"
                f"• Использовано: {current_total - current_remaining}\n\n"
                f"Чтобы изменить: /edit {name} [новое_число]"
            )

@dp.message(Command("backup"))
async def cmd_backup(m: Message):
    await ensure_db()
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"gym_backup_{ts}.db"
    backup_path = os.path.join(DATA_DIR, backup_name)
    shutil.copy2(DB, backup_path)
    await m.answer_document(FSInputFile(backup_path), caption="📦 Резервная копия базы")

@dp.message(Command("export"))
async def cmd_export(m: Message):
    await ensure_db()
    csv_path = os.path.join(DATA_DIR, f"visits_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    async with aiosqlite.connect(DB) as db, open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["member_id", "member_name", "dt", "status", "remaining", "trainings_total", "vacation"])
        async with db.execute("""
            SELECT v.member_id, m.name, v.dt, v.status, m.remaining, m.trainings_total, m.vacation
            FROM visits v
            LEFT JOIN members m ON m.id = v.member_id
            ORDER BY v.id
        """) as c:
            async for row in c:
                w.writerow(row)
    await m.answer_document(FSInputFile(csv_path), caption="📤 Экспорт журнала посещений")

@dp.message(Command("dbpath"))
async def cmd_dbpath(m: Message):
    await m.answer(f"DB path: `{DB}`\nDATA_DIR: `{DATA_DIR}`", parse_mode="Markdown")

# --- ВОССТАНОВЛЕНИЕ БД ---
@dp.message(Command("restore"))
async def cmd_restore(m: Message):
    """Инструкция и восстановление, если файл приложен без подписи."""
    if not m.document:
        return await m.answer(
            "🔄 Для восстановления базы:\n"
            "1) Отправь файл .db с подписью `/restore`\n"
            "2) Я заменю текущую базу и перезапущу бота\n\n"
            "⚠️ Все текущие данные будут заменены."
        )
    # если прислали как /restore + файл (без подписи к самому документу)
    if not m.document.file_name.endswith(".db"):
        return await m.answer("✗ Файл должен быть .db")
    await _do_restore_from_document(m, m.document.file_name)

@dp.message(F.document & (F.caption.startswith("/restore")))
async def restore_with_caption(m: Message):
    """Восстановление, если к файлу приложена подпись /restore."""
    if not m.document.file_name.endswith(".db"):
        return await m.answer("✗ Файл должен быть .db")
    await _do_restore_from_document(m, m.document.file_name)

@dp.message(F.document & ~F.caption)
async def restore_document_without_caption(m: Message):
    if not m.document.file_name.endswith(".db"):
        return await m.answer("✗ Файл должен быть .db")
    await m.answer(
        "Я получил файл базы.\n"
        "Чтобы восстановить его, отправь этот же файл с подписью:\n\n"
        "`/restore`",
        parse_mode="Markdown"
    )

async def _do_restore_from_document(m: Message, file_name: str):
    try:
        await m.answer("🔄 Восстанавливаю базу из бэкапа...")
        file_path = os.path.join(DATA_DIR, f"restored_{file_name}")
        await bot.download(m.document, destination=file_path)
        shutil.copy2(file_path, DB)
        os.remove(file_path)
        await m.answer("✅ База восстановлена. Перезапускаю бота…")
        import sys
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        await m.answer(f"✗ Ошибка при восстановлении: {e}")

# ---------- ОБРАБОТЧИКИ КНОПОК ----------
@dp.callback_query(
    F.data.startswith("member_") |
    F.data.startswith("act_") |
    (F.data == "back_to_list")
)
async def handle_member_and_actions(cb: CallbackQuery):
    try:
        await ensure_db()
        async with aiosqlite.connect(DB) as db:
            # Назад к списку
            if cb.data == "back_to_list":
                members = await get_all_members(db)
                await cb.message.edit_text("Кого отмечаем сегодня?", reply_markup=members_keyboard(members))
                return await cb.answer()

            # Подменю по ученику
            if cb.data.startswith("member_"):
                member_id = int(cb.data.split("_", 1)[1])
                row = await get_member_by_id(db, member_id)
                if not row:
                    return await cb.answer("Не нашёл ученика", show_alert=True)
                _id, name, rem, total, vac = row
                text = f"Выбран: {name} — {rem}/{total} тренировок" + (" 🏖" if vac else "")
                await cb.message.edit_text(text, reply_markup=actions_keyboard(member_id, vac))
                return await cb.answer()

            # Действия
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
                        msg += "\n🏖 В отпуске — не списано."
                    await cb.answer(msg, show_alert=True)

                elif action == "renew":
                    await renew_trainings(db, member_id, None)
                    _id, name, rem, total, vac = await get_member_by_id(db, member_id)
                    await cb.answer(f"💰 Продлены тренировки: {name} — {total} занятий.", show_alert=True)

                elif action == "edit":
                    await cb.answer(
                        f"✏️ Редактирование: {name}\n"
                        f"Текущий пакет: {total}\n"
                        f"Отправь: /edit {name} [новое_число]",
                        show_alert=True
                    )

                elif action == "undo":
                    name2, err = await undo_last(db, member_id)
                    if err:
                        await cb.answer(f"🔄 {err}", show_alert=True)
                    else:
                        _id, _nm, rem, total, vac = await get_member_by_id(db, member_id)
                        await cb.answer(f"🔄 Отмена: {name2}. Остаток {rem}/{total}.", show_alert=True)

                elif action == "vac":
                    new_vac = 0 if vac else 1
                    await db.execute("UPDATE members SET vacation=? WHERE id=?", (new_vac, member_id))
                    await db.commit()
                    await cb.answer(f"🏖 Отпуск для {name}: {'включён' если new_vac else 'выключен'}.", show_alert=True)

                # Обновляем подменю
                _id, name, rem, total, vac = await get_member_by_id(db, member_id)
                text = f"Выбран: {name} — {rem}/{total} тренировок" + (" 🏖" if vac else "")
                try:
                    await cb.message.edit_text(text, reply_markup=actions_keyboard(member_id, vac))
                except Exception as e:
                    if "message is not modified" not in str(e).lower():
                        raise
                return await cb.answer()

    except Exception as e:
        return await cb.answer(f"Ошибка: {e}", show_alert=True)

# ---------- ЗАПУСК ----------
async def main():
    await ensure_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
