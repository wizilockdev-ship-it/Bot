import asyncio
import json
import sqlite3
import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ==================== НАСТРОЙКИ (ВСЁ ЖЕСТКО В КОДЕ) ====================
BOT_TOKEN = "8962930179:AAF_KwqJ9MAR-_CJTn3HsqaqmZG1ImaoEoY"
ADMIN_ID = 8786951363  # Ваш Telegram ID (число)
SELL_CODE = "1ptjf"  # Ваш код с сайта linkni.me
DEV_CHANNEL_ID = "@inviteandpay"  # Канал разработчика (например: @my_channel)
DEV_CHANNEL_LINK = "https://t.me/inviteandpay"  # Ссылка на канал

PORT = 10000  # Фиксированный порт для Render (без переменной окружения)
DB_FILE = "database.db"

PRGRAM_RATE = 2.0  # 1 PRGRAM = 2 рубля
REFERRAL_REWARD = 50.0  # Вознаграждение за реферала после ОП

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ==================== БАЗА ДАННЫХ (SQLite) ====================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL DEFAULT 0.0,
            referrals INTEGER DEFAULT 0,
            referred_by INTEGER,
            is_linkni_sub INTEGER DEFAULT 0,
            joined_date TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            amount REAL,
            method TEXT,
            details TEXT,
            referrals INTEGER,
            status TEXT DEFAULT 'pending'
        )
    """)
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect(DB_FILE)

def get_user(user_id: int, username: str = ""):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, balance, referrals, referred_by, is_linkni_sub, joined_date FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")

    if not row:
        cursor.execute(
            "INSERT INTO users (user_id, username, balance, referrals, referred_by, is_linkni_sub, joined_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, 0.0, 0, None, 0, today_str)
        )
        conn.commit()
        cursor.execute("SELECT user_id, username, balance, referrals, referred_by, is_linkni_sub, joined_date FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    else:
        if username and row[1] != username:
            cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
            conn.commit()

    conn.close()
    return {
        "user_id": row[0],
        "username": row[1],
        "balance": row[2],
        "referrals": row[3],
        "referred_by": row[4],
        "is_linkni_sub": bool(row[5]),
        "joined_date": row[6]
    }

def update_user_field(user_id: int, field: str, value):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
    conn.commit()
    conn.close()

# ==================== ПРОВЕРКА КАНАЛА РАЗРАБОТЧИКА ====================
async def check_dev_channel_sub(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=DEV_CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False

# ==================== СТАТУСЫ FSM ====================
class WithdrawState(StatesGroup):
    waiting_for_details = State()
    waiting_for_admin_reject = State()

# ==================== КЛАВИАТУРЫ ====================
def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💎 Заработать"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="💸 Вывод"), KeyboardButton(text="📊 Статистика")]
        ],
        resize_keyboard=True
    )

def sub_kb(user_id: int, is_dev_sub: bool, is_linkni_sub: bool):
    buttons = []
    
    dev_status = "✅" if is_dev_sub else "❌"
    buttons.append([InlineKeyboardButton(
        text=f"{dev_status} 1. Канал Разработчика", 
        url=DEV_CHANNEL_LINK
    )])

    linkni_status = "✅" if is_linkni_sub else "❌"
    linkni_url = f"https://telegram.me/linknibot/app?startapp=x_{SELL_CODE}_{user_id}"
    buttons.append([InlineKeyboardButton(
        text=f"{linkni_status} 2. Задания Linkni (ОП)", 
        url=linkni_url
    )])

    buttons.append([InlineKeyboardButton(text="🔄 Проверить подписки", callback_query_data="check_all_subs")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_panel_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Заявки на вывод", callback_query_data="admin_withdrawals")]
        ]
    )

# ==================== ХЭНДЛЕРЫ БОТА ====================

@router.message(CommandStart())
async def start_cmd(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name or "User"
    u_data = get_user(user_id, username)

    args = message.text.split()
    if len(args) > 1 and not u_data["referred_by"]:
        referrer_id = args[1]
        if referrer_id.isdigit() and int(referrer_id) != user_id:
            update_user_field(user_id, "referred_by", int(referrer_id))

    is_dev_sub = await check_dev_channel_sub(user_id)
    is_linkni_sub = u_data["is_linkni_sub"]

    if not (is_dev_sub and is_linkni_sub):
        text = (
            "🚀 <b>Добро пожаловать в бота!</b>\n\n"
            "Для доступа к заработку и всем функциям необходимо выполнить <b>2 простых шага</b>:\n\n"
            "1️⃣ Подписаться на наш <b>Канал Разработчика</b>\n"
            "2️⃣ Пройти подписку в сервисе <b>Linkni</b>\n\n"
            "<i>После выполнения нажмите кнопку проверки!</i>"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=sub_kb(user_id, is_dev_sub, is_linkni_sub))
    else:
        await message.answer("✨ <b>Главное меню открыто!</b> Выбирайте нужный раздел ниже:", parse_mode="HTML", reply_markup=main_kb())

async def verify_sub_gate(message: Message) -> bool:
    user_id = message.from_user.id
    u_data = get_user(user_id)
    
    is_dev_sub = await check_dev_channel_sub(user_id)
    is_linkni_sub = u_data["is_linkni_sub"]

    if not (is_dev_sub and is_linkni_sub):
        await message.answer(
            "🔒 <b>Доступ ограничен!</b>\n\nВы отписались или не до конца прошли обязательные подписки.",
            parse_mode="HTML",
            reply_markup=sub_kb(user_id, is_dev_sub, is_linkni_sub)
        )
        return False
    return True

@router.callback_query(F.data == "check_all_subs")
async def check_all_subs_cb(callback: CallbackQuery):
    user_id = callback.from_user.id
    u_data = get_user(user_id)
    
    is_dev_sub = await check_dev_channel_sub(user_id)
    is_linkni_sub = u_data["is_linkni_sub"]

    if is_dev_sub and is_linkni_sub:
        await callback.message.delete()
        await callback.message.answer("🎉 <b>Все подписки подтверждены!</b> Добро пожаловать!", parse_mode="HTML", reply_markup=main_kb())
    else:
        await callback.answer("❌ Выполнены не все подписки! Перейдите по кнопкам выше.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=sub_kb(user_id, is_dev_sub, is_linkni_sub))

@router.message(F.text == "💎 Заработать")
async def earn_cmd(message: Message):
    if not await verify_sub_gate(message): return
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    text = (
        f"💰 <b>Заработок на рефералах</b>\n\n"
        f"Приглашайте друзей по вашей личной ссылке! За каждого приглашенного пользователя, "
        f"который выполнит обязательные подписки, вы получаете <b>{REFERRAL_REWARD:.0f} ₽</b> на баланс!\n\n"
        f"🔗 <b>Ваша реферальная ссылка:</b>\n"
        f"<code>{ref_link}</code>"
    )
    await message.answer(text, parse_mode="HTML")

@router.message(F.text == "👤 Профиль")
async def profile_cmd(message: Message):
    if not await verify_sub_gate(message): return
    u_data = get_user(message.from_user.id)
    text = (
        f"👤 <b>Ваш личный профиль</b>\n\n"
        f"🆔 <b>ID:</b> <code>{message.from_user.id}</code>\n"
        f"💵 <b>Баланс:</b> <code>{u_data['balance']:.2f} ₽</code>\n"
        f"👥 <b>Рефералов:</b> <code>{u_data['referrals']} чел.</code>\n"
        f"📅 <b>Дата регистрации:</b> {u_data['joined_date']}"
    )
    await message.answer(text, parse_mode="HTML")

@router.message(F.text == "📊 Статистика")
async def stats_cmd(message: Message):
    if not await verify_sub_gate(message): return
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    cursor.execute("SELECT COUNT(*) FROM users WHERE joined_date = ?", (today_str,))
    today_users = cursor.fetchone()[0]
    conn.close()

    text = (
        f"📊 <b>Общая статистика бота</b>\n\n"
        f"🌐 <b>Всего игроков в боте:</b> <code>{total_users}</code>\n"
        f"🆕 <b>Новых за сегодня:</b> <code>{today_users}</code>\n\n"
        f"🔥 Проект развивает и выплачивает стабильно!"
    )
    await message.answer(text, parse_mode="HTML")

@router.message(F.text == "💸 Вывод")
async def withdraw_cmd(message: Message):
    if not await verify_sub_gate(message): return
    
    weekday = datetime.datetime.now().weekday()
    if weekday not in [5, 6]:
        await message.answer(
            "⏳ <b>Вывод средств временно закрыт!</b>\n\nЗаявки принимаются <b>только по выходным дням</b> (Суббота и Воскресенье).",
            parse_mode="HTML"
        )
        return

    u_data = get_user(message.from_user.id)
    bal = u_data["balance"]
    if bal <= 0:
        await message.answer("❌ <b>У вас недостаточный баланс для вывода.</b>", parse_mode="HTML")
        return

    max_prgram = bal / PRGRAM_RATE
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Карта (Рубли)", callback_query_data="w_method_card")],
        [InlineKeyboardButton(text="💎 Криптовалюта (TON)", callback_query_data="w_method_ton")],
        [InlineKeyboardButton(text=f"⚡ PRGRAM ({max_prgram:.1f} Gram)", callback_query_data="w_method_prgram")]
    ])

    await message.answer(
        f"💸 <b>Вывод средств</b>\n\nДоступно на балансе: <b>{bal:.2f} ₽</b>\nВыберите удобный способ:",
        parse_mode="HTML",
        reply_markup=kb
    )

@router.callback_query(F.data.startswith("w_method_"))
async def method_select(callback: CallbackQuery, state: FSMContext):
    method = callback.data.split("_")[2]
    u_data = get_user(callback.from_user.id)
    bal = u_data["balance"]
    
    await state.update_data(method=method)
    
    if method == "prgram":
        max_gram = bal / PRGRAM_RATE
        text = (
            f"⚡ <b>Вывод в PRGRAM</b>\n\nКурс: <code>1 Gram = {PRGRAM_RATE} ₽</code>\n"
            f"Вы получите: <b>{max_gram:.2f} Gram</b>\n\nВведите ваш логин/адрес в PRGRAM:"
        )
    else:
        text = f"💳 <b>Вывод ({method.upper()})</b>\n\nСумма: <b>{bal:.2f} ₽</b>\nВведите реквизиты карты или кошелька:"

    await state.set_state(WithdrawState.waiting_for_details)
    await callback.message.answer(text, parse_mode="HTML")

@router.message(WithdrawState.waiting_for_details)
async def process_withdraw(message: Message, state: FSMContext):
    user_id = message.from_user.id
    u_data = get_user(user_id)
    bal = u_data["balance"]
    
    state_data = await state.get_data()
    method = state_data.get("method")
    details = message.text.strip()
    
    amount = bal
    update_user_field(user_id, "balance", 0.0)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO withdrawals (user_id, username, amount, method, details, referrals, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
        (user_id, u_data["username"], amount, method, details, u_data["referrals"])
    )
    wid = cursor.lastrowid
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer("✅ <b>Заявка на вывод создана!</b> Ожидайте проверки администратором.", parse_mode="HTML", reply_markup=main_kb())

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_query_data=f"adm_approve_{wid}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_query_data=f"adm_reject_{wid}")
        ]
    ])
    
    await bot.send_message(
        ADMIN_ID,
        f"🚨 <b>Новая заявка на вывод #{wid}</b>\n\n"
        f"👤 <b>Игрок:</b> @{u_data['username']} (<code>{user_id}</code>)\n"
        f"👥 <b>Рефералов:</b> {u_data['referrals']}\n"
        f"💰 <b>Сумма:</b> <code>{amount:.2f} ₽</code>\n"
        f"📌 <b>Метод:</b> {method.upper()}\n"
        f"📝 <b>Реквизиты:</b> <code>{details}</code>",
        parse_mode="HTML",
        reply_markup=admin_kb
    )

# ==================== АДМИН ПАНЕЛЬ ====================
@router.message(Command("admin"))
async def admin_cmd(message: Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("🛠 <b>Панель администратора</b>", parse_mode="HTML", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_withdrawals")
async def view_withdrawals(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, amount, username FROM withdrawals WHERE status = 'pending'")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await callback.message.answer("Активных заявок нет.")
    else:
        text = "📋 <b>Список активных заявок:</b>\n\n" + "\n".join([f"• Заявка #{r[0]}: {r[1]} ₽ (@{r[2]})" for r in rows])
        await callback.message.answer(text, parse_mode="HTML")

@router.callback_query(F.data.startswith("adm_approve_"))
async def approve_w(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    wid = callback.data.split("_")[2]
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, amount FROM withdrawals WHERE id = ? AND status = 'pending'", (wid,))
    row = cursor.fetchone()

    if row:
        user_id, amount = row[0], row[1]
        cursor.execute("UPDATE withdrawals SET status = 'approved' WHERE id = ?", (wid,))
        conn.commit()
        
        await callback.message.edit_text(callback.message.text + "\n\n✅ <b>ОДОБРЕНО</b>", parse_mode="HTML")
        await bot.send_message(user_id, f"🎉 <b>Ваша заявка на вывод #{wid} ({amount:.2f} ₽) одобрена и выплачена!</b>", parse_mode="HTML")
    conn.close()

@router.callback_query(F.data.startswith("adm_reject_"))
async def reject_w(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    wid = callback.data.split("_")[2]
    await state.update_data(reject_wid=wid)
    await state.set_state(WithdrawState.waiting_for_admin_reject)
    await callback.message.answer("Введите причину отказа:")

@router.message(WithdrawState.waiting_for_admin_reject)
async def process_rejection_reason(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    reason = message.text
    data = await state.get_data()
    wid = data.get("reject_wid")
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, amount FROM withdrawals WHERE id = ? AND status = 'pending'", (wid,))
    row = cursor.fetchone()

    if row:
        user_id, amount = row[0], row[1]
        cursor.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = ?", (wid,))
        cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()

        await message.answer(f"Заявка #{wid} отклонена. Баланс возвращен.")
        await bot.send_message(
            user_id,
            f"❌ <b>Ваша заявка на вывод #{wid} была отклонена.</b>\n\n"
            f"<b>Причина:</b> {reason}\n"
            f"💰 Средства (<b>{amount:.2f} ₽</b>) возвращены на ваш баланс.",
            parse_mode="HTML"
        )
    conn.close()
    await state.clear()

# ==================== WEBHOOK LINKNI ====================
async def linkni_webhook_handler(request):
    try:
        data = await request.json()
        status = data.get("status")
        sub_code = data.get("sub_code")

        if status == "subscribed" and sub_code and str(sub_code).isdigit():
            user_id = int(sub_code)
            u_data = get_user(user_id)

            if not u_data["is_linkni_sub"]:
                update_user_field(user_id, "is_linkni_sub", 1)

                ref_id = u_data.get("referred_by")
                if ref_id:
                    ref_data = get_user(ref_id)
                    new_bal = ref_data["balance"] + REFERRAL_REWARD
                    new_refs = ref_data["referrals"] + 1
                    
                    update_user_field(ref_id, "balance", new_bal)
                    update_user_field(ref_id, "referrals", new_refs)

                    await bot.send_message(
                        ref_id,
                        f"🎉 <b>Ваш реферал прошёл подписку Linkni!</b>\n"
                        f"💰 На ваш баланс начислено <b>+{REFERRAL_REWARD:.0f} ₽</b>!",
                        parse_mode="HTML"
                    )

                await bot.send_message(user_id, "🎉 <b>Задание Linkni успешно выполнено!</b>", parse_mode="HTML")

        return web.json_response({"status": "ok"})
    except Exception as e:
        print(f"Error in webhook: {e}")
        return web.json_response({"status": "error"}, status=500)

# ==================== ЗАПУСК ПРИЛОЖЕНИЯ ====================
async def main():
    init_db()
    app = web.Application()
    app.router.add_post("/webhook", linkni_webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    print(f"🚀 Веб-сервер запущен на порту {PORT}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
