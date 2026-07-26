# ============================================================================
#  VELRUM — бот взаимных подписок за баллы (валюта: V)
#  Версия под Render (веб-сервис + фоновый поллинг), без картинок,
#  с единоразовым штрафом за раннюю отписку и полным управлением заданиями.
# ============================================================================

# ===== БЛОК: CONFIG ==========================================================
# Все ключевые настройки указываются прямо здесь, вручную — ничего в
# переменных окружения искать не нужно. Просто впиши свои значения ниже.

import os

# --- Токен бота (получить у @BotFather) ---
BOT_TOKEN = "8852402958:AAFoS_sWh890Bt1rVDpuLi0ko302VYHTJUw"

# --- Telegram ID администраторов бота (можно несколько через запятую) ---
ADMIN_IDS = [8786951363]

# --- ID закрытого чата поддержки (группа/супергруппа, куда бот пересылает
# вопросы игроков). Бот должен быть добавлен в этот чат. Чтобы узнать ID —
# добавь бота в чат, напиши там что угодно, и ID придёт в логах бота при
# первом же сообщении (или используй любого бота типа @getmyid_bot). ID
# групп/супергрупп отрицательный, например -1001234567890.
ADMIN_CHAT_ID = -1003953440216

# --- Ссылка на создателя бота — показывается в статистике админ-панели ---
CREATOR_LINK = "https://t.me/winikson"

DB_FILE = "db.json"
# Если DATABASE_URL не пустая строка — бот использует Postgres (Render Postgres /
# Supabase / Neon), иначе — локальный файл db.json. На Render диск НЕ сохраняется
# между деплоями и перезапусками — для реального проекта обязательно впиши сюда
# connection string своей Postgres-базы!
DATABASE_URL = "postgresql://neondb_owner:npg_nRlTm2JjM9PC@ep-wispy-grass-aw9i2moo-pooler.c-12.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"   # например: "postgresql://user:password@host:5432/dbname"

# Порт для health-check веб-сервера. Render сам передаёт нужный порт через
# переменную окружения PORT — эту строку менять не нужно.
PORT = int(os.getenv("PORT", "10000"))

BOT_DISPLAY_NAME = "VELRUM"
CURRENCY_NAME = "V"

# --- Обязательный канал для использования бота ---
MANDATORY_CHANNEL_ID = -1004307471533   # например: -1001234567890 (0 = проверка отключена)
MANDATORY_CHANNEL_LINK = "https://t.me/velrum_hub"

# --- Экономика ---
MIN_PRICE_PER_SUB = 500
DAILY_BONUS_AMOUNT = 1500
DAILY_BONUS_COOLDOWN_HOURS = 24

# --- Реферальная система ---
# Реферал засчитывается (и начисляется награда) только после того как
# приглашённый подпишется на обязательный канал (MANDATORY_CHANNEL_ID).
REFERRAL_REWARD = 4000            # награда за обычного реферала, V
REFERRAL_REWARD_PREMIUM = 8000    # награда, если у реферала Telegram Premium, V

# --- Штраф за раннюю отписку: единоразовый, фиксированный ---
UNSUBSCRIBE_LOCK_DAYS = 7        # сколько дней подряд нужно быть подписанным, чтобы награда закрепилась
RESUB_GRACE_HOURS = 24           # сколько часов даётся на возврат в канал после отписки, пока сумма удержана
FREEZE_AFTER_OFFENSES = 3        # после скольких нарушений баланс замораживается

EARN_PAGE_SIZE = 6
SUBSCRIPTION_RECHECK_INTERVAL = 900   # фоновая проверка, сек (15 мин)

# ============================================================================

import asyncio
import datetime as dt
import html
import json
import logging

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ChatMemberUpdated,
)

from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

try:
    import asyncpg
except ImportError:
    asyncpg = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("velrum")

BOT_USERNAME = ""   # заполняется автоматически при запуске (main()), нужно для реф. ссылок

# ============================================================================
# ===== БЛОК: ОФОРМЛЕНИЕ ТЕКСТА (без картинок — упор на аккуратную типографику)
# ============================================================================

BAR = "━━━━━━━━━━━━━━━━━━━━"


def screen_header(icon: str, title: str) -> str:
    return f"{icon}  <b>{title.upper()}</b>\n{BAR}\n"


def card(lines: list[str]) -> str:
    """Аккуратный текстовый блок-«карточка» из строк."""
    return "\n".join(lines)


def fmt_v(amount) -> str:
    return f"{amount} {CURRENCY_NAME}"


async def send_screen(target, text: str, reply_markup=None):
    msg_target = target if isinstance(target, Message) else target.message
    await msg_target.answer(text, reply_markup=reply_markup)


# ============================================================================
# ===== БЛОК: ХРАНИЛИЩЕ ДАННЫХ (JSON-файл или Postgres) =======================
# ============================================================================

_db_lock = asyncio.Lock()
_pg_pool = None
DB: dict = {}


def _default_db() -> dict:
    return {
        "users": {},
        "tasks": {},
        "completions": [],
        "transactions": [],
        "support_tickets": {},   # admin_chat_message_id -> {user_id, username, full_name, created_at}
        "meta": {
            "next_task_id": 1,
            "start_date": None,
            "turnover": 0,
            "support_chat_id": None,   # назначается командой /setsupport прямо в нужном чате
        },
    }


def _load_db_from_file() -> dict:
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("db.json повреждён или пуст — создаю новую базу.")
    return _default_db()


def _save_db_to_file():
    tmp_path = DB_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, DB_FILE)


async def init_db():
    global DB, _pg_pool
    if DATABASE_URL:
        if asyncpg is None:
            raise RuntimeError("DATABASE_URL задан, но пакет asyncpg не установлен: pip install asyncpg")
        _pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with _pg_pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    id INTEGER PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            row = await conn.fetchrow("SELECT data FROM bot_state WHERE id = 1")
        if row is not None:
            DB = json.loads(row["data"])
            logger.info("База загружена из Postgres.")
        else:
            DB = _default_db()
            logger.info("Postgres пуст — создаю новую базу.")
    else:
        DB = _load_db_from_file()
        logger.warning(
            "DATABASE_URL не задан — работаю с локальным файлом %s. "
            "На Render это НЕ переживёт перезапуск/деплой! Подключи Postgres.",
            DB_FILE,
        )

    for key, default in _default_db().items():
        DB.setdefault(key, default)
    for u in DB["users"].values():
        u.setdefault("unsub_offenses", 0)
        u.setdefault("balance_frozen", False)
        u.setdefault("is_banned", False)
        u.setdefault("referrer_id", None)
        u.setdefault("referral_rewarded", False)
        u.setdefault("referrals_count", 0)
        u.setdefault("referrals_earned", 0)

    if not DB["meta"].get("start_date"):
        DB["meta"]["start_date"] = dt.datetime.utcnow().isoformat()
    DB["meta"].setdefault("support_chat_id", None)

    await _save_db()


async def _save_db():
    if _pg_pool is not None:
        payload = json.dumps(DB, ensure_ascii=False)
        async with _pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bot_state (id, data, updated_at) VALUES (1, $1, now())
                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()
                """,
                payload,
            )
    else:
        _save_db_to_file()


def _now() -> str:
    return dt.datetime.utcnow().isoformat()


def _new_user(user_id: int, username, full_name) -> dict:
    return {
        "id": user_id, "username": username, "full_name": full_name,
        "balance": 0, "created_at": _now(), "last_bonus_at": None,
        "is_banned": False, "balance_frozen": False, "unsub_offenses": 0,
        "referrer_id": None, "referral_rewarded": False,
        "referrals_count": 0, "referrals_earned": 0,
    }


async def get_or_create_user(user_id: int, username, full_name):
    uid = str(user_id)
    async with _db_lock:
        if uid in DB["users"]:
            return DB["users"][uid], False
        DB["users"][uid] = _new_user(user_id, username, full_name)
        await _save_db()
        return DB["users"][uid], True


FROZEN_BALANCE_MESSAGE = (
    "🧊 Баланс заморожен из-за повторных ранних отписок и временно недоступен "
    "для трат. Обратитесь к администратору."
)


async def change_balance(user_id: int, amount: int, reason: str):
    uid = str(user_id)
    async with _db_lock:
        user = DB["users"].get(uid)
        if not user:
            return
        user["balance"] += amount
        DB["transactions"].append({"user_id": user_id, "amount": amount, "reason": reason, "created_at": _now()})
        DB["meta"]["turnover"] = DB["meta"].get("turnover", 0) + abs(amount)
        await _save_db()


# ============================================================================
# ===== БЛОК: ЗАДАНИЯ (заработок / продвижение) ===============================
# ============================================================================

async def get_active_tasks(exclude_user_id: int) -> list[dict]:
    async with _db_lock:
        completed_ids = {c["task_id"] for c in DB["completions"] if c["user_id"] == exclude_user_id}
        tasks = [
            t for t in DB["tasks"].values()
            if t["is_active"] and t["slots_used"] < t["slots_total"]
            and t["owner_id"] != exclude_user_id and t["id"] not in completed_ids
        ]
        tasks.sort(key=lambda t: t["created_at"], reverse=True)
        return tasks


VALID_STATUSES = {"member", "administrator", "creator"}


async def is_user_subscribed(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in VALID_STATUSES
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


NOT_SUBSCRIBED_ERROR = "✖️ Подписка не найдена. Подпишитесь и нажмите «Проверить» снова."


async def try_complete_task(bot: Bot, task_id: int, user_id: int):
    async with _db_lock:
        user = DB["users"].get(str(user_id))
        if user and user.get("is_banned"):
            return None, "⛔ Вы заблокированы и не можете выполнять задания."
        task = DB["tasks"].get(str(task_id))
        if not task or not task["is_active"]:
            return None, "Это задание больше недоступно."
        if any(c["task_id"] == task_id and c["user_id"] == user_id for c in DB["completions"]):
            return None, "Вы уже выполнили это задание."
        if task["slots_used"] >= task["slots_total"]:
            return None, "Слоты по этому заданию закончились."
        chat_id = task["chat_id"]

    if not await is_user_subscribed(bot, chat_id, user_id):
        return None, NOT_SUBSCRIBED_ERROR

    async with _db_lock:
        task = DB["tasks"].get(str(task_id))
        if not task or not task["is_active"] or task["slots_used"] >= task["slots_total"]:
            return None, "Слоты по этому заданию закончились."
        if any(c["task_id"] == task_id and c["user_id"] == user_id for c in DB["completions"]):
            return None, "Вы уже выполнили это задание."

        price = task["price_per_sub"]
        task["slots_used"] += 1
        DB["completions"].append({
            "task_id": task_id, "user_id": user_id, "chat_id": chat_id, "reward": price,
            "completed_at": _now(), "status": "active",  # active -> penalized | finalized
        })
        user = DB["users"][str(user_id)]
        user["balance"] += price
        DB["transactions"].append({"user_id": user_id, "amount": price, "reason": f"task_reward:{task_id}", "created_at": _now()})
        DB["meta"]["turnover"] = DB["meta"].get("turnover", 0) + price
        await _save_db()
        return price, None


async def create_task_with_payment(owner_id: int, chat_id: int, chat_title: str, chat_link: str, price: int, slots: int):
    total_cost = price * slots
    async with _db_lock:
        user = DB["users"].get(str(owner_id))
        if user and user.get("is_banned"):
            return None, "⛔ Вы заблокированы и не можете создавать задания."
        if user and user.get("balance_frozen"):
            return None, FROZEN_BALANCE_MESSAGE
        if user is None or user["balance"] < total_cost:
            have = user["balance"] if user else 0
            return None, f"✖️ Недостаточно {CURRENCY_NAME}. Нужно {fmt_v(total_cost)}, у вас {fmt_v(have)}."

        user["balance"] -= total_cost
        DB["transactions"].append({"user_id": owner_id, "amount": -total_cost, "reason": "task_creation", "created_at": _now()})
        DB["meta"]["turnover"] = DB["meta"].get("turnover", 0) + total_cost

        task_id = DB["meta"]["next_task_id"]
        DB["meta"]["next_task_id"] += 1
        task = {
            "id": task_id, "owner_id": owner_id, "chat_id": chat_id, "chat_title": chat_title,
            "chat_link": chat_link, "price_per_sub": price, "slots_total": slots,
            "slots_used": 0, "is_active": True, "created_at": _now(),
        }
        DB["tasks"][str(task_id)] = task
        await _save_db()
        return task, None


async def get_owner_tasks(owner_id: int) -> list[dict]:
    async with _db_lock:
        tasks = [t for t in DB["tasks"].values() if t["owner_id"] == owner_id]
        tasks.sort(key=lambda t: t["created_at"], reverse=True)
        return tasks


async def owner_delete_task(owner_id: int, task_id: int):
    async with _db_lock:
        task = DB["tasks"].get(str(task_id))
        if task is None or task["owner_id"] != owner_id:
            return False, 0
        remaining = task["slots_total"] - task["slots_used"]
        refund = remaining * task["price_per_sub"]
        if refund > 0:
            user = DB["users"].get(str(owner_id))
            if user:
                user["balance"] += refund
                DB["transactions"].append({"user_id": owner_id, "amount": refund, "reason": f"task_delete_refund:{task_id}", "created_at": _now()})
                DB["meta"]["turnover"] = DB["meta"].get("turnover", 0) + refund
        del DB["tasks"][str(task_id)]
        await _save_db()
        return True, refund


async def owner_toggle_task(owner_id: int, task_id: int):
    """Ставит задание на паузу или возобновляет — без удаления и без возврата денег."""
    async with _db_lock:
        task = DB["tasks"].get(str(task_id))
        if task is None or task["owner_id"] != owner_id:
            return None, "Задание не найдено или это не ваше задание."
        task["is_active"] = not task["is_active"]
        await _save_db()
        return task["is_active"], None


async def owner_buy_more_slots(owner_id: int, task_id: int, count: int):
    async with _db_lock:
        task = DB["tasks"].get(str(task_id))
        if task is None or task["owner_id"] != owner_id:
            return None, "Задание не найдено."
        user = DB["users"].get(str(owner_id))
        if user and user.get("is_banned"):
            return None, "⛔ Вы заблокированы."
        if user and user.get("balance_frozen"):
            return None, FROZEN_BALANCE_MESSAGE
        cost = task["price_per_sub"] * count
        if user is None or user["balance"] < cost:
            have = user["balance"] if user else 0
            return None, f"✖️ Недостаточно {CURRENCY_NAME}. Нужно {fmt_v(cost)}, у вас {fmt_v(have)}."
        user["balance"] -= cost
        task["slots_total"] += count
        task["is_active"] = True
        DB["transactions"].append({"user_id": owner_id, "amount": -cost, "reason": f"task_topup:{task_id}", "created_at": _now()})
        DB["meta"]["turnover"] = DB["meta"].get("turnover", 0) + cost
        await _save_db()
        return cost, None


# ============================================================================
# ===== БЛОК: РЕФЕРАЛЬНАЯ СИСТЕМА ==============================================
# Реферал засчитывается ОДИН раз и только после того, как приглашённый
# подтверждённо подписан на обязательный канал. До этого момента переход
# по ссылке просто запоминается (referrer_id), но награда не начисляется.
# ============================================================================

def referral_link(user_id: int) -> str:
    if not BOT_USERNAME:
        return "ссылка временно недоступна, попробуй ещё раз чуть позже"
    return f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"


async def register_referrer(new_user_id: int, referrer_id: int):
    if referrer_id == new_user_id:
        return
    async with _db_lock:
        new_user = DB["users"].get(str(new_user_id))
        referrer = DB["users"].get(str(referrer_id))
        if not new_user or not referrer:
            return
        if new_user.get("referrer_id"):
            return  # реферер уже закреплён — не перезаписываем
        new_user["referrer_id"] = referrer_id
        await _save_db()


async def try_reward_referral(bot: Bot, user_id: int, is_premium: bool):
    """Начисляет награду тому, кто пригласил user_id — но только один раз и
    только когда вызывается после подтверждённой подписки на канал."""
    async with _db_lock:
        user = DB["users"].get(str(user_id))
        if not user or not user.get("referrer_id") or user.get("referral_rewarded"):
            return
        referrer_id = user["referrer_id"]
        referrer = DB["users"].get(str(referrer_id))
        if not referrer:
            return

        reward = REFERRAL_REWARD_PREMIUM if is_premium else REFERRAL_REWARD
        referrer["balance"] += reward
        referrer["referrals_count"] = referrer.get("referrals_count", 0) + 1
        referrer["referrals_earned"] = referrer.get("referrals_earned", 0) + reward
        user["referral_rewarded"] = True
        DB["transactions"].append({"user_id": referrer_id, "amount": reward, "reason": f"referral:{user_id}", "created_at": _now()})
        DB["meta"]["turnover"] = DB["meta"].get("turnover", 0) + reward
        await _save_db()

    premium_note = " 👑 (у него Premium — двойная награда!)" if is_premium else ""
    try:
        await bot.send_message(
            referrer_id,
            f"🤝 <b>Новый реферал подтверждён!</b>\n{BAR}\nНачислено: <b>{fmt_v(reward)}</b>{premium_note}",
        )
    except Exception:
        pass


# ============================================================================
# ===== БЛОК: ЕЖЕДНЕВНЫЙ БОНУС ================================================
# ============================================================================

async def claim_daily_bonus(user_id: int):
    async with _db_lock:
        user = DB["users"].get(str(user_id))
        if user is None:
            return None, "Сначала нажмите /start."
        if user.get("is_banned"):
            return None, "⛔ Вы заблокированы."

        now = dt.datetime.utcnow()
        last = user.get("last_bonus_at")
        if last:
            elapsed = now - dt.datetime.fromisoformat(last)
            if elapsed < dt.timedelta(hours=DAILY_BONUS_COOLDOWN_HOURS):
                remaining = dt.timedelta(hours=DAILY_BONUS_COOLDOWN_HOURS) - elapsed
                hours = int(remaining.total_seconds() // 3600)
                minutes = int((remaining.total_seconds() % 3600) // 60)
                return None, f"⏳ Следующий бонус через {hours} ч {minutes} мин."

        user["balance"] += DAILY_BONUS_AMOUNT
        user["last_bonus_at"] = now.isoformat()
        DB["transactions"].append({"user_id": user_id, "amount": DAILY_BONUS_AMOUNT, "reason": "daily_bonus", "created_at": _now()})
        DB["meta"]["turnover"] = DB["meta"].get("turnover", 0) + DAILY_BONUS_AMOUNT
        await _save_db()
        return DAILY_BONUS_AMOUNT, None


# ============================================================================
# ===== БЛОК: ОТПИСКА → УДЕРЖАНИЕ СУММЫ → 24Ч НА ВОЗВРАТ ======================
# Правило: пока не прошло UNSUBSCRIBE_LOCK_DAYS (7 дней) подряд в канале —
# как только игрок отписывается, у него СРАЗУ списывается сумма награды за
# это задание, и ему приходит сообщение с кнопками "Подписаться" / "Проверить".
# Статус выполнения переходит в "grace" (удержание). Есть RESUB_GRACE_HOURS
# (24 часа), чтобы подписаться обратно:
#   • если подписался вовремя — сумма возвращается, статус снова "active";
#   • если нет — списание становится окончательным ("finalized"), плюс
#     засчитывается нарушение (после FREEZE_AFTER_OFFENSES баланс морозится).
# Цикл может повторяться (отписался/вернулся) сколько угодно раз, пока не
# истекут все 7 дней с момента выполнения задания.
# ============================================================================

def resub_keyboard(task: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Подписаться", url=task["chat_link"])],
        [InlineKeyboardButton(text="✅ Проверить", callback_data=f"resub_check:{task['id']}")],
    ])


async def start_unsub_grace(bot: Bot, task: dict, user_id: int):
    """Игрок отписался: сразу списываем сумму, даём RESUB_GRACE_HOURS на возврат."""
    async with _db_lock:
        completion = next(
            (c for c in DB["completions"] if c["task_id"] == task["id"] and c["user_id"] == user_id),
            None,
        )
        if completion is None or completion["status"] != "active":
            return  # уже в удержании / закрыто / не найдено — второй раз не запускаем
        price = completion["reward"]
        deadline = dt.datetime.utcnow() + dt.timedelta(hours=RESUB_GRACE_HOURS)
        completion["status"] = "grace"
        completion["grace_deadline"] = deadline.isoformat()

        user = DB["users"].get(str(user_id))
        if user:
            user["balance"] -= price
            DB["transactions"].append({"user_id": user_id, "amount": -price, "reason": f"unsub_hold:{task['id']}", "created_at": _now()})
            DB["meta"]["turnover"] = DB["meta"].get("turnover", 0) + price
        await _save_db()

    try:
        await bot.send_message(
            user_id,
            f"⚠️ <b>Вы отписались от «{task['chat_title']}»</b>\n{BAR}\n"
            f"Списано: {fmt_v(-price)}.\n"
            f"У вас есть {RESUB_GRACE_HOURS} ч, чтобы подписаться обратно — тогда сумма вернётся автоматически.\n"
            f"Если не успеть — списание останется окончательным.",
            reply_markup=resub_keyboard(task),
        )
    except Exception:
        pass


async def refund_unsub_hold(bot: Bot, task: dict, user_id: int, notify: bool = True):
    """Игрок вовремя подписался обратно — возвращаем удержанную сумму."""
    async with _db_lock:
        completion = next(
            (c for c in DB["completions"] if c["task_id"] == task["id"] and c["user_id"] == user_id),
            None,
        )
        if completion is None or completion["status"] != "grace":
            return False
        price = completion["reward"]
        completion["status"] = "active"
        completion.pop("grace_deadline", None)

        user = DB["users"].get(str(user_id))
        if user:
            user["balance"] += price
            DB["transactions"].append({"user_id": user_id, "amount": price, "reason": f"unsub_refund:{task['id']}", "created_at": _now()})
            DB["meta"]["turnover"] = DB["meta"].get("turnover", 0) + price
        await _save_db()

    if notify:
        try:
            await bot.send_message(
                user_id,
                f"✅ <b>Подписка на «{task['chat_title']}» восстановлена</b>\n{BAR}\n"
                f"Возвращено: {fmt_v(price)}. Спасибо, что остались с нами! 🙌",
            )
        except Exception:
            pass
    return True


async def finalize_unsub_penalty(bot: Bot, task: dict, user_id: int):
    """Игрок не успел подписаться обратно за RESUB_GRACE_HOURS — штраф окончательный."""
    async with _db_lock:
        completion = next(
            (c for c in DB["completions"] if c["task_id"] == task["id"] and c["user_id"] == user_id),
            None,
        )
        if completion is None or completion["status"] != "grace":
            return
        completion["status"] = "finalized"
        completion.pop("grace_deadline", None)

        user = DB["users"].get(str(user_id))
        just_frozen = False
        if user:
            user["unsub_offenses"] = user.get("unsub_offenses", 0) + 1
            if user["unsub_offenses"] >= FREEZE_AFTER_OFFENSES and not user.get("balance_frozen"):
                user["balance_frozen"] = True
                just_frozen = True
        await _save_db()

    freeze_line = (
        "\n\n🧊 Это уже не первое подобное нарушение — баланс заморожен до решения администратора."
        if just_frozen else ""
    )
    try:
        await bot.send_message(
            user_id,
            f"⛔ <b>Штраф окончательный</b>\n{BAR}\n"
            f"Вы не подписались обратно на «{task['chat_title']}» за {RESUB_GRACE_HOURS} ч. "
            f"Списанная сумма не возвращается." + freeze_line,
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("resub_check:"))
async def resub_check_callback(callback: CallbackQuery, bot: Bot):
    task_id = int(callback.data.split(":", 1)[1])
    user_id = callback.from_user.id
    async with _db_lock:
        task = DB["tasks"].get(str(task_id))
        completion = next(
            (c for c in DB["completions"] if c["task_id"] == task_id and c["user_id"] == user_id),
            None,
        )
    if not task or not completion or completion["status"] != "grace":
        await callback.answer("Актуальных удержаний по этому заданию не найдено.", show_alert=True)
        return

    if not await is_user_subscribed(bot, task["chat_id"], user_id):
        await callback.answer(NOT_SUBSCRIBED_ERROR, show_alert=True)
        return

    await refund_unsub_hold(bot, task, user_id, notify=False)
    try:
        await callback.message.edit_text(f"✅ Подписка восстановлена, {fmt_v(completion['reward'])} возвращены на баланс!")
    except TelegramBadRequest:
        pass
    await callback.answer("Готово ✅")


async def recheck_subscriptions(bot: Bot):
    """Фоновая проверка (каждые SUBSCRIPTION_RECHECK_INTERVAL сек):
    1) активные выполнения младше 7 дней — если игрок отписался, запускаем
       удержание суммы на RESUB_GRACE_HOURS (см. start_unsub_grace);
    2) выполнения в статусе "grace" — если игрок уже подписался обратно,
       автоматически возвращаем сумму; если время вышло — штраф окончательный;
    3) выполнения младше 7 дней, доживших до дедлайна в статусе "active" —
       закрываем как успешные (риск списания больше не грозит);
    4) проверяет, жив ли бот как админ в каналах активных заданий — если
       удалён из админов, задание деактивируется, без последствий для игроков.
    """
    now = dt.datetime.utcnow()
    cutoff = now - dt.timedelta(days=UNSUBSCRIBE_LOCK_DAYS)

    async with _db_lock:
        tasks_snapshot = {tid: dict(t) for tid, t in DB["tasks"].items() if t["is_active"]}
    for tid, task in tasks_snapshot.items():
        try:
            me = await bot.get_chat_member(task["chat_id"], bot.id)
            still_admin = me.status in ("administrator", "creator")
        except Exception:
            still_admin = False
        if not still_admin:
            async with _db_lock:
                t = DB["tasks"].get(tid)
                if t:
                    t["is_active"] = False
                    await _save_db()
            logger.info("Задание %s деактивировано: бот больше не админ канала.", tid)

    async with _db_lock:
        active_pending = [
            c for c in DB["completions"]
            if c["status"] == "active" and dt.datetime.fromisoformat(c["completed_at"]) >= cutoff
        ]
        grace_pending = [dict(c) for c in DB["completions"] if c["status"] == "grace"]
        tasks_snapshot = {tid: dict(t) for tid, t in DB["tasks"].items()}

    # 1) кто ещё "active" в пределах 7 дней — проверяем подписку
    for completion in active_pending:
        task = tasks_snapshot.get(str(completion["task_id"]))
        if task is None:
            continue
        if not await is_user_subscribed(bot, task["chat_id"], completion["user_id"]):
            await start_unsub_grace(bot, task, completion["user_id"])

    # 2) кто в "grace" — проверяем, вернулся ли, или вышло ли время
    for completion in grace_pending:
        task = tasks_snapshot.get(str(completion["task_id"]))
        if task is None:
            continue
        user_id = completion["user_id"]
        if await is_user_subscribed(bot, task["chat_id"], user_id):
            await refund_unsub_hold(bot, task, user_id)
            continue
        deadline_raw = completion.get("grace_deadline")
        if deadline_raw and now >= dt.datetime.fromisoformat(deadline_raw):
            await finalize_unsub_penalty(bot, task, user_id)

    # 3) кто дожил до 7 дней в статусе "active" — закрываем как успешные
    async with _db_lock:
        changed = False
        for c in DB["completions"]:
            if c["status"] == "active" and dt.datetime.fromisoformat(c["completed_at"]) < cutoff:
                c["status"] = "finalized"
                changed = True
        if changed:
            await _save_db()


# ============================================================================
# ===== БЛОК: КЛАВИАТУРЫ ======================================================
# ============================================================================

def main_menu(admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="⚡ Заработать"), KeyboardButton(text="🎁 Бонус")],
        [KeyboardButton(text="📯 Продвигать"), KeyboardButton(text="🛰 Кабинет")],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="🆘 Поддержка")],
    ]
    if admin:
        rows.append([KeyboardButton(text="🛠 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def back_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅️ Назад")]], resize_keyboard=True)


def earn_list_keyboard(tasks: list[dict], page: int) -> InlineKeyboardMarkup:
    start = page * EARN_PAGE_SIZE
    page_tasks = tasks[start:start + EARN_PAGE_SIZE]
    rows = []
    for t in page_tasks:
        rows.append([
            InlineKeyboardButton(text=f"📡 {t['chat_title'][:20]} · {fmt_v(t['price_per_sub'])}", url=t["chat_link"]),
            InlineKeyboardButton(text="✅ Проверить", callback_data=f"check:{t['id']}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"earn_page:{page - 1}"))
    if start + EARN_PAGE_SIZE < len(tasks):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"earn_page:{page + 1}"))
    if nav:
        rows.append(nav)
    if not rows:
        rows = [[InlineKeyboardButton(text="Заданий пока нет", callback_data="noop")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cabinet_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗂 Мои задания", callback_data="cabinet:my_tasks")],
        [InlineKeyboardButton(text="🤝 Реферальная система", callback_data="cabinet:referral")],
    ])


def my_tasks_keyboard(tasks: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for t in tasks:
        if not t["is_active"]:
            status, toggle_label = "⏸ На паузе", "▶️ Возобновить"
        elif t["slots_used"] >= t["slots_total"]:
            status, toggle_label = "⚪️ Слоты закончились", "▶️ Возобновить"
        else:
            status, toggle_label = "🟢 Активно", "⏸ Остановить"
        rows.append([InlineKeyboardButton(
            text=f"{status} · {t['chat_title'][:18]} · {t['slots_used']}/{t['slots_total']}",
            callback_data="noop",
        )])
        rows.append([
            InlineKeyboardButton(text="➕ Докупить", callback_data=f"task_buy:{t['id']}"),
            InlineKeyboardButton(text=toggle_label, callback_data=f"task_toggle:{t['id']}"),
        ])
        rows.append([
            InlineKeyboardButton(text="🗑 Удалить задание", callback_data=f"task_delete:{t['id']}"),
        ])
    if not rows:
        rows = [[InlineKeyboardButton(text="Заданий пока нет", callback_data="noop")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def task_delete_confirm_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"task_delete_confirm:{task_id}"),
            InlineKeyboardButton(text="✖️ Отмена", callback_data="cabinet:my_tasks"),
        ]
    ])


def mandatory_sub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Подписаться", url=MANDATORY_CHANNEL_LINK)],
        [InlineKeyboardButton(text="💠 Я подписался", callback_data="mandatory_check")],
    ])


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="🪙 Баланс пользователя", callback_data="admin:balance")],
        [InlineKeyboardButton(text="⛔ Бан / 💠 Разбан", callback_data="admin:ban")],
        [InlineKeyboardButton(text="🧊 Разморозить баланс", callback_data="admin:unfreeze")],
        [InlineKeyboardButton(text="📯 Управление заданиями", callback_data="admin:tasks")],
        [InlineKeyboardButton(text="🗂 Последние пользователи", callback_data="admin:users")],
        [InlineKeyboardButton(text="📢 Рассылка всем", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin:settings")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_tasks_keyboard(tasks: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for t in tasks:
        mark = "🟢" if t["is_active"] else "⏸"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {t['chat_title'][:22]} ({t['slots_used']}/{t['slots_total']})",
            callback_data=f"admin_task_toggle:{t['id']}",
        )])
    if not rows:
        rows = [[InlineKeyboardButton(text="Заданий пока нет", callback_data="noop")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================================
# ===== БЛОК: ОБЯЗАТЕЛЬНАЯ ПОДПИСКА ============================================
# ============================================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def is_subscribed_to_mandatory(bot: Bot, user_id: int) -> bool:
    if not MANDATORY_CHANNEL_ID:
        return True
    return await is_user_subscribed(bot, MANDATORY_CHANNEL_ID, user_id)


async def send_mandatory_gate(message: Message):
    await send_screen(
        message,
        screen_header("🔒", "Доступ закрыт") + "Чтобы пользоваться ботом — подпишись на канал ниже:",
        mandatory_sub_keyboard(),
    )


# ============================================================================
# ===== БЛОК: FSM-СОСТОЯНИЯ ====================================================
# ============================================================================

class AdvertiseForm(StatesGroup):
    waiting_for_chat = State()
    waiting_for_price = State()
    waiting_for_slots = State()


class TaskBuySubsForm(StatesGroup):
    waiting_for_count = State()


class AdminBalanceForm(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_amount = State()


class AdminBanForm(StatesGroup):
    waiting_for_user_id = State()


class AdminUnfreezeForm(StatesGroup):
    waiting_for_user_id = State()


class AdminBroadcastForm(StatesGroup):
    waiting_for_text = State()


class AdminSettingsForm(StatesGroup):
    waiting_for_values = State()


class SupportForm(StatesGroup):
    waiting_for_message = State()


router = Router(name="velrum")
group_router = Router(name="velrum_group")
support_router = Router(name="velrum_support")

router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


def get_support_chat_id() -> int:
    """Чат поддержки: сначала берём тот, что назначен командой /setsupport
    (хранится в базе и переживает рестарты), иначе — константу ADMIN_CHAT_ID
    из конфига вверху файла."""
    configured = DB.get("meta", {}).get("support_chat_id")
    return configured or ADMIN_CHAT_ID or 0


async def _is_support_chat(message: Message) -> bool:
    chat_id = get_support_chat_id()
    return bool(chat_id) and message.chat.id == chat_id


support_router.message.filter(_is_support_chat)


# ============================================================================
# ===== БЛОК: /start И ОБЯЗАТЕЛЬНАЯ ПОДПИСКА ===================================
# ============================================================================

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, bot: Bot):
    user, created = await get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name)

    if created and command.args and command.args.startswith("ref_"):
        try:
            referrer_id = int(command.args.split("_", 1)[1])
        except ValueError:
            referrer_id = None
        if referrer_id:
            await register_referrer(message.from_user.id, referrer_id)

    if not await is_subscribed_to_mandatory(bot, message.from_user.id):
        await send_mandatory_gate(message)
        return

    await try_reward_referral(bot, message.from_user.id, bool(message.from_user.is_premium))

    name = message.from_user.full_name or "друг"
    text = (
        f"🛰 <b>{BOT_DISPLAY_NAME}</b>\n{BAR}\n"
        f"Привет, {name} 👋\n\n"
        f"⚡ <b>Заработать</b> — подписывайся на каналы, получай {CURRENCY_NAME}\n"
        f"📯 <b>Продвигать</b> — трать {CURRENCY_NAME} на подписчиков для своего канала\n"
        f"🎁 <b>Бонус</b> — забирай награду каждые 24 часа\n"
        f"🛰 <b>Кабинет</b> — баланс и твои задания\n{BAR}\n"
        f"Выбери пункт меню ниже 👇"
    )
    await send_screen(message, text, main_menu(admin=is_admin(message.from_user.id)))


@router.callback_query(F.data == "mandatory_check")
async def mandatory_check_callback(callback: CallbackQuery, bot: Bot):
    if not await is_subscribed_to_mandatory(bot, callback.from_user.id):
        await callback.answer("✖️ Подписка не найдена — подпишись и попробуй снова.", show_alert=True)
        return
    await callback.answer("💠 Подписка подтверждена!")
    await get_or_create_user(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    await try_reward_referral(bot, callback.from_user.id, bool(callback.from_user.is_premium))
    await callback.message.answer(
        f"👋 Добро пожаловать в {BOT_DISPLAY_NAME}! Выбери пункт меню ниже 👇",
        reply_markup=main_menu(admin=is_admin(callback.from_user.id)),
    )


async def _require_mandatory(message_or_cb, bot: Bot) -> bool:
    user_id = message_or_cb.from_user.id
    if await is_subscribed_to_mandatory(bot, user_id):
        return True
    if isinstance(message_or_cb, CallbackQuery):
        await message_or_cb.answer("🔒 Сначала подпишись на обязательный канал.", show_alert=True)
        await send_mandatory_gate(message_or_cb.message)
    else:
        await send_mandatory_gate(message_or_cb)
    return False


# ============================================================================
# ===== БЛОК: "⚡ ЗАРАБОТАТЬ" ===================================================
# ============================================================================

@router.message(F.text == "⚡ Заработать")
async def show_tasks(message: Message, bot: Bot):
    if not await _require_mandatory(message, bot):
        return
    tasks = await get_active_tasks(exclude_user_id=message.from_user.id)
    if not tasks:
        await send_screen(message, screen_header("⚡", "Заработать") + "Сейчас нет доступных заданий.\nЗагляни чуть позже 🙌")
        return
    total_pages = max(1, (len(tasks) + EARN_PAGE_SIZE - 1) // EARN_PAGE_SIZE)
    await send_screen(
        message,
        screen_header("⚡", "Заработать") + f"Страница 1/{total_pages}\n\n"
        "📡 — переход на канал\n✅ — засчитать выполнение и получить награду",
        earn_list_keyboard(tasks, 0),
    )


@router.callback_query(F.data.startswith("earn_page:"))
async def earn_page(callback: CallbackQuery):
    page = int(callback.data.split(":", 1)[1])
    tasks = await get_active_tasks(exclude_user_id=callback.from_user.id)
    total_pages = max(1, (len(tasks) + EARN_PAGE_SIZE - 1) // EARN_PAGE_SIZE)
    try:
        await callback.message.edit_text(
            screen_header("⚡", "Заработать") + f"Страница {page + 1}/{total_pages}\n\n"
            "📡 — переход на канал\n✅ — засчитать выполнение и получить награду",
            reply_markup=earn_list_keyboard(tasks, page),
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("check:"))
async def check_task(callback: CallbackQuery, bot: Bot):
    task_id = int(callback.data.split(":", 1)[1])
    async with _db_lock:
        task_snapshot = DB["tasks"].get(str(task_id))
    task_title = task_snapshot["chat_title"] if task_snapshot else "задание"

    reward, error = await try_complete_task(bot, task_id, callback.from_user.id)
    if error:
        await callback.answer(error, show_alert=True)
        return

    await callback.answer(f"💠 Задание засчитано! {fmt_v(reward)}", show_alert=True)
    await callback.message.answer(f"💠 Подписка засчитана: «{task_title}» · {fmt_v(reward)}")

    remaining = await get_active_tasks(exclude_user_id=callback.from_user.id)
    try:
        await callback.message.edit_reply_markup(reply_markup=earn_list_keyboard(remaining, 0))
    except TelegramBadRequest:
        pass


# ============================================================================
# ===== БЛОК: "📯 ПРОДВИГАТЬ" ===================================================
# ============================================================================

@router.message(F.text == "📯 Продвигать")
async def start_advertise(message: Message, state: FSMContext, bot: Bot):
    if not await _require_mandatory(message, bot):
        return
    await state.set_state(AdvertiseForm.waiting_for_chat)
    await send_screen(
        message,
        screen_header("📯", "Новое задание") +
        "Перешли любое сообщение из своего канала/группы, либо отправь @username чата.\n\n"
        "⚠️ Бот должен быть администратором в этом чате с правом просмотра участников.",
        back_keyboard(),
    )


@router.message(AdvertiseForm.waiting_for_chat, F.text == "⬅️ Назад")
async def cancel_advertise(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu(admin=is_admin(message.from_user.id)))


@router.message(AdvertiseForm.waiting_for_chat)
async def receive_chat(message: Message, state: FSMContext, bot: Bot):
    chat_ref = None
    if message.forward_from_chat:
        chat_ref = message.forward_from_chat.id
    elif message.text and message.text.startswith("@"):
        chat_ref = message.text.strip()
    else:
        await message.answer("Не удалось распознать чат. Перешли сообщение из канала или отправь @username.")
        return

    try:
        chat = await bot.get_chat(chat_ref)
        member = await bot.get_chat_member(chat.id, bot.id)
        if member.status not in ("administrator", "creator"):
            await message.answer("✖️ Бот не администратор этого чата. Добавь бота в админы и попробуй снова.")
            return
    except Exception:
        await message.answer("✖️ Не удалось получить доступ к чату. Проверь права бота и попробуй снова.")
        return

    if not chat.username:
        await message.answer("✖️ У канала нет публичного юзернейма — ссылку «Подписаться» не сформировать. Используй канал с юзернеймом (@channel).")
        await state.clear()
        return

    await state.update_data(chat_id=chat.id, chat_title=chat.title or chat.username, chat_link=f"https://t.me/{chat.username}")
    await state.set_state(AdvertiseForm.waiting_for_price)
    await message.answer(f"Укажи цену за одного подписчика (в {CURRENCY_NAME}).\n\n🔴 Минимум — {fmt_v(MIN_PRICE_PER_SUB)}\n\n✏️ Введи цену числом:")


@router.message(AdvertiseForm.waiting_for_price)
async def receive_price(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("Введи цену числом.")
        return
    price = int(message.text)
    if price < MIN_PRICE_PER_SUB:
        await message.answer(f"Цена не может быть ниже {fmt_v(MIN_PRICE_PER_SUB)}.")
        return

    async with _db_lock:
        user = DB["users"].get(str(message.from_user.id))
        balance = user["balance"] if user else 0
    max_slots = balance // price if price else 0

    await state.update_data(price=price)
    await state.set_state(AdvertiseForm.waiting_for_slots)
    await message.answer(
        f"Сколько подписчиков нужно набрать?\n\n"
        f"🪙 Баланс: {fmt_v(balance)}\n📦 Доступно при цене {fmt_v(price)}: {max_slots} подписчиков\n\n"
        f"Введи количество числом:"
    )


@router.message(AdvertiseForm.waiting_for_slots)
async def receive_slots(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("Введи целое положительное число.")
        return
    slots = int(message.text)
    data = await state.get_data()
    task, error = await create_task_with_payment(
        message.from_user.id, data["chat_id"], data["chat_title"], data["chat_link"], data["price"], slots,
    )
    await state.clear()
    if error:
        await message.answer(error, reply_markup=main_menu(admin=is_admin(message.from_user.id)))
        return
    total_cost = data["price"] * slots
    await message.answer(
        f"✅ Задание создано! Списано {fmt_v(-total_cost)}.\nОно уже видно другим в разделе «⚡ Заработать».",
        reply_markup=main_menu(admin=is_admin(message.from_user.id)),
    )


# ============================================================================
# ===== БЛОК: "🛰 КАБИНЕТ" (профиль) ===========================================
# ============================================================================

@router.message(F.text == "🛰 Кабинет")
async def cabinet(message: Message, bot: Bot):
    if not await _require_mandatory(message, bot):
        return
    async with _db_lock:
        user = DB["users"].get(str(message.from_user.id))
    balance = user["balance"] if user else 0
    await send_screen(
        message,
        screen_header("🛰", "Кабинет") + f"💎 Баланс: <b>{fmt_v(balance)}</b>",
        cabinet_menu(),
    )


@router.callback_query(F.data == "cabinet:my_tasks")
async def cabinet_my_tasks(callback: CallbackQuery):
    tasks = await get_owner_tasks(callback.from_user.id)
    text = screen_header("🗂", "Мои задания")
    try:
        await callback.message.edit_text(text, reply_markup=my_tasks_keyboard(tasks))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=my_tasks_keyboard(tasks))
    await callback.answer()


@router.callback_query(F.data == "cabinet:referral")
async def cabinet_referral(callback: CallbackQuery):
    async with _db_lock:
        user = DB["users"].get(str(callback.from_user.id))
    count = user.get("referrals_count", 0) if user else 0
    earned = user.get("referrals_earned", 0) if user else 0
    text = (
        screen_header("🤝", "Рефералы") +
        f"Твоя ссылка:\n<code>{referral_link(callback.from_user.id)}</code>\n\n"
        f"💰 За реферала: {fmt_v(REFERRAL_REWARD)}\n"
        f"👑 Если у него Telegram Premium: {fmt_v(REFERRAL_REWARD_PREMIUM)}\n\n"
        f"⚠️ Реферал засчитывается только после того, как приглашённый подпишется "
        f"на обязательный канал.\n{BAR}\n"
        f"👥 Приглашено: <b>{count}</b>\n💎 Заработано всего: <b>{fmt_v(earned)}</b>"
    )
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("task_toggle:"))
async def task_toggle(callback: CallbackQuery):
    task_id = int(callback.data.split(":", 1)[1])
    is_active, error = await owner_toggle_task(callback.from_user.id, task_id)
    if error:
        await callback.answer(error, show_alert=True)
        return
    await callback.answer("▶️ Задание возобновлено." if is_active else "⏸ Задание поставлено на паузу.")
    tasks = await get_owner_tasks(callback.from_user.id)
    try:
        await callback.message.edit_reply_markup(reply_markup=my_tasks_keyboard(tasks))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("task_delete:"))
async def task_delete_ask(callback: CallbackQuery):
    task_id = int(callback.data.split(":", 1)[1])
    await callback.message.answer(
        "❗️ Удалить задание? Остаток за неиспользованные слоты вернётся на баланс.",
        reply_markup=task_delete_confirm_keyboard(task_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("task_delete_confirm:"))
async def task_delete_confirm(callback: CallbackQuery):
    task_id = int(callback.data.split(":", 1)[1])
    ok, refund = await owner_delete_task(callback.from_user.id, task_id)
    if not ok:
        await callback.answer("Задание не найдено или это не твоё задание.", show_alert=True)
        return
    await callback.answer(f"🗑 Удалено. Возвращено {fmt_v(refund)}.", show_alert=True)
    tasks = await get_owner_tasks(callback.from_user.id)
    try:
        await callback.message.edit_text(screen_header("🗂", "Мои задания"), reply_markup=my_tasks_keyboard(tasks))
    except TelegramBadRequest:
        await callback.message.answer(screen_header("🗂", "Мои задания"), reply_markup=my_tasks_keyboard(tasks))


@router.callback_query(F.data.startswith("task_buy:"))
async def task_buy_start(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split(":", 1)[1])
    await state.update_data(task_id=task_id)
    await state.set_state(TaskBuySubsForm.waiting_for_count)
    await callback.message.answer("Сколько подписчиков докупить? Введи число:")
    await callback.answer()


@router.message(TaskBuySubsForm.waiting_for_count)
async def task_buy_count(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("Введи целое положительное число.")
        return
    count = int(message.text)
    data = await state.get_data()
    cost, error = await owner_buy_more_slots(message.from_user.id, data["task_id"], count)
    await state.clear()
    if error:
        await message.answer(error)
        return
    await message.answer(f"✅ Добавлено {count} подписчиков. Списано {fmt_v(-cost)}.")


# ============================================================================
# ===== БЛОК: "🎁 ЕЖЕДНЕВНЫЙ БОНУС" =============================================
# ============================================================================

@router.message(F.text == "🎁 Бонус")
async def daily_bonus(message: Message, bot: Bot):
    if not await _require_mandatory(message, bot):
        return
    amount, error = await claim_daily_bonus(message.from_user.id)
    if error:
        await message.answer(error)
        return
    await send_screen(message, screen_header("🎁", "Бонус") + f"Получено: <b>{fmt_v(amount)}</b>!\nВозвращайся через {DAILY_BONUS_COOLDOWN_HOURS} ч 🕓")


# ============================================================================
# ===== БЛОК: "📊 СТАТИСТИКА" (личная статистика — доступна всем) =============
# ============================================================================

@router.message(F.text == "📊 Статистика")
async def personal_stats(message: Message, bot: Bot):
    if not await _require_mandatory(message, bot):
        return
    async with _db_lock:
        total_users = len(DB["users"])
        banned_count = sum(1 for u in DB["users"].values() if u.get("is_banned"))
        active_users = total_users - banned_count
        turnover = DB["meta"].get("turnover", 0)
        start_date_raw = DB["meta"].get("start_date")
    start_date = dt.datetime.fromisoformat(start_date_raw).strftime("%d.%m.%Y") if start_date_raw else "—"

    text = (
        screen_header("📊", "Статистика") +
        f"👤 Игроков в боте: <b>{active_users}</b>\n"
        f"💱 Валюты в обороте: <b>{fmt_v(turnover)}</b>\n"
        f"📅 Дата старта: {start_date}\n"
        f"👑 Админ: {CREATOR_LINK}"
    )
    await send_screen(message, text)


# ============================================================================
# ===== БЛОК: "🆘 ПОДДЕРЖКА" (переписка игрок ↔ закрытый чат админов) =========
# Игрок пишет вопрос → бот пересылает его в закрытый чат поддержки как
# отдельное сообщение → администратор отвечает на него ОТВЕТОМ (reply) в том
# же чате → бот берёт этот ответ и отправляет игроку в личку.
# ============================================================================

@router.message(F.text == "🆘 Поддержка")
async def support_start(message: Message, state: FSMContext, bot: Bot):
    if not await _require_mandatory(message, bot):
        return
    await state.set_state(SupportForm.waiting_for_message)
    await send_screen(
        message,
        screen_header("🆘", "Поддержка") +
        "Опиши свой вопрос или проблему одним сообщением — мы ответим прямо здесь, в этом чате.",
        back_keyboard(),
    )


@router.message(SupportForm.waiting_for_message, F.text == "⬅️ Назад")
async def support_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu(admin=is_admin(message.from_user.id)))


@router.message(SupportForm.waiting_for_message)
async def support_receive(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    user = message.from_user
    text = message.text or message.caption

    support_chat_id = get_support_chat_id()
    if not support_chat_id:
        await message.answer(
            "⚠️ Поддержка сейчас недоступна, попробуй позже.",
            reply_markup=main_menu(admin=is_admin(user.id)),
        )
        return
    if not text:
        await message.answer(
            "⚠️ Пришли вопрос текстом.",
            reply_markup=main_menu(admin=is_admin(user.id)),
        )
        return

    uname = f"@{user.username}" if user.username else (user.full_name or f"ID {user.id}")
    admin_text = (
        f"🆘 <b>Новое обращение</b>\n{BAR}\n"
        f"От: {html.escape(uname)} (ID: <code>{user.id}</code>)\n\n"
        f"{html.escape(text)}\n\n"
        f"↩️ Ответь на ЭТО сообщение (reply), чтобы отправить ответ игроку."
    )
    try:
        sent = await bot.send_message(support_chat_id, admin_text)
    except Exception:
        logger.warning("Не удалось отправить обращение в чат поддержки (support_chat_id=%s).", support_chat_id)
        await message.answer(
            "✖️ Не удалось отправить обращение. Попробуй позже.",
            reply_markup=main_menu(admin=is_admin(user.id)),
        )
        return

    async with _db_lock:
        DB.setdefault("support_tickets", {})[str(sent.message_id)] = {
            "user_id": user.id, "username": user.username, "full_name": user.full_name, "created_at": _now(),
        }
        await _save_db()

    await message.answer(
        screen_header("🆘", "Поддержка") + "✅ Сообщение отправлено. Как только ответят — пришлём ответ сюда же.",
        reply_markup=main_menu(admin=is_admin(user.id)),
    )


@support_router.message(F.reply_to_message)
async def support_answer(message: Message, bot: Bot):
    admin_msg_id = str(message.reply_to_message.message_id)
    async with _db_lock:
        ticket = DB.get("support_tickets", {}).get(admin_msg_id)
    if not ticket:
        return  # это не ответ на обращение поддержки — игнорируем

    answer_text = message.text or message.caption
    if not answer_text:
        await message.reply("⚠️ Ответ должен содержать текст.")
        return

    try:
        await bot.send_message(
            ticket["user_id"],
            f"🆘 <b>Ответ поддержки</b>\n{BAR}\n{html.escape(answer_text)}",
        )
        await message.reply("✅ Ответ отправлен игроку.")
    except Exception:
        await message.reply("✖️ Не удалось отправить ответ — возможно, игрок заблокировал бота.")


# ============================================================================
# ===== БЛОК: АДМИН-ПАНЕЛЬ =====================================================
# ============================================================================

@router.message(F.text == "🛠 Админ-панель")
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    await send_screen(message, screen_header("🛠", "Админ-панель"), admin_panel_keyboard())


@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    async with _db_lock:
        users_count = len(DB["users"])
        banned_count = sum(1 for u in DB["users"].values() if u.get("is_banned"))
        active_tasks = sum(1 for t in DB["tasks"].values() if t["is_active"])
        turnover = DB["meta"].get("turnover", 0)
        start_date_raw = DB["meta"].get("start_date")
    start_date = dt.datetime.fromisoformat(start_date_raw).strftime("%d.%m.%Y") if start_date_raw else "—"

    await callback.message.answer(
        screen_header("📊", "Статистика") +
        f"👤 Пользователей: <b>{users_count}</b> (забанено: {banned_count})\n"
        f"📯 Активных заданий: <b>{active_tasks}</b>\n"
        f"💱 Оборот {CURRENCY_NAME}: <b>{fmt_v(turnover)}</b>\n"
        f"📅 Запуск бота: {start_date}\n"
        f"👑 Создатель: {CREATOR_LINK}",
        reply_markup=admin_panel_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:balance")
async def admin_balance_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminBalanceForm.waiting_for_user_id)
    await callback.message.answer("Введи Telegram ID пользователя:")
    await callback.answer()


@router.message(AdminBalanceForm.waiting_for_user_id)
async def admin_balance_user_id(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("ID должен быть числом. Попробуй снова:")
        return
    target_id = int(message.text)
    if str(target_id) not in DB["users"]:
        await message.answer("Пользователь не найден (должен хотя бы раз запустить бота).")
        await state.clear()
        return
    await state.update_data(target_id=target_id)
    await state.set_state(AdminBalanceForm.waiting_for_amount)
    await message.answer(f"Введи сумму {CURRENCY_NAME} (можно отрицательную, например -500):")


@router.message(AdminBalanceForm.waiting_for_amount)
async def admin_balance_amount(message: Message, state: FSMContext, bot: Bot):
    if not message.text or not message.text.lstrip("-").isdigit():
        await message.answer("Введи число, например 1000 или -500.")
        return
    amount = int(message.text)
    data = await state.get_data()
    await change_balance(data["target_id"], amount, "admin_adjustment")
    await state.clear()
    await message.answer(f"✅ Баланс пользователя {data['target_id']} изменён на {fmt_v(amount)}.")
    try:
        await bot.send_message(data["target_id"], f"ℹ️ Администратор изменил твой баланс на {fmt_v(amount)}.")
    except Exception:
        pass


@router.callback_query(F.data == "admin:ban")
async def admin_ban_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminBanForm.waiting_for_user_id)
    await callback.message.answer("Введи Telegram ID пользователя, чтобы переключить бан:")
    await callback.answer()


@router.message(AdminBanForm.waiting_for_user_id)
async def admin_ban_toggle(message: Message, state: FSMContext):
    await state.clear()
    if not message.text or not message.text.isdigit():
        await message.answer("ID должен быть числом.")
        return
    uid = message.text
    async with _db_lock:
        user = DB["users"].get(uid)
        if not user:
            await message.answer("Пользователь не найден.")
            return
        user["is_banned"] = not user.get("is_banned", False)
        state_word = "забанен ⛔" if user["is_banned"] else "разбанен 💠"
        await _save_db()
    await message.answer(f"Пользователь {uid} теперь {state_word}.")


@router.callback_query(F.data == "admin:unfreeze")
async def admin_unfreeze_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminUnfreezeForm.waiting_for_user_id)
    await callback.message.answer("Введи Telegram ID пользователя, чтобы разморозить баланс:")
    await callback.answer()


@router.message(AdminUnfreezeForm.waiting_for_user_id)
async def admin_unfreeze(message: Message, state: FSMContext):
    await state.clear()
    if not message.text or not message.text.isdigit():
        await message.answer("ID должен быть числом.")
        return
    async with _db_lock:
        user = DB["users"].get(message.text)
        if not user:
            await message.answer("Пользователь не найден.")
            return
        user["balance_frozen"] = False
        user["unsub_offenses"] = 0
        await _save_db()
    await message.answer(f"🧊 Баланс пользователя {message.text} разморожен.")


@router.callback_query(F.data == "admin:tasks")
async def admin_tasks(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    async with _db_lock:
        tasks = sorted(DB["tasks"].values(), key=lambda t: t["created_at"], reverse=True)[:20]
    await callback.message.answer(screen_header("📯", "Последние задания"), reply_markup=admin_tasks_keyboard(tasks))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_task_toggle:"))
async def admin_task_toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    task_id = int(callback.data.split(":", 1)[1])
    async with _db_lock:
        task = DB["tasks"].get(str(task_id))
        if task:
            task["is_active"] = not task["is_active"]
            await _save_db()
    async with _db_lock:
        tasks = sorted(DB["tasks"].values(), key=lambda t: t["created_at"], reverse=True)[:20]
    try:
        await callback.message.edit_reply_markup(reply_markup=admin_tasks_keyboard(tasks))
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data == "admin:users")
async def admin_users(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    async with _db_lock:
        users = sorted(DB["users"].values(), key=lambda u: u["created_at"], reverse=True)[:15]
    lines = [screen_header("🗂", "Последние пользователи")]
    for u in users:
        name = f"@{u['username']}" if u.get("username") else (u.get("full_name") or f"ID {u['id']}")
        lines.append(f"• {name} — {fmt_v(u['balance'])} (ID: {u['id']})")
    await callback.message.answer("\n".join(lines))
    await callback.answer()


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminBroadcastForm.waiting_for_text)
    await callback.message.answer("Пришли текст рассылки для всех пользователей:")
    await callback.answer()


@router.message(AdminBroadcastForm.waiting_for_text)
async def admin_broadcast_send(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    async with _db_lock:
        user_ids = list(DB["users"].keys())
    sent = 0
    for uid in user_ids:
        try:
            await bot.send_message(int(uid), message.text)
            sent += 1
        except Exception:
            pass
    await message.answer(f"📢 Рассылка завершена. Доставлено: {sent}/{len(user_ids)}.")


@router.callback_query(F.data == "admin:settings")
async def admin_settings_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminSettingsForm.waiting_for_values)
    await callback.message.answer(
        "⚙️ Текущие настройки (меняются до перезапуска процесса, для постоянных "
        "изменений — правь константы вверху файла):\n\n"
        f"MIN_PRICE_PER_SUB={MIN_PRICE_PER_SUB}\n"
        f"DAILY_BONUS_AMOUNT={DAILY_BONUS_AMOUNT}\n"
        f"UNSUBSCRIBE_LOCK_DAYS={UNSUBSCRIBE_LOCK_DAYS}\n"
        f"RESUB_GRACE_HOURS={RESUB_GRACE_HOURS}\n\n"
        "Чтобы изменить на время работы процесса, пришли в этом же формате "
        "(строка на параметр, KEY=значение)."
    )
    await callback.answer()


@router.message(AdminSettingsForm.waiting_for_values)
async def admin_settings_apply(message: Message, state: FSMContext):
    await state.clear()
    global MIN_PRICE_PER_SUB, DAILY_BONUS_AMOUNT, UNSUBSCRIBE_LOCK_DAYS, RESUB_GRACE_HOURS
    applied = []
    for line in message.text.splitlines():
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not val.lstrip("-").isdigit():
            continue
        value = int(val)
        if key == "MIN_PRICE_PER_SUB":
            MIN_PRICE_PER_SUB = value
        elif key == "DAILY_BONUS_AMOUNT":
            DAILY_BONUS_AMOUNT = value
        elif key == "UNSUBSCRIBE_LOCK_DAYS":
            UNSUBSCRIBE_LOCK_DAYS = value
        elif key == "RESUB_GRACE_HOURS":
            RESUB_GRACE_HOURS = value
        else:
            continue
        applied.append(f"{key}={value}")
    if not applied:
        await message.answer("⚠️ Не распознал ни одной настройки.")
        return
    await message.answer("✅ Применено:\n" + "\n".join(applied))


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


# ============================================================================
# ===== БЛОК: СОБЫТИЯ ГРУПП (мгновенная реакция на отписку/удаление бота) =====
# ============================================================================

@group_router.message(Command("setsupport"))
async def set_support_chat(message: Message):
    """Отправь эту команду прямо в группе/супергруппе, которую хочешь сделать
    чатом поддержки — команда сработает только у админа бота (см. ADMIN_IDS)."""
    if not is_admin(message.from_user.id):
        return
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("⚠️ Эту команду нужно отправить в групповом чате.")
        return
    async with _db_lock:
        DB["meta"]["support_chat_id"] = message.chat.id
        await _save_db()
    await message.reply(
        f"✅ Этот чат назначен чатом поддержки (ID: <code>{message.chat.id}</code>).\n"
        f"Теперь сюда будут приходить обращения игроков — отвечай на них ответом (reply)."
    )


@group_router.chat_member()
async def on_member_status_changed(event: ChatMemberUpdated, bot: Bot):
    new_status = event.new_chat_member.status
    user_id = event.new_chat_member.user.id
    chat_id = event.chat.id

    if user_id == bot.id:
        if new_status not in ("administrator", "creator"):
            async with _db_lock:
                changed = False
                for t in DB["tasks"].values():
                    if t["chat_id"] == chat_id and t["is_active"]:
                        t["is_active"] = False
                        changed = True
                if changed:
                    await _save_db()
        return

    if new_status not in ("left", "kicked"):
        return
    async with _db_lock:
        matching_tasks = [dict(t) for t in DB["tasks"].values() if t["chat_id"] == chat_id]
    for task in matching_tasks:
        await start_unsub_grace(bot, task, user_id)


# ============================================================================
# ===== БЛОК: ВЕБ-СЕРВЕР ДЛЯ RENDER (health-check + удержание "живым") ========
# Render free-плану для Web Service нужен открытый порт — без него сервис
# считается упавшим. Health-check заодно используется внешним пингером
# (см. README), чтобы бесплатный инстанс не засыпал по неактивности.
# ============================================================================

async def _health(_request):
    return web.Response(text=f"{BOT_DISPLAY_NAME} is alive")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Health-check веб-сервер слушает порт %s", PORT)


# ============================================================================
# ===== БЛОК: ЗАПУСК БОТА ======================================================
# ============================================================================

async def main():
    if not BOT_TOKEN or "ВСТАВЬ" in BOT_TOKEN:
        raise RuntimeError("Впиши свой BOT_TOKEN (переменная окружения BOT_TOKEN или константа вверху файла).")

    await init_db()
    await start_web_server()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(group_router)
    dp.include_router(support_router)
    dp.include_router(router)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(recheck_subscriptions, "interval", seconds=SUBSCRIPTION_RECHECK_INTERVAL, args=[bot])
    scheduler.start()

    storage_desc = "Postgres" if DATABASE_URL else f"JSON ({DB_FILE}, НЕ переживёт рестарт на Render!)"
    logger.info("%s bot starting (polling, storage: %s)...", BOT_DISPLAY_NAME, storage_desc)
    await bot.delete_webhook(drop_pending_updates=True)
    allowed_updates = dp.resolve_used_update_types() + ["chat_member"]
    await dp.start_polling(bot, allowed_updates=allowed_updates)


if __name__ == "__main__":
    asyncio.run(main())
