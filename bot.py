import asyncio
import html
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ButtonStyle, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config


# ============================================================
# BASIC SETTINGS — config.py is NOT changed.
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vpn_bot")

BOT_TOKEN = config.BOT_TOKEN
ADMIN_ID = int(config.ADMIN_ID)
DB_PATH = getattr(config, "DATABASE", "vpn_bot.db")
BOT_NAME = getattr(config, "BOT_NAME", "VPN SHOP")

# Реквизиты оплаты по твоему запросу. config.py не меняем.
PAYMENT_DEFAULT = (
    "💳 <b>Оплата подписки</b>\n\n"
    "🏦 Банк: <b>Сбер</b>\n"
    "💳 Карта: <code>2202209226426665</code>\n"
    "👤 Получатель: <b>Артем</b>\n\n"
    "💰 К оплате: <b>{price} ₽</b>\n"
    "📦 Тариф: <b>{tariff}</b>\n"
    "📅 Срок: <b>{days} дней</b>\n\n"
    "После оплаты нажмите «✅ Я оплатил» и отправьте чек."
)

HAPP_IOS = "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"
HAPP_ANDROID = "https://play.google.com/store/apps/details?id=com.happproxy"

LOCAL_TZ = timezone(timedelta(hours=4), name="Asia/Yerevan")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# SQLite is intentionally simple: the whole project stays in one bot.py + config.py.
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

def db_execute(query: str, params=()):
    cur = db.execute(query, params)
    db.commit()
    return cur


def db_one(query: str, params=()):
    return db.execute(query, params).fetchone()


def db_all(query: str, params=()):
    return db.execute(query, params).fetchall()


def ensure_column(table: str, column: str, definition: str):
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        db.commit()


# ============================================================
# DATABASE INIT / MIGRATION
# ============================================================


db.execute(
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        orders_count INTEGER NOT NULL DEFAULT 0,
        total_spent REAL NOT NULL DEFAULT 0,
        blocked INTEGER NOT NULL DEFAULT 0
    )
    """
)

db.execute(
    """
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        tariff TEXT NOT NULL,
        price INTEGER NOT NULL,
        days INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'waiting_payment',
        receipt_file_id TEXT,
        vpn_key TEXT,
        created_at TEXT,
        issued_at TEXT,
        expires_at TEXT,
        receipt_type TEXT DEFAULT 'photo'
    )
    """
)

ensure_column("orders", "created_at", "TEXT")
ensure_column("orders", "issued_at", "TEXT")
ensure_column("orders", "expires_at", "TEXT")
ensure_column("orders", "receipt_type", "TEXT DEFAULT 'photo'")


db.execute(
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """
)

db.execute(
    """
    CREATE TABLE IF NOT EXISTS tariffs (
        tariff_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        price INTEGER NOT NULL,
        days INTEGER NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 0
    )
    """
)

db.execute(
    """
    CREATE TABLE IF NOT EXISTS whitelist (
        user_id INTEGER PRIMARY KEY,
        note TEXT DEFAULT '',
        added_at TEXT NOT NULL
    )
    """
)

db.commit()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def local_date(value: Optional[str]) -> str:
    dt = parse_dt(value)
    if not dt:
        return "—"
    return dt.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")


def seed_defaults():
    defaults = {
        "welcome": getattr(config, "WELCOME_TEXT", f"👋 Добро пожаловать в {BOT_NAME}!\n\nВыберите действие ниже."),
        "payment": PAYMENT_DEFAULT,
        "support": "💬 <b>Поддержка</b>\n\nПо вопросам оплаты и подключения напишите администратору.",
        "instruction": (
            "📖 <b>Как подключиться через Happ</b>\n\n"
            "1️⃣ Установите Happ.\n"
            "2️⃣ Скопируйте выданный ключ целиком.\n"
            "3️⃣ Откройте Happ и добавьте конфигурацию из буфера обмена.\n"
            "4️⃣ Выберите добавленный профиль.\n"
            "5️⃣ Нажмите подключение и разрешите VPN-профиль, если система попросит.\n\n"
            "Если ключ начинается с vless://, vmess://, trojan:// или похожей ссылки, не изменяйте её при копировании."
        ),
        "delivery": (
            "🎉 <b>VPN готов!</b>\n\n"
            "📦 Тариф: <b>{tariff}</b>\n"
            "📅 Срок: <b>{days} дней</b>\n"
            "📆 Действует до: <b>{expires}</b>\n\n"
            "🔐 <b>Ваш ключ:</b>\n\n"
            "<code>{key}</code>\n\n"
            "Скопируйте ключ целиком и добавьте его в Happ.\n\n"
            "📱 Ниже — прямые ссылки на Happ для iPhone и Android."
        ),
        "whitelist_info": (
            "🤍 <b>Белый список</b>\n\n"
            "Здесь администратор хранит пользователей, которым доверяет и которых хочет отдельно учитывать.\n\n"
            "Белый список сам по себе не выдаёт VPN и не заменяет оплату — это внутренний список бота."
        ),
    }
    for key, value in defaults.items():
        db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))

    tariffs = getattr(config, "TARIFFS", {})
    for index, (tariff_id, tariff) in enumerate(tariffs.items()):
        db.execute(
            """
            INSERT OR IGNORE INTO tariffs
                (tariff_id, name, price, days, description, enabled, sort_order)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (
                str(tariff_id),
                str(tariff.get("name", tariff_id)),
                int(tariff.get("price", 0)),
                int(tariff.get("days", 30)),
                str(tariff.get("description", "")),
                index,
            ),
        )
    db.commit()


seed_defaults()

# Миграция реквизитов: один раз переводим старую БД на актуальные реквизиты.
if not db_one("SELECT 1 FROM settings WHERE key = ?", ("payment_requisites_v2",)):
    set_payment = PAYMENT_DEFAULT
    db_execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", ("payment", set_payment))
    db_execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", ("payment_requisites_v2", "1"))


def setting(key: str) -> str:
    row = db_one("SELECT value FROM settings WHERE key = ?", (key,))
    if row:
        return row["value"]
    return ""


def set_setting(key: str, value: str):
    db_execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, value))


def format_payment(template: str, tariff: str, price: int, days: int) -> str:
    replacements = {
        "{tariff}": html.escape(str(tariff)),
        "{price}": str(price),
        "{days}": str(days),
    }
    result = template
    for needle, value in replacements.items():
        result = result.replace(needle, value)
    return result


def get_tariffs(enabled_only=True):
    if enabled_only:
        return db_all("SELECT * FROM tariffs WHERE enabled=1 ORDER BY sort_order, tariff_id")
    return db_all("SELECT * FROM tariffs ORDER BY sort_order, tariff_id")


def get_tariff(tariff_id: str):
    return db_one("SELECT * FROM tariffs WHERE tariff_id = ?", (tariff_id,))


def touch_user(user):
    now = iso(now_utc())
    existing = db_one("SELECT user_id FROM users WHERE user_id = ?", (user.id,))
    username = user.username or ""
    first_name = user.first_name or ""
    if existing:
        db_execute(
            """
            UPDATE users
            SET username=?, first_name=?, last_seen=?, blocked=0
            WHERE user_id=?
            """,
            (username, first_name, now, user.id),
        )
    else:
        db_execute(
            """
            INSERT INTO users(user_id, username, first_name, first_seen, last_seen)
            VALUES(?, ?, ?, ?, ?)
            """,
            (user.id, username, first_name, now, now),
        )


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def is_whitelisted(user_id: int) -> bool:
    return db_one("SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,)) is not None


def html_user(user) -> str:
    name = html.escape(user.full_name or "Пользователь")
    username = f"@{html.escape(user.username)}" if user.username else "без username"
    return f"{name} ({username})"


def style_button(text: str, callback_data: str = None, url: str = None, style: str = None):
    kwargs = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style in {"primary", "success", "danger"}:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def back_kb(callback_data="menu"):
    return InlineKeyboardMarkup(
        inline_keyboard=[[style_button("⬅️ Назад", callback_data=callback_data)]]
    )


def main_kb(user_id: int):
    rows = [
        [style_button("🛒 Купить VPN", callback_data="buy", style="success")],
        [style_button("🔐 Моя подписка", callback_data="subscription", style="primary")],
        [style_button("📖 Как подключиться", callback_data="instruction")],
        [style_button("💬 Поддержка", callback_data="support")],
    ]
    if is_admin(user_id):
        rows.append([style_button("⚙️ Админ-панель", callback_data="admin", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tariffs_kb():
    rows = []
    for tariff in get_tariffs(True):
        rows.append([
            style_button(
                f"{tariff['name']} — {tariff['price']}₽",
                callback_data=f"tariff:{tariff['tariff_id']}",
                style="success",
            )
        ])
    rows.append([style_button("⬅️ Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [style_button("✅ Я оплатил", callback_data="paid", style="success")],
            [style_button("⬅️ Назад", callback_data="buy")],
        ]
    )


def happ_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [style_button("🍎 HAPP для iPhone", url=HAPP_IOS, style="primary")],
            [style_button("🤖 HAPP для Android", url=HAPP_ANDROID, style="success")],
            [style_button("📖 Подробная инструкция", callback_data="instruction")],
        ]
    )


def admin_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [style_button("📥 Новые заявки", callback_data="admin:orders", style="success")],
            [style_button("📦 Все заказы", callback_data="admin:history")],
            [style_button("👥 Пользователи", callback_data="admin:users", style="primary")],
            [style_button("📊 Статистика", callback_data="admin:stats", style="primary")],
            [style_button("💰 Тарифы и цены", callback_data="admin:tariffs", style="success")],
            [style_button("📝 Тексты", callback_data="admin:texts")],
            [style_button("🤍 Белый список", callback_data="admin:whitelist")],
            [style_button("📢 Рассылка", callback_data="admin:broadcast", style="danger")],
            [style_button("⚙️ Настройки", callback_data="admin:settings")],
            [style_button("⬅️ В магазин", callback_data="menu")],
        ]
    )


def order_admin_kb(order_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [style_button("🔑 Выдать ключ", callback_data=f"give:{order_id}", style="success")],
            [style_button("❌ Отклонить", callback_data=f"reject:{order_id}", style="danger")],
        ]
    )


def text_admin_kb():
    names = [
        ("welcome", "🏠 Главное сообщение"),
        ("payment", "💳 Реквизиты / оплата"),
        ("instruction", "📖 Инструкция HAPP"),
        ("delivery", "🎁 Сообщение после выдачи"),
        ("support", "💬 Поддержка"),
        ("whitelist_info", "🤍 Описание белого списка"),
    ]
    rows = [[style_button(title, callback_data=f"edittext:{key}")] for key, title in names]
    rows.append([style_button("⬅️ Назад", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_delivery(template: str, tariff: str, days: int, key: str, expires: str, order_id: int) -> str:
    replacements = {
        "{tariff}": html.escape(str(tariff)),
        "{days}": str(days),
        "{key}": html.escape(str(key)),
        "{expires}": html.escape(str(expires)),
        "{order_id}": str(order_id),
    }
    result = template
    for needle, value in replacements.items():
        result = result.replace(needle, value)
    return result


# ============================================================
# FSM
# ============================================================


class UserStates(StatesGroup):
    waiting_receipt = State()


class AdminStates(StatesGroup):
    waiting_key = State()
    waiting_edit_text = State()
    waiting_price = State()
    waiting_name = State()
    waiting_days = State()
    waiting_description = State()
    waiting_add_tariff = State()
    waiting_whitelist_user = State()
    waiting_broadcast = State()
    waiting_support_text = State()


# ============================================================
# HELPERS
# ============================================================


async def safe_edit(callback: CallbackQuery, text: str, reply_markup=None):
    try:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await callback.message.answer(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )


async def admin_only(callback: CallbackQuery) -> bool:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return False
    return True


async def admin_message_only(message: Message) -> bool:
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа.")
        return False
    return True


# ============================================================
# USER MENU
# ============================================================


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    touch_user(message.from_user)
    await message.answer(
        setting("welcome"),
        parse_mode=ParseMode.HTML,
        reply_markup=main_kb(message.from_user.id),
    )


@dp.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    touch_user(callback.from_user)
    await safe_edit(callback, setting("welcome"), main_kb(callback.from_user.id))
    await callback.answer()


@dp.callback_query(F.data == "buy")
async def cb_buy(callback: CallbackQuery):
    text = "🛒 <b>Выберите тариф</b>\n\nНажмите на нужный вариант ниже."
    await safe_edit(callback, text, tariffs_kb())
    await callback.answer()


@dp.callback_query(F.data.startswith("tariff:"))
async def cb_tariff(callback: CallbackQuery, state: FSMContext):
    tariff_id = callback.data.split(":", 1)[1]
    tariff = get_tariff(tariff_id)
    if not tariff or not tariff["enabled"]:
        await callback.answer("Тариф недоступен.", show_alert=True)
        return

    await state.update_data(
        tariff_id=tariff["tariff_id"],
        tariff_name=tariff["name"],
        price=int(tariff["price"]),
        days=int(tariff["days"]),
    )

    description = tariff["description"] or "Без дополнительного описания."
    text = (
        f"🛒 <b>{html.escape(tariff['name'])}</b>\n\n"
        f"{description}\n\n"
        f"💰 Цена: <b>{tariff['price']} ₽</b>\n"
        f"📅 Срок: <b>{tariff['days']} дней</b>\n\n"
        f"{format_payment(setting('payment'), tariff['name'], int(tariff['price']), int(tariff['days']))}"
    )

    await safe_edit(callback, text, payment_kb())
    await callback.answer()


@dp.callback_query(F.data == "paid")
async def cb_paid(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("tariff_id"):
        await callback.answer("Сначала выберите тариф.", show_alert=True)
        return

    await state.set_state(UserStates.waiting_receipt)
    await callback.message.answer(
        "🧾 <b>Отправьте чек об оплате.</b>\n\n"
        "Можно отправить фото или файл с чеком.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("buy"),
    )
    await callback.answer()


@dp.message(UserStates.waiting_receipt, F.photo)
async def receive_receipt_photo(message: Message, state: FSMContext):
    await create_order_from_receipt(message, state, "photo", message.photo[-1].file_id)


@dp.message(UserStates.waiting_receipt, F.document)
async def receive_receipt_document(message: Message, state: FSMContext):
    await create_order_from_receipt(message, state, "document", message.document.file_id)


async def create_order_from_receipt(message: Message, state: FSMContext, receipt_type: str, file_id: str):
    touch_user(message.from_user)
    data = await state.get_data()
    if not data.get("tariff_id"):
        await state.clear()
        await message.answer("❌ Сессия покупки истекла. Начните покупку заново.")
        return

    created = iso(now_utc())
    cursor = db.execute(
        """
        INSERT INTO orders(
            user_id, username, tariff, price, days, status,
            receipt_file_id, created_at, receipt_type
        ) VALUES(?, ?, ?, ?, ?, 'waiting_admin', ?, ?, ?)
        """,
        (
            message.from_user.id,
            message.from_user.username or "",
            data["tariff_name"],
            int(data["price"]),
            int(data["days"]),
            file_id,
            created,
            receipt_type,
        ),
    )
    db.commit()
    order_id = cursor.lastrowid
    db_execute(
        "UPDATE users SET orders_count = orders_count + 1, total_spent = total_spent + ? WHERE user_id = ?",
        (int(data["price"]), message.from_user.id),
    )

    await state.clear()
    await message.answer(
        f"✅ <b>Заявка #{order_id} создана!</b>\n\n"
        "Чек отправлен администратору. После проверки оплаты вам выдадут ключ.",
        parse_mode=ParseMode.HTML,
    )

    username = f"@{html.escape(message.from_user.username)}" if message.from_user.username else "без username"
    admin_text = (
        f"🆕 <b>НОВАЯ ЗАЯВКА #{order_id}</b>\n\n"
        f"👤 {html.escape(message.from_user.full_name)}\n"
        f"🔗 {username}\n"
        f"🆔 <code>{message.from_user.id}</code>\n\n"
        f"📦 {html.escape(data['tariff_name'])}\n"
        f"💰 <b>{data['price']} ₽</b>\n"
        f"📅 {data['days']} дней\n"
        f"⏳ Статус: ожидает проверки"
    )

    if receipt_type == "photo":
        await bot.send_photo(
            ADMIN_ID,
            file_id,
            caption=admin_text,
            parse_mode=ParseMode.HTML,
            reply_markup=order_admin_kb(order_id),
        )
    else:
        await bot.send_document(
            ADMIN_ID,
            file_id,
            caption=admin_text,
            parse_mode=ParseMode.HTML,
            reply_markup=order_admin_kb(order_id),
        )


@dp.message(UserStates.waiting_receipt)
async def wrong_receipt(message: Message):
    await message.answer(
        "❌ Отправьте фото или файл с чеком.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb("buy"),
    )


@dp.callback_query(F.data == "subscription")
async def cb_subscription(callback: CallbackQuery):
    row = db_one(
        """
        SELECT * FROM orders
        WHERE user_id = ? AND status = 'completed'
        ORDER BY COALESCE(expires_at, issued_at, created_at) DESC, id DESC
        LIMIT 1
        """,
        (callback.from_user.id,),
    )
    if not row:
        text = "🔐 <b>Моя подписка</b>\n\nАктивной подписки пока нет."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [style_button("🛒 Купить VPN", callback_data="buy", style="success")],
                [style_button("⬅️ Назад", callback_data="menu")],
            ]
        )
        await safe_edit(callback, text, kb)
        await callback.answer()
        return

    exp = parse_dt(row["expires_at"])
    active = bool(exp and exp > now_utc())
    status = "🟢 Активна" if active else "🔴 Истекла"
    text = (
        f"🔐 <b>Моя подписка</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"📦 {html.escape(row['tariff'])}\n"
        f"📆 До: <b>{local_date(row['expires_at'])}</b>\n\n"
        f"🔑 Ваш ключ:\n<code>{html.escape(row['vpn_key'] or '—')}</code>"
    )
    kb_rows = [
        [style_button("📱 Открыть HAPP", callback_data="happ", style="primary")],
        [style_button("📖 Инструкция", callback_data="instruction")],
        [style_button("🛒 Купить ещё", callback_data="buy", style="success")],
        [style_button("⬅️ Назад", callback_data="menu")],
    ]
    await safe_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await callback.answer()


@dp.callback_query(F.data == "happ")
async def cb_happ(callback: CallbackQuery):
    text = (
        "📱 <b>HAPP</b>\n\n"
        "Выберите магазин:"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [style_button("🍎 iPhone / iOS", url=HAPP_IOS, style="primary")],
            [style_button("🤖 Android", url=HAPP_ANDROID, style="success")],
            [style_button("⬅️ Назад", callback_data="subscription")],
        ]
    )
    await safe_edit(callback, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "instruction")
async def cb_instruction(callback: CallbackQuery):
    text = (
        f"{setting('instruction')}\n\n"
        "📌 <b>Если ключ не добавляется</b>\n"
        "• Проверьте, что скопировали его целиком.\n"
        "• Не добавляйте лишние пробелы или переносы.\n"
        "• Убедитесь, что используете актуальную версию Happ.\n"
        "• Если проблема остаётся, отправьте скриншот в поддержку."
    )
    await safe_edit(callback, text, happ_kb())
    await callback.answer()


@dp.callback_query(F.data == "support")
async def cb_support(callback: CallbackQuery):
    await safe_edit(callback, setting("support"), back_kb("menu"))
    await callback.answer()


# ============================================================
# ADMIN PANEL
# ============================================================


@dp.callback_query(F.data == "admin")
async def cb_admin(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    stats = db_one("SELECT COUNT(*) AS c FROM orders WHERE status='waiting_admin'")
    await safe_edit(
        callback,
        f"⚙️ <b>Админ-панель</b>\n\n📥 Новых заявок: <b>{stats['c']}</b>",
        admin_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    users = db_one("SELECT COUNT(*) AS c FROM users")["c"]
    orders = db_one("SELECT COUNT(*) AS c FROM orders")["c"]
    pending = db_one("SELECT COUNT(*) AS c FROM orders WHERE status='waiting_admin'")["c"]
    completed = db_one("SELECT COUNT(*) AS c FROM orders WHERE status='completed'")["c"]
    rejected = db_one("SELECT COUNT(*) AS c FROM orders WHERE status='rejected'")["c"]
    revenue = db_one("SELECT COALESCE(SUM(price), 0) AS s FROM orders WHERE status='completed'")["s"]
    active = db_one(
        "SELECT COUNT(*) AS c FROM orders WHERE status='completed' AND expires_at IS NOT NULL AND expires_at > ?",
        (iso(now_utc()),),
    )["c"]
    white = db_one("SELECT COUNT(*) AS c FROM whitelist")["c"]
    today_start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    today_orders = db_one(
        "SELECT COUNT(*) AS c FROM orders WHERE created_at >= ?",
        (iso(today_start),),
    )["c"]
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"📦 Заказов всего: <b>{orders}</b>\n"
        f"📥 Ожидают: <b>{pending}</b>\n"
        f"✅ Выдано: <b>{completed}</b>\n"
        f"❌ Отклонено: <b>{rejected}</b>\n"
        f"💰 Выручка: <b>{revenue} ₽</b>\n"
        f"🔐 Активных подписок: <b>{active}</b>\n"
        f"🤍 В белом списке: <b>{white}</b>\n"
        f"📅 Заявок сегодня: <b>{today_orders}</b>"
    )
    await safe_edit(callback, text, back_kb("admin"))
    await callback.answer()


@dp.callback_query(F.data == "admin:users")
async def admin_users(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    rows = db_all("SELECT * FROM users ORDER BY last_seen DESC LIMIT 12")
    if not rows:
        text = "👥 <b>Пользователи</b>\n\nПока пользователей нет."
    else:
        parts = ["👥 <b>Последние пользователи</b>"]
        for row in rows:
            uname = f"@{html.escape(row['username'])}" if row["username"] else "без username"
            white = " 🤍" if is_whitelisted(row["user_id"]) else ""
            blocked = " 🚫" if row["blocked"] else ""
            parts.append(
                f"\n🆔 <code>{row['user_id']}</code> — {uname}{white}{blocked}\n"
                f"   заказов: {row['orders_count']} | потрачено: {row['total_spent']:.0f} ₽"
            )
        text = "".join(parts)
    await safe_edit(callback, text, back_kb("admin"))
    await callback.answer()


@dp.callback_query(F.data == "admin:orders")
async def admin_orders(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    rows = db_all(
        "SELECT * FROM orders WHERE status='waiting_admin' ORDER BY id DESC LIMIT 15"
    )
    if not rows:
        text = "📥 <b>Новые заявки</b>\n\n✅ Новых заявок нет."
        await safe_edit(callback, text, back_kb("admin"))
        await callback.answer()
        return

    parts = ["📥 <b>Новые заявки</b>"]
    buttons = []
    for row in rows:
        uname = f"@{html.escape(row['username'])}" if row["username"] else str(row["user_id"])
        parts.append(
            f"\n<b>#{row['id']}</b> • {uname} • {html.escape(row['tariff'])} • <b>{row['price']} ₽</b>"
        )
        buttons.append([
            style_button(f"#{row['id']} — открыть", callback_data=f"order:view:{row['id']}", style="primary")
        ])
    buttons.append([style_button("⬅️ Назад", callback_data="admin")])
    await safe_edit(callback, "\n".join(parts), InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@dp.callback_query(F.data.startswith("order:view:"))
async def admin_order_view(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    order_id = int(callback.data.rsplit(":", 1)[1])
    row = db_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if not row:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    uname = f"@{html.escape(row['username'])}" if row["username"] else "без username"
    text = (
        f"📦 <b>Заявка #{row['id']}</b>\n\n"
        f"👤 {uname}\n"
        f"🆔 <code>{row['user_id']}</code>\n"
        f"📦 {html.escape(row['tariff'])}\n"
        f"💰 <b>{row['price']} ₽</b>\n"
        f"📅 {row['days']} дней\n"
        f"📌 Статус: <b>{html.escape(row['status'])}</b>\n"
        f"🕒 Создана: {local_date(row['created_at'])}"
    )

    buttons = []
    if row["status"] == "waiting_admin":
        buttons.append([style_button("🔑 Выдать ключ", callback_data=f"give:{order_id}", style="success")])
        buttons.append([style_button("❌ Отклонить", callback_data=f"reject:{order_id}", style="danger")])
    elif row["status"] == "completed":
        buttons.append([style_button("🔁 Отправить ключ ещё раз", callback_data=f"resend:{order_id}", style="primary")])
    buttons.append([style_button("⬅️ Назад", callback_data="admin:orders" if row["status"] == "waiting_admin" else "admin:history")])
    await safe_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@dp.callback_query(F.data == "admin:history")
async def admin_history(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    rows = db_all("SELECT * FROM orders ORDER BY id DESC LIMIT 20")
    if not rows:
        text = "📦 <b>Все заказы</b>\n\nПока заказов нет."
        await safe_edit(callback, text, back_kb("admin"))
        await callback.answer()
        return
    parts = ["📦 <b>Последние заказы</b>"]
    buttons = []
    icons = {"completed": "✅", "waiting_admin": "⏳", "rejected": "❌", "issuing": "🔑"}
    for row in rows:
        icon = icons.get(row["status"], "•")
        parts.append(
            f"\n{icon} <b>#{row['id']}</b> • {html.escape(row['tariff'])} • {row['price']} ₽"
        )
        buttons.append([
            style_button(f"#{row['id']}", callback_data=f"order:view:{row['id']}")
        ])
    buttons.append([style_button("⬅️ Назад", callback_data="admin")])
    await safe_edit(callback, "\n".join(parts), InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


# ============================================================
# GIVE / REJECT / RESEND
# ============================================================


@dp.callback_query(F.data.startswith("give:"))
async def admin_give(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    order_id = int(callback.data.split(":", 1)[1])
    row = db_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if not row:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if row["status"] not in {"waiting_admin", "issuing"}:
        await callback.answer("Эта заявка уже обработана.", show_alert=True)
        return

    db_execute("UPDATE orders SET status='issuing' WHERE id=?", (order_id,))
    await state.set_state(AdminStates.waiting_key)
    await state.update_data(order_id=order_id)
    await callback.message.answer(
        f"🔑 <b>Выдача ключа для заявки #{order_id}</b>\n\n"
        "Купи ключ у поставщика и отправь его сюда одним сообщением.\n\n"
        "Можно отправить VLESS / VMess / Trojan / Shadowsocks или другой ключ, который поддерживает Happ.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_key)
async def admin_receive_key(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return
    if not message.text:
        await message.answer("❌ Отправь ключ обычным текстовым сообщением.")
        return

    data = await state.get_data()
    order_id = data.get("order_id")
    row = db_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if not row:
        await state.clear()
        await message.answer("❌ Заявка не найдена.")
        return

    key = message.text.strip()
    now = now_utc()

    # Renewal extends an already active subscription instead of resetting the date.
    previous = db_one(
        """
        SELECT expires_at FROM orders
        WHERE user_id=? AND status='completed' AND expires_at IS NOT NULL AND expires_at > ?
        ORDER BY expires_at DESC LIMIT 1
        """,
        (row["user_id"], iso(now)),
    )
    base = parse_dt(previous["expires_at"]) if previous else None
    if not base or base < now:
        base = now
    expires = base + timedelta(days=int(row["days"]))

    db_execute(
        """
        UPDATE orders
        SET status='completed', vpn_key=?, issued_at=?, expires_at=?
        WHERE id=?
        """,
        (key, iso(now), iso(expires), order_id),
    )

    delivery = format_delivery(
        setting("delivery"),
        row["tariff"],
        int(row["days"]),
        key,
        local_date(iso(expires)),
        order_id,
    )

    try:
        await bot.send_message(
            row["user_id"],
            delivery,
            parse_mode=ParseMode.HTML,
            reply_markup=happ_kb(),
        )
        sent = True
    except TelegramForbiddenError:
        db_execute("UPDATE users SET blocked=1 WHERE user_id=?", (row["user_id"],))
        sent = False
    except Exception as exc:
        logger.exception("delivery failed: %s", exc)
        sent = False

    await state.clear()
    status_line = "✅ Ключ отправлен клиенту." if sent else "⚠️ Ключ сохранён, но клиент недоступен для отправки."
    await message.answer(
        f"{status_line}\n\n"
        f"📦 Заявка: <b>#{order_id}</b>\n"
        f"📅 До: <b>{local_date(iso(expires))}</b>",
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(F.data.startswith("reject:"))
async def admin_reject(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    order_id = int(callback.data.split(":", 1)[1])
    row = db_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if not row:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if row["status"] not in {"waiting_admin", "issuing"}:
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return

    db_execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
    try:
        await bot.send_message(
            row["user_id"],
            f"❌ <b>Заявка #{order_id} отклонена.</b>\n\nЕсли оплата была отправлена, обратитесь в поддержку.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramForbiddenError:
        db_execute("UPDATE users SET blocked=1 WHERE user_id=?", (row["user_id"],))
    except Exception:
        pass

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(f"❌ Заявка #{order_id} отклонена.")
    await callback.answer()


@dp.callback_query(F.data.startswith("resend:"))
async def admin_resend(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    order_id = int(callback.data.split(":", 1)[1])
    row = db_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if not row or row["status"] != "completed" or not row["vpn_key"]:
        await callback.answer("Ключ не найден.", show_alert=True)
        return

    delivery = format_delivery(
        setting("delivery"),
        row["tariff"],
        int(row["days"]),
        row["vpn_key"],
        local_date(row["expires_at"]),
        order_id,
    )
    try:
        await bot.send_message(
            row["user_id"],
            "🔁 <b>Повторная отправка ключа</b>\n\n" + delivery,
            parse_mode=ParseMode.HTML,
            reply_markup=happ_kb(),
        )
        await callback.answer("Ключ отправлен.")
    except Exception as exc:
        await callback.answer("Не удалось отправить ключ.", show_alert=True)
        logger.exception(exc)


# ============================================================
# ADMIN — TARIFFS
# ============================================================


@dp.callback_query(F.data == "admin:tariffs")
async def admin_tariffs(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    rows = get_tariffs(False)
    buttons = []
    for row in rows:
        state = "🟢" if row["enabled"] else "🔴"
        buttons.append([
            style_button(
                f"{state} {row['name']} — {row['price']}₽",
                callback_data=f"edittariff:{row['tariff_id']}",
            )
        ])
    buttons.append([style_button("➕ Добавить тариф", callback_data="addtariff", style="success")])
    buttons.append([style_button("⬅️ Назад", callback_data="admin")])
    await safe_edit(callback, "💰 <b>Тарифы и цены</b>\n\nНажми на тариф для редактирования.", InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@dp.callback_query(F.data.startswith("edittariff:"))
async def admin_edit_tariff_menu(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    tariff_id = callback.data.split(":", 1)[1]
    row = get_tariff(tariff_id)
    if not row:
        await callback.answer("Тариф не найден.", show_alert=True)
        return
    enabled = "🟢 включён" if row["enabled"] else "🔴 выключен"
    text = (
        f"💰 <b>{html.escape(row['name'])}</b>\n\n"
        f"ID: <code>{html.escape(row['tariff_id'])}</code>\n"
        f"Цена: <b>{row['price']} ₽</b>\n"
        f"Срок: <b>{row['days']} дней</b>\n"
        f"Статус: {enabled}\n\n"
        f"Описание:\n{row['description'] or '—'}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [style_button("💰 Изменить цену", callback_data=f"price:{tariff_id}", style="success")],
            [style_button("📝 Изменить название", callback_data=f"name:{tariff_id}")],
            [style_button("📅 Изменить срок", callback_data=f"days:{tariff_id}")],
            [style_button("📖 Изменить описание", callback_data=f"desc:{tariff_id}")],
            [style_button("🔄 Вкл / выкл", callback_data=f"toggle:{tariff_id}")],
            [style_button("⬅️ Назад", callback_data="admin:tariffs")],
        ]
    )
    await safe_edit(callback, text, kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("price:"))
async def admin_price_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    tariff_id = callback.data.split(":", 1)[1]
    await state.set_state(AdminStates.waiting_price)
    await state.update_data(tariff_id=tariff_id)
    await callback.message.answer("💰 Отправь новую цену одним числом, например: <code>199</code>", parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.message(AdminStates.waiting_price)
async def admin_price_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return
    try:
        price = int(message.text.strip())
        if price < 0:
            raise ValueError
    except Exception:
        await message.answer("❌ Цена должна быть целым числом от 0 и выше.")
        return
    data = await state.get_data()
    db_execute("UPDATE tariffs SET price=? WHERE tariff_id=?", (price, data["tariff_id"]))
    await state.clear()
    await message.answer(f"✅ Цена изменена: <b>{price} ₽</b>", parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("name:"))
async def admin_name_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    tariff_id = callback.data.split(":", 1)[1]
    await state.set_state(AdminStates.waiting_name)
    await state.update_data(tariff_id=tariff_id)
    await callback.message.answer("📝 Отправь новое название тарифа.")
    await callback.answer()


@dp.message(AdminStates.waiting_name)
async def admin_name_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Название пустое.")
        return
    data = await state.get_data()
    db_execute("UPDATE tariffs SET name=? WHERE tariff_id=?", (name[:80], data["tariff_id"]))
    await state.clear()
    await message.answer("✅ Название изменено.")


@dp.callback_query(F.data.startswith("days:"))
async def admin_days_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    tariff_id = callback.data.split(":", 1)[1]
    await state.set_state(AdminStates.waiting_days)
    await state.update_data(tariff_id=tariff_id)
    await callback.message.answer("📅 Отправь новый срок в днях, например: <code>30</code>", parse_mode=ParseMode.HTML)
    await callback.answer()


@dp.message(AdminStates.waiting_days)
async def admin_days_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return
    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError
    except Exception:
        await message.answer("❌ Срок должен быть положительным целым числом.")
        return
    data = await state.get_data()
    db_execute("UPDATE tariffs SET days=? WHERE tariff_id=?", (days, data["tariff_id"]))
    await state.clear()
    await message.answer(f"✅ Срок изменён: <b>{days} дней</b>", parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("desc:"))
async def admin_desc_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    tariff_id = callback.data.split(":", 1)[1]
    await state.set_state(AdminStates.waiting_description)
    await state.update_data(tariff_id=tariff_id)
    await callback.message.answer(
        "📖 Отправь новое описание тарифа.\n\n"
        "<b>Жирный</b>, <i>курсив</i>, ссылки и другое форматирование Telegram сохранится.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_description)
async def admin_desc_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return
    text = message.html_text or html.escape(message.text or "")
    data = await state.get_data()
    db_execute("UPDATE tariffs SET description=? WHERE tariff_id=?", (text, data["tariff_id"]))
    await state.clear()
    await message.answer("✅ Описание сохранено вместе с форматированием.", parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("toggle:"))
async def admin_toggle_tariff(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    tariff_id = callback.data.split(":", 1)[1]
    row = get_tariff(tariff_id)
    if not row:
        await callback.answer("Не найден.", show_alert=True)
        return
    db_execute("UPDATE tariffs SET enabled=? WHERE tariff_id=?", (0 if row["enabled"] else 1, tariff_id))
    await admin_edit_tariff_menu(callback)


@dp.callback_query(F.data == "addtariff")
async def admin_add_tariff_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.set_state(AdminStates.waiting_add_tariff)
    await callback.message.answer(
        "➕ <b>Новый тариф</b>\n\n"
        "Отправь 4 строки:\n"
        "<code>id\nназвание\nцена\nдни</code>\n\n"
        "Пример:\n"
        "<code>vpn_180\nVPN на 180 дней\n799\n180</code>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_add_tariff)
async def admin_add_tariff_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return
    lines = [x.strip() for x in (message.text or "").splitlines() if x.strip()]
    if len(lines) != 4:
        await message.answer("❌ Нужно ровно 4 строки: id, название, цена, дни.")
        return
    tariff_id, name, price_raw, days_raw = lines
    try:
        price = int(price_raw)
        days = int(days_raw)
        if price < 0 or days <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Цена должна быть ≥ 0, срок — > 0.")
        return
    if get_tariff(tariff_id):
        await message.answer("❌ Такой ID уже существует.")
        return
    max_order = db_one("SELECT COALESCE(MAX(sort_order), 0) AS m FROM tariffs")["m"]
    db_execute(
        "INSERT INTO tariffs(tariff_id,name,price,days,description,enabled,sort_order) VALUES(?,?,?,?,?,1,?)",
        (tariff_id, name, price, days, "", max_order + 1),
    )
    await state.clear()
    await message.answer("✅ Новый тариф создан. Описание можно добавить в админке.")


# ============================================================
# ADMIN — TEXTS / FORMATTING
# ============================================================


@dp.callback_query(F.data == "admin:texts")
async def admin_texts(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    await safe_edit(
        callback,
        "📝 <b>Тексты</b>\n\n"
        "Нажми на раздел и отправь новый текст.\n"
        "HTML-разметка Telegram будет сохранена.",
        text_admin_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("edittext:"))
async def admin_edittext_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    key = callback.data.split(":", 1)[1]
    titles = {
        "welcome": "главного сообщения",
        "payment": "реквизитов / оплаты",
        "instruction": "инструкции HAPP",
        "delivery": "сообщения после выдачи",
        "support": "поддержки",
        "whitelist_info": "описания белого списка",
    }
    if key not in titles:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return

    hint = ""
    if key == "delivery":
        hint = (
            "\n\n<b>Доступные переменные:</b>\n"
            "<code>{tariff}</code> — тариф\n"
            "<code>{days}</code> — срок\n"
            "<code>{key}</code> — ключ\n"
            "<code>{expires}</code> — дата окончания\n"
            "<code>{order_id}</code> — номер заявки"
        )
    elif key == "payment":
        hint = (
            "\n\n<b>Чтобы цена всегда была актуальной, используй переменные:</b>\n"
            "<code>{price}</code> — текущая цена выбранного тарифа\n"
            "<code>{tariff}</code> — название выбранного тарифа\n"
            "<code>{days}</code> — срок тарифа"
        )
    await state.set_state(AdminStates.waiting_edit_text)
    await state.update_data(edit_key=key)
    await callback.message.answer(
        f"📝 Отправь новый текст {titles[key]}.\n"
        "Форматирование Telegram сохранится: жирный, курсив, ссылки, спойлеры и т.д." + hint,
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_edit_text)
async def admin_edittext_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return
    data = await state.get_data()
    key = data.get("edit_key")
    raw = message.html_text
    if raw is None:
        raw = html.escape(message.text or "")
    if not raw.strip():
        await message.answer("❌ Текст пустой.")
        return
    set_setting(key, raw)
    await state.clear()
    await message.answer(
        "✅ Текст сохранён. Telegram-форматирование тоже сохранено.",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# ADMIN — WHITELIST
# ============================================================


@dp.callback_query(F.data == "admin:whitelist")
async def admin_whitelist(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    rows = db_all("SELECT * FROM whitelist ORDER BY added_at DESC LIMIT 30")
    parts = [setting("whitelist_info"), "", f"👥 Записей: <b>{len(rows)}</b>"]
    buttons = [[style_button("➕ Добавить пользователя", callback_data="white:add", style="success")]]
    for row in rows:
        note = f" — {html.escape(row['note'])}" if row["note"] else ""
        parts.append(f"\n🤍 <code>{row['user_id']}</code>{note}")
        buttons.append([style_button(f"❌ Удалить {row['user_id']}", callback_data=f"white:del:{row['user_id']}", style="danger")])
    buttons.append([style_button("⬅️ Назад", callback_data="admin")])
    await safe_edit(callback, "\n".join(parts), InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@dp.callback_query(F.data == "white:add")
async def admin_whitelist_add_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.set_state(AdminStates.waiting_whitelist_user)
    await callback.message.answer(
        "🤍 Отправь Telegram ID пользователя.\n\nНапример: <code>123456789</code>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_whitelist_user)
async def admin_whitelist_add_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return
    try:
        user_id = int((message.text or "").strip())
        if user_id <= 0:
            raise ValueError
    except Exception:
        await message.answer("❌ Нужен корректный числовой Telegram ID.")
        return
    db_execute(
        "INSERT OR REPLACE INTO whitelist(user_id,note,added_at) VALUES(?,?,?)",
        (user_id, "", iso(now_utc())),
    )
    await state.clear()
    await message.answer(f"✅ Пользователь <code>{user_id}</code> добавлен в белый список.", parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("white:del:"))
async def admin_whitelist_delete(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    user_id = int(callback.data.rsplit(":", 1)[1])
    db_execute("DELETE FROM whitelist WHERE user_id=?", (user_id,))
    await admin_whitelist(callback)


# ============================================================
# ADMIN — BROADCAST
# ============================================================


@dp.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.set_state(AdminStates.waiting_broadcast)
    await callback.message.answer(
        "📢 <b>Рассылка</b>\n\n"
        "Отправь одним сообщением текст для рассылки.\n"
        "Жирный, курсив, ссылки, спойлеры и другое форматирование сохранятся.\n\n"
        "Перед отправкой я покажу предпросмотр.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_broadcast)
async def admin_broadcast_receive(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return
    if not message.text:
        await message.answer("❌ Сейчас поддерживается текстовая рассылка. Отправь текстовым сообщением.")
        return
    formatted = message.html_text or html.escape(message.text)
    await state.update_data(broadcast_text=formatted)

    preview_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [style_button("🚀 Отправить всем", callback_data="broadcast:send", style="danger")],
            [style_button("❌ Отмена", callback_data="broadcast:cancel", style="danger")],
        ]
    )
    await message.answer(
        "👀 <b>Предпросмотр рассылки:</b>\n\n" + formatted,
        parse_mode=ParseMode.HTML,
        reply_markup=preview_kb,
    )


@dp.callback_query(F.data == "broadcast:cancel")
async def admin_broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.clear()
    await callback.message.answer("❌ Рассылка отменена.")
    await callback.answer()


@dp.callback_query(F.data == "broadcast:send")
async def admin_broadcast_send(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    if not text:
        await state.clear()
        await callback.answer("Рассылка уже завершена.", show_alert=True)
        return

    users = db_all("SELECT user_id FROM users WHERE blocked=0")
    sent = 0
    failed = 0
    await callback.message.answer(f"📢 Начинаю рассылку по <b>{len(users)}</b> пользователям...", parse_mode=ParseMode.HTML)

    for row in users:
        try:
            await bot.send_message(row["user_id"], text, parse_mode=ParseMode.HTML)
            sent += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.2)
            try:
                await bot.send_message(row["user_id"], text, parse_mode=ParseMode.HTML)
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            failed += 1
            db_execute("UPDATE users SET blocked=1 WHERE user_id=?", (row["user_id"],))
        except Exception:
            failed += 1

    await state.clear()
    await callback.message.answer(
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"📨 Отправлено: <b>{sent}</b>\n"
        f"⚠️ Не доставлено: <b>{failed}</b>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


# ============================================================
# ADMIN — SETTINGS
# ============================================================


@dp.callback_query(F.data == "admin:settings")
async def admin_settings(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    pending = db_one("SELECT COUNT(*) AS c FROM orders WHERE status='waiting_admin'")["c"]
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"🤖 Название: <b>{html.escape(BOT_NAME)}</b>\n"
        f"🆔 Admin ID: <code>{ADMIN_ID}</code>\n"
        f"📥 Новых заявок: <b>{pending}</b>\n\n"
        "Реквизиты оплаты, сообщения, тарифы и белый список редактируются в отдельных разделах.\n\n"
        "📱 HAPP:\n"
        "iPhone — прямая ссылка App Store\n"
        "Android — прямая ссылка Google Play"
    )
    await safe_edit(callback, text, back_kb("admin"))
    await callback.answer()


# ============================================================
# FALLBACK: keep bot usable after cancelled states
# ============================================================


@dp.message()
async def fallback(message: Message):
    touch_user(message.from_user)
    await message.answer(
        "Выберите действие в меню 👇",
        reply_markup=main_kb(message.from_user.id),
    )


# ============================================================
# START POLLING — NO main() FUNCTION
# ============================================================

asyncio.run(dp.start_polling(bot))
