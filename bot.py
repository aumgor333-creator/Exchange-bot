"""
Телеграм-бот обмена валют (Дананг / Нячанг / Фукуок).
Логика полностью повторяет предыдущий сценарий в Make, но без задержек
и без ограничений no-code платформы.

Настройка — см. README.md.
"""

import hashlib
import hmac
import html
import logging
import os
import re
from datetime import datetime

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

import sheets

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# НАСТРОЙКИ (берутся из переменных окружения — задаются в Railway)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MANAGER_CHAT_ID = os.environ["MANAGER_CHAT_ID"]  # куда падает заявка

CITIES = ["Дананг", "Нячанг", "Фукуок"]
CURRENCIES = {"RUB": "RUB/VND", "USD": "USD/VND", "USDT": "USDT/VND"}

AMOUNT_PRESETS = {
    "RUB/VND": ["10 000", "30 000", "50 000", "100 000"],
    "USD/VND": ["100", "300", "500", "1 000"],
    "USDT/VND": ["100", "300", "500", "1 000"],
}

CURRENCY_LABEL = {"RUB/VND": "рублей", "USD/VND": "долларов", "USDT/VND": "USDT"}
CITY_PHRASE = {"Дананг": "в Дананге", "Нячанг": "в Нячанге", "Фукуок": "на Фукуоке"}


# ---------------------------------------------------------------------------
# ПАРСИНГ СУММЫ
# ---------------------------------------------------------------------------
def parse_amount(text: str):
    """Достаёт число из текста вроде '30 000', '40тыс', '1000usd'."""
    if not text:
        return None
    t = text.lower().replace(" ", "").replace(",", ".")
    is_thousands = "тыс" in t
    t = re.sub(r"[^\d.]", "", t.replace("тыс", ""))
    if not t:
        return None
    try:
        value = float(t)
    except ValueError:
        return None
    if is_thousands:
        value *= 1000
    return value


def resolve_pair(text: str):
    """Определяет валютную пару по ключевым словам в тексте."""
    if not text:
        return None
    t = text.lower()
    if "usdt" in t or "тезер" in t:
        return "USDT/VND"
    if "usd" in t or "доллар" in t:
        return "USD/VND"
    if "rub" in t or "руб" in t or "тыс" in t:
        return "RUB/VND"
    return None


# ---------------------------------------------------------------------------
# КЛАВИАТУРЫ
# ---------------------------------------------------------------------------
def city_keyboard():
    return ReplyKeyboardMarkup(
        [CITIES, ["Другой"]], resize_keyboard=True, one_time_keyboard=True
    )


def currency_keyboard():
    return ReplyKeyboardMarkup(
        [list(CURRENCIES.keys())], resize_keyboard=True, one_time_keyboard=True
    )


def amount_keyboard(pair: str):
    presets = AMOUNT_PRESETS[pair]
    rows = [presets[i : i + 2] for i in range(0, len(presets), 2)]
    rows.append(["Другая сумма"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def confirm_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Подтвердить", callback_data="confirm_order"),
                InlineKeyboardButton("Изменить", callback_data="edit_order"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# ОБРАБОТЧИКИ
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0] if getattr(context, "args", None) else "").strip()
    if arg.startswith("pl_"):
        await partner_login(update, context, arg)
        return
    if arg:
        try:
            if arg.lower() in sheets.get_partners():
                user = update.effective_user
                sheets.set_client_partner(
                    user.id, user.full_name, f"@{user.username}" if user.username else "", arg.lower()
                )
        except Exception:
            log.exception("Не удалось закрепить клиента за партнёром")
    context.user_data.clear()
    context.user_data["stage"] = "city"
    await update.message.reply_text(
        "Здравствуйте! Рады приветствовать вас в нашем сервисе обмена валют "
        "во Вьетнаме 😊\n\n"
        "⚡ Быстрый обмен, доставка курьером.\n"
        "🛡 Безопасность и конфиденциальность сделок гарантированы.\n\n"
        "Пожалуйста, выберите город, который вас интересует:",
        reply_markup=city_keyboard(),
    )


async def cmd_exchange(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /exchange — оформить заявку на обмен (начать сначала)."""
    await start(update, context)


CURRENCY_ICON = {"RUB/VND": "🔴", "USD/VND": "🟢", "USDT/VND": "🔵"}

MONTHS_RU = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /rate — показать актуальные курсы из Google Таблицы."""
    try:
        rates = sheets.get_best_rates()
    except Exception:
        log.exception("Не удалось получить курсы из таблицы")
        await update.message.reply_text(
            "Не получилось получить курсы прямо сейчас. Попробуйте чуть позже "
            "или уточните у менеджера: /support"
        )
        return

    if not rates:
        await update.message.reply_text("Курсы пока не заданы.")
        return

    updated = sheets.get_last_update()
    if updated:
        date_str = f"{updated.day} {MONTHS_RU[updated.month]}, {updated.strftime('%H:%M')}"
        header = f"💰 <b>Актуальные курсы</b> (на {date_str}):"
    else:
        header = "💰 <b>Актуальные курсы:</b>"

    p = client_partner(update.effective_user.id)
    if p:
        rates = [{**r, "rate": sheets.partner_rate(r["rate"], p["margin"])} for r in rates]

    lines = [header, ""]
    for r in rates:
        icon = CURRENCY_ICON.get(r["pair"], "⚪")
        code = r["pair"].split("/")[0]
        rate_str = fmt_rate(r["rate"])
        lines.append(f"{icon} <b>{code}</b>  →  {rate_str} VND")

    lines.append("")
    lines.append("🛵 Бесплатная доставка с личной встречей!")
    lines.append("🏧 Возможна выдача наличных через банкоматы по всему Вьетнаму без карты")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def format_client_block(user) -> str:
    """Формирует блок с именем, username и ID клиента для заявок/уведомлений."""
    username_line = f"@{user.username}" if user.username else "не указан"
    return (
        f"👤 Имя: {user.full_name}\n"
        f"🔗 Username: {username_line}\n"
        f"🆔 Telegram ID: {user.id}"
    )


# ---------------------------------------------------------------------------
# ПАРТНЁРЫ
# ---------------------------------------------------------------------------
STATUSES = {
    "work": "💬 в работе",
    "meet": "📍 встреча назначена",
    "done": "✅ обмен состоялся",
    "lost": "❌ отказ",
}
STATUS_SHEET = {"work": "в работе", "meet": "встреча", "done": "обмен", "lost": "отказ"}


def fmt_num(x) -> str:
    return f"{float(x):,.0f}".replace(",", " ")


def fmt_rate(x) -> str:
    x = float(x)
    return (f"{x:.1f}".rstrip("0").rstrip(".") if x < 1000 else fmt_num(x))


def partner_login_code(code: str) -> str:
    """Секретная ссылка входа для партнёра: pl_<код>_<подпись>."""
    sig = hmac.new(BOT_TOKEN.encode(), f"partner:{code}".encode(), hashlib.sha256).hexdigest()[:12]
    return f"pl_{code}_{sig}"


def client_partner(user_id):
    """Партнёр, за которым закреплён клиент (dict) или None. Ошибки таблицы не ломают бота."""
    try:
        code = sheets.get_client_partner(user_id)
        return sheets.get_partners().get(code) if code else None
    except Exception:
        log.exception("Не удалось прочитать партнёров из таблицы")
        return None


def is_manager_chat(chat_id) -> bool:
    return str(chat_id) == str(MANAGER_CHAT_ID)


async def partner_login(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str):
    try:
        partners = sheets.get_partners(force=True)
    except Exception:
        log.exception("Партнёры недоступны")
        await update.message.reply_text("Не получилось подключить, попробуйте позже.")
        return
    code = next((c for c in partners if partner_login_code(c) == arg), None)
    if not code:
        await update.message.reply_text("Ссылка устарела или неверная. Попросите новую у менеджера.")
        return
    chat_id = update.effective_chat.id
    sheets.set_partner_chat(code, chat_id)
    p = partners[code]
    await update.message.reply_text(
        f"Готово, вы подключены как партнёр «{p['name']}» 🤝\n\n"
        "Сюда будут приходить заявки ваших клиентов с их контактами, "
        "все изменения статуса и ваш доход по каждому обмену.\n\n"
        "/my — ваши заявки и доход за месяц"
    )
    await context.bot.send_message(
        chat_id=MANAGER_CHAT_ID,
        text=f"🤝 Партнёр «{p['name']}» подключился к боту.\n{format_client_block(update.effective_user)}",
    )


async def cmd_partners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/partners — только в чате менеджера: ссылки для клиентов и входа партнёров."""
    if not is_manager_chat(update.effective_chat.id):
        return
    try:
        partners = sheets.get_partners(force=True)
    except Exception:
        log.exception("Партнёры недоступны")
        await update.message.reply_text("Не удалось прочитать лист «Партнёры» в таблице.")
        return
    if not partners:
        await update.message.reply_text("В листе «Партнёры» пока нет ни одного партнёра.")
        return
    bot_name = context.bot.username
    lines = ["🤝 <b>Партнёры</b>", ""]
    for p in partners.values():
        lines += [
            f"<b>{html.escape(p['name'])}</b> · маржа {fmt_rate(p['margin'] * 100)}%",
            f"Ссылка для клиентов (на сайт): https://t.me/{bot_name}?start={p['code']}",
            f"Вход для партнёра (отправить ему лично): https://t.me/{bot_name}?start={partner_login_code(p['code'])}",
            "Партнёр подключён ✅" if p["chat_id"] else "Партнёр ещё не открыл ссылку входа",
            "",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def cmd_my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/my — для партнёра: его заявки и доход за текущий месяц."""
    try:
        p = sheets.partner_by_chat(update.effective_chat.id)
    except Exception:
        log.exception("Партнёры недоступны")
        p = None
    if not p:
        await update.message.reply_text("Эта команда для партнёров.")
        return
    month = datetime.now().strftime("%Y-%m")
    orders = sheets.partner_orders(p["code"], month)
    if not orders:
        await update.message.reply_text("В этом месяце заявок от ваших клиентов пока нет.")
        return
    lines = [f"📋 <b>Ваши заявки за {datetime.now().strftime('%m.%Y')}</b>", ""]
    done_sum = done_income = 0.0
    done_n = lost_n = 0
    for row_no, d in orders:
        status = d["Статус"] or "новая"
        user = html.escape(d["Username"] or d["Имя"])
        lines.append(
            f"№{row_no - 1} · {html.escape(d['Имя'])} {user if d['Username'] else ''}\n"
            f"   {fmt_num(sheets._to_float(d['Сумма']) or 0)} {d['Пара'].split('/')[0]} · {status}"
        )
        if status == "обмен":
            done_n += 1
            done_sum += sheets._to_float(d["Итог VND"]) or 0
            done_income += sheets._to_float(d["Доход партнёра VND"]) or 0
        elif status == "отказ":
            lost_n += 1
    lines += [
        "",
        f"Заявок: {len(orders)} · обменов: {done_n} · отказов: {lost_n}",
        f"Выдано клиентам: {fmt_num(done_sum)} ₫",
        f"💰 Ваш доход за месяц: <b>{fmt_num(done_income)} ₫</b>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def status_keyboard(row_no):
    if not row_no:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💬 В работе", callback_data=f"st:{row_no}:work"),
                InlineKeyboardButton("📍 Встреча", callback_data=f"st:{row_no}:meet"),
            ],
            [
                InlineKeyboardButton("✅ Обмен", callback_data=f"st:{row_no}:done"),
                InlineKeyboardButton("❌ Отказ", callback_data=f"st:{row_no}:lost"),
            ],
        ]
    )


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_manager_chat(query.message.chat_id):
        await query.answer("Статусы меняет менеджер")
        return
    _, row_s, st = query.data.split(":")
    row_no = int(row_s)
    try:
        sheets.set_order_status(row_no, STATUS_SHEET[st])
        order = sheets.get_order(row_no)
    except Exception:
        log.exception("Не удалось обновить статус")
        await query.answer("Таблица недоступна, попробуйте ещё раз", show_alert=True)
        return
    await query.answer(STATUSES[st])

    text = "\n".join(l for l in (query.message.text or "").split("\n") if not l.startswith("📌 Статус:"))
    final = st in ("done", "lost")
    await query.edit_message_text(
        f"{text}\n📌 Статус: {STATUSES[st]}",
        reply_markup=None if final else status_keyboard(row_no),
    )

    code = (order.get("Партнёр") or "").strip().lower()
    p = sheets.get_partners().get(code) if code else None
    if p and p["chat_id"]:
        name = html.escape(order.get("Имя", ""))
        msg = f"{STATUSES[st].split(' ', 1)[0]} №{row_no - 1} · {name} · {STATUSES[st].split(' ', 1)[1]}"
        if st == "done":
            amount = sheets._to_float(order.get("Сумма")) or 0
            msg += (
                f"\n{fmt_num(amount)} {order.get('Пара', '').split('/')[0]} ↔ "
                f"{fmt_num(sheets._to_float(order.get('Итог VND')) or 0)} ₫"
                f"\n💰 Ваш доход: <b>{fmt_num(sheets._to_float(order.get('Доход партнёра VND')) or 0)} ₫</b>"
            )
        try:
            await context.bot.send_message(chat_id=p["chat_id"], text=msg, parse_mode="HTML")
        except Exception:
            log.exception("Не удалось отправить статус партнёру")


async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /support — позвать специалиста, уведомить менеджера."""
    user = update.effective_user

    await update.message.reply_text(
        "Хорошо! Передал менеджеру, он свяжется с вами в ближайшее время 🤝"
    )

    manager_text = (
        "🙋 ЗАПРОС СПЕЦИАЛИСТА\n\n"
        f"{format_client_block(user)}\n\n"
        "Клиент хочет связаться с менеджером напрямую."
    )
    await context.bot.send_message(chat_id=MANAGER_CHAT_ID, text=manager_text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    stage = context.user_data.get("stage")

    # -------------------- ГОРОД --------------------
    if stage in (None, "city"):
        if text in CITIES:
            context.user_data["city"] = text
            context.user_data["stage"] = "currency"
            await update.message.reply_text(
                "Выберете валюту обмена на VND", reply_markup=currency_keyboard()
            )
            return
        if text == "Другой":
            context.user_data["stage"] = "city_custom"
            await update.message.reply_text(
                "Напишите название города, пожалуйста.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        await update.message.reply_text(
            "Пожалуйста, выберите город.", reply_markup=city_keyboard()
        )
        return

    if stage == "city_custom":
        context.user_data["city"] = text
        context.user_data["stage"] = "currency"
        await update.message.reply_text(
            "Выберете валюту обмена на VND", reply_markup=currency_keyboard()
        )
        return

    # -------------------- ВАЛЮТА --------------------
    if stage == "currency":
        pair = CURRENCIES.get(text.upper())
        if not pair:
            await update.message.reply_text(
                "Выберете валюту обмена на VND", reply_markup=currency_keyboard()
            )
            return
        context.user_data["pair"] = pair
        context.user_data["stage"] = "amount"
        await update.message.reply_text(
            "Выберите сумму для обмена или напишите свою цифрами:",
            reply_markup=amount_keyboard(pair),
        )
        return

    # -------------------- СУММА --------------------
    if stage == "amount":
        if text == "Другая сумма":
            await update.message.reply_text(
                "Напишите нужную сумму цифрами.", reply_markup=ReplyKeyboardRemove()
            )
            return

        pair = context.user_data.get("pair") or resolve_pair(text)
        amount = parse_amount(text)
        if not pair:
            context.user_data["stage"] = "currency"
            await update.message.reply_text(
                "Выберете валюту обмена на VND", reply_markup=currency_keyboard()
            )
            return
        if not amount or amount <= 0:
            await update.message.reply_text("Укажите сумму для обмена, пожалуйста.")
            return

        rate_row = sheets.get_rate(pair, amount)
        if not rate_row:
            await update.message.reply_text(
                "Для такой суммы курс сейчас не настроен. "
                "Пожалуйста, укажите другую сумму или уточните у менеджера."
            )
            return

        base_rate = rate_row["rate"]
        p = client_partner(update.effective_user.id)
        rate = sheets.partner_rate(base_rate, p["margin"]) if p else base_rate
        total_vnd = round(amount * rate)

        context.user_data["partner"] = p["code"] if p else ""
        context.user_data["base_rate"] = base_rate
        context.user_data["amount"] = amount
        context.user_data["rate"] = rate
        context.user_data["total_vnd"] = total_vnd
        context.user_data["stage"] = "confirm"

        city = context.user_data["city"]
        amount_fmt = f"{amount:,.0f}".replace(",", " ")
        total_fmt = f"{total_vnd:,.0f}".replace(",", " ")

        lines = [
            f"Вы хотите обменять {amount_fmt} {CURRENCY_LABEL[pair]} "
            f"{CITY_PHRASE.get(city, 'в ' + city)}.",
            f"Направление: {pair.replace('/VND', '')} → VND",
            f"Курс: {fmt_rate(rate)}",
            f"Итоговая сумма: {total_fmt} VND",
        ]
        if pair == "RUB/VND":
            tier_lines = []
            for threshold, icon in ((50000, "🚀"), (100000, "💎")):
                if amount >= threshold:
                    continue
                next_row = sheets.get_rate(pair, threshold)
                next_rate = next_row["rate"] if next_row else None
                if next_rate and p:
                    next_rate = sheets.partner_rate(next_rate, p["margin"])
                if next_rate and next_rate > rate:
                    threshold_fmt = f"{threshold:,.0f}".replace(",", " ")
                    label = "максимальный курс" if threshold == 100000 else "курс"
                    tier_lines.append(
                        f"• От {threshold_fmt} ₽: {label} {fmt_rate(next_rate)} {icon}"
                    )
            if tier_lines:
                lines.append("")
                lines.append("📈 Хотите курс еще выгоднее? У нас действует прогрессивная шкала:")
                lines.extend(tier_lines)

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=confirm_keyboard(),
        )
        return

    # -------------------- НЕОЖИДАННОЕ СООБЩЕНИЕ --------------------
    await update.message.reply_text(
        "Чтобы начать заново, отправьте /start."
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if (query.data or "").startswith("st:"):
        await handle_status(update, context)
        return
    await query.answer()

    if query.data == "confirm_order":
        data = context.user_data
        amount_fmt = f"{data['amount']:,.0f}".replace(",", " ")
        total_fmt = f"{data['total_vnd']:,.0f}".replace(",", " ")
        pair = data["pair"]

        summary = (
            f"Вы хотите обменять {amount_fmt} {CURRENCY_LABEL[pair]} "
            f"{CITY_PHRASE.get(data['city'], 'в ' + data['city'])}.\n"
            f"Направление: {pair.replace('/VND', '')} → VND\n"
            f"Курс: {fmt_rate(data['rate'])}\n"
            f"Итоговая сумма: {total_fmt} VND"
        )

        await query.edit_message_text(
            "Заявку зафиксировал! 🤝 Время и место согласуйте с менеджером — "
            "он скоро свяжется с вами."
        )

        user = query.from_user
        partner = sheets.get_partners().get(data.get("partner")) if data.get("partner") else None
        base_rate = data.get("base_rate", data["rate"])
        income = round(data["amount"] * (base_rate - data["rate"])) if partner else 0

        row_no = None
        try:
            row_no = sheets.log_order_full(
                {
                    "datetime": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "name": user.full_name,
                    "username": f"@{user.username}" if user.username else "",
                    "telegram_id": user.id,
                    "city": data.get("city", ""),
                    "pair": pair,
                    "amount": data["amount"],
                    "rate": data["rate"],
                    "total_vnd": data["total_vnd"],
                    "partner": partner["code"] if partner else "",
                    "base_rate": base_rate if partner else "",
                    "partner_income": income if partner else "",
                    "status": "новая",
                }
            )
        except Exception:
            log.exception("Не удалось записать заявку в журнал таблицы")

        number = f" №{row_no - 1}" if row_no else ""
        partner_line = (
            f"🤝 Клиент партнёра: {partner['name']} (ваш курс {fmt_rate(base_rate)}, "
            f"доход партнёра {fmt_num(income)} ₫)\n\n"
            if partner else ""
        )
        manager_text = (
            f"🔔 НОВАЯ ЗАЯВКА{number}\n\n"
            f"{partner_line}"
            f"{format_client_block(user)}\n\n"
            "📋 Подтверждённые условия:\n"
            f"{summary}\n\n"
            f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            "📌 Статус: 🆕 новая"
        )
        await context.bot.send_message(
            chat_id=MANAGER_CHAT_ID, text=manager_text, reply_markup=status_keyboard(row_no)
        )

        if partner and partner["chat_id"]:
            uname = f" · @{html.escape(user.username)}" if user.username else ""
            try:
                await context.bot.send_message(
                    chat_id=partner["chat_id"],
                    parse_mode="HTML",
                    text=(
                        f"🆕 <b>Новая заявка{number}</b>\n"
                        f"👤 <a href=\"tg://user?id={user.id}\">{html.escape(user.full_name)}</a>{uname}\n"
                        f"💱 {amount_fmt} {pair.split('/')[0]} → {total_fmt} ₫ · {html.escape(data['city'])}\n"
                        f"💰 Ваш доход, если обмен состоится: {fmt_num(income)} ₫"
                    ),
                )
            except Exception:
                log.exception("Не удалось отправить заявку партнёру")

        # Сброс валюты/суммы/курса, город остаётся — можно сразу оформить новый обмен
        city = data.get("city")
        context.user_data.clear()
        context.user_data["city"] = city
        context.user_data["stage"] = "currency"
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Если нужен ещё один обмен — выберите валюту:",
            reply_markup=currency_keyboard(),
        )
        return

    if query.data == "edit_order":
        context.user_data["stage"] = "amount"
        pair = context.user_data.get("pair")
        await query.edit_message_text("Принято! Укажите новую сумму для обмена.")
        if pair:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="Выберите сумму или напишите свою цифрами:",
                reply_markup=amount_keyboard(pair),
            )
        return


async def _post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("exchange", "Оформить заявку на обмен"),
            BotCommand("rate", "Актуальные курсы валют и USDT"),
            BotCommand("support", "Позвать специалиста"),
            BotCommand("start", "Начать обмен"),
        ]
    )


def main():
    persistence = PicklePersistence(filepath="bot_state.pkl")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("exchange", cmd_exchange))
    app.add_handler(CommandHandler("rate", cmd_rate))
    app.add_handler(CommandHandler("support", cmd_support))
    app.add_handler(CommandHandler("partners", cmd_partners))
    app.add_handler(CommandHandler("my", cmd_my))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
