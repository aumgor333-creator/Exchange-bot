"""
Телеграм-бот обмена валют (Дананг / Нячанг / Фукуок).
Логика полностью повторяет предыдущий сценарий в Make, но без задержек
и без ограничений no-code платформы.

Настройка — см. README.md.
"""

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


CURRENCY_ICON = {"RUB/VND": "₽ RUB", "USD/VND": "$ USD", "USDT/VND": "💲 USDT"}

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
        header = f"💱 Актуальные курсы (на {date_str}):"
    else:
        header = "💱 Актуальные курсы:"

    lines = [header, ""]
    for r in rates:
        label = CURRENCY_ICON.get(r["pair"], r["pair"])
        rate_str = f"{r['rate']:,.0f}".replace(",", " ")
        lines.append(f"{label} → {rate_str} VND")

    lines.append("")
    lines.append("🛵 Бесплатная доставка с личной встречей!")
    lines.append("🏧 Возможна выдача наличных через банкоматы по всему Вьетнаму без карты")

    await update.message.reply_text("\n".join(lines))


async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /support — позвать специалиста, уведомить менеджера."""
    user = update.effective_user
    username = f"@{user.username}" if user.username else user.full_name

    await update.message.reply_text(
        "Хорошо! Передал менеджеру, он свяжется с вами в ближайшее время 🤝"
    )

    manager_text = (
        "🙋 ЗАПРОС СПЕЦИАЛИСТА\n\n"
        f"👤 Клиент: {username}\n"
        f"🆔 Telegram ID: {user.id}\n\n"
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

        rate = rate_row["rate"]
        total_vnd = round(amount * rate)

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
            f"Курс: {rate}",
            f"Итоговая сумма: {total_fmt} VND",
        ]
        if pair == "RUB/VND":
            tier_lines = []
            for threshold, icon in ((50000, "🚀"), (100000, "💎")):
                if amount >= threshold:
                    continue
                next_row = sheets.get_rate(pair, threshold)
                if next_row and next_row["rate"] > rate:
                    threshold_fmt = f"{threshold:,.0f}".replace(",", " ")
                    label = "максимальный курс" if threshold == 100000 else "курс"
                    tier_lines.append(
                        f"• От {threshold_fmt} ₽: {label} {next_row['rate']:.0f} {icon}"
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
            f"Курс: {data['rate']}\n"
            f"Итоговая сумма: {total_fmt} VND"
        )

        await query.edit_message_text(
            "Заявку зафиксировал! 🤝 Время и место согласуйте с менеджером — "
            "он скоро свяжется с вами."
        )

        user = query.from_user
        username = f"@{user.username}" if user.username else user.full_name
        manager_text = (
            "🔔 НОВАЯ ЗАЯВКА\n\n"
            f"👤 Клиент: {username}\n"
            f"🆔 Telegram ID: {user.id}\n\n"
            "📋 Подтверждённые условия:\n"
            f"{summary}\n\n"
            f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        await context.bot.send_message(chat_id=MANAGER_CHAT_ID, text=manager_text)

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
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
