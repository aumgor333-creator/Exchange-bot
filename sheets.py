"""
Чтение курсов из Google Таблицы.

Структура листа (строка заголовка + данные), как у вас настроено сейчас:
  A: Валютная пара (например RUB/VND, USD/VND, USDT/VND)
  B: Курс
  C: От (нижняя граница суммы)
  D: До (верхняя граница суммы, пусто = без верхней границы)

Если поменяете порядок колонок в таблице — поправьте номера ниже (COL_*).
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

if "GOOGLE_SHEET_ID" not in os.environ:
    visible = sorted(os.environ.keys())
    raise RuntimeError(
        "Переменная GOOGLE_SHEET_ID не найдена в этом контейнере.\n"
        "Все переменные окружения, которые реально видит процесс сейчас:\n"
        + "\n".join(visible)
    )

SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
WORKSHEET_NAME = os.environ.get("GOOGLE_WORKSHEET_NAME", "Курсы")

# Индексы колонок (0 = A, 1 = B, 2 = C, 3 = D)
# Ваша таблица: A - Валютная пара, B - Курс, C - от, D - до
COL_PAIR = 0
COL_RATE = 1
COL_FROM = 2
COL_TO = 3

# Пары, которые показываем клиенту в /rate, и порядок отображения
DISPLAY_PAIRS = ["RUB/VND", "USD/VND", "USDT/VND"]

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]
_client = None
_drive_service = None


def _get_credentials():
    creds_raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_dict = json.loads(creds_raw)
    return Credentials.from_service_account_info(creds_dict, scopes=_SCOPES)


def _get_client():
    global _client
    if _client is None:
        _client = gspread.authorize(_get_credentials())
    return _client


def _get_drive_service():
    global _drive_service
    if _drive_service is None:
        _drive_service = build("drive", "v3", credentials=_get_credentials())
    return _drive_service


def _to_float(value):
    if value in (None, ""):
        return None
    value = str(value).replace(" ", "").replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return None


def get_rate(pair: str, amount: float):
    """Возвращает {'rate': float, 'from': float|None, 'to': float|None}
    для первой подходящей строки, либо None, если строка не найдена."""
    client = _get_client()
    sheet = client.open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)
    rows = sheet.get_all_values()[1:]  # пропускаем заголовок

    for row in rows:
        if len(row) <= max(COL_PAIR, COL_FROM, COL_TO, COL_RATE):
            continue
        if row[COL_PAIR].strip() != pair:
            continue

        low = _to_float(row[COL_FROM])
        high = _to_float(row[COL_TO])
        rate = _to_float(row[COL_RATE])
        if rate is None:
            continue

        if low is not None and amount < low:
            continue
        if high is not None and amount > high:
            continue

        return {"rate": rate, "from": low, "to": high}

    return None


def get_best_rates():
    """Возвращает лучший (самый выгодный) курс по каждой из трёх пар
    RUB/VND, USD/VND, USDT/VND — для RUB/VND это тариф с наибольшим
    порогом «от» (топ-диапазон, обычно от 100 000).
    Результат: [{'pair': 'RUB/VND', 'rate': 275.0}, ...] в порядке DISPLAY_PAIRS,
    пары без данных в таблице пропускаются."""
    client = _get_client()
    sheet = client.open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)
    rows = sheet.get_all_values()[1:]

    best_by_pair = {}
    for row in rows:
        if len(row) <= max(COL_PAIR, COL_FROM, COL_TO, COL_RATE):
            continue
        pair = row[COL_PAIR].strip()
        if pair not in DISPLAY_PAIRS:
            continue
        rate = _to_float(row[COL_RATE])
        if rate is None:
            continue
        low = _to_float(row[COL_FROM]) or 0

        current = best_by_pair.get(pair)
        if current is None or low > current["from"]:
            best_by_pair[pair] = {"rate": rate, "from": low}

    result = []
    for pair in DISPLAY_PAIRS:
        if pair in best_by_pair:
            result.append({"pair": pair, "rate": best_by_pair[pair]["rate"]})
    return result


def get_last_update():
    """Возвращает datetime последнего изменения самой таблицы (по данным Google Drive),
    в часовом поясе UTC+7 (Вьетнам), либо None при ошибке."""
    try:
        service = _get_drive_service()
        meta = service.files().get(fileId=SHEET_ID, fields="modifiedTime").execute()
        modified = meta.get("modifiedTime")
        if not modified:
            return None
        dt = datetime.fromisoformat(modified.replace("Z", "+00:00"))
        return dt.astimezone(timezone(timedelta(hours=7)))
    except Exception:
        log.exception("Не удалось получить дату последнего обновления таблицы (Drive API)")
        return None


ORDERS_WORKSHEET_NAME = os.environ.get("GOOGLE_ORDERS_WORKSHEET_NAME", "Заявки")
ORDERS_HEADER = [
    "Дата/время",
    "Имя",
    "Username",
    "Telegram ID",
    "Город",
    "Пара",
    "Сумма",
    "Курс",
    "Итог VND",
]


def _get_or_create_orders_worksheet(spreadsheet):
    try:
        return spreadsheet.worksheet(ORDERS_WORKSHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=ORDERS_WORKSHEET_NAME, rows=1000, cols=len(ORDERS_HEADER)
        )
        ws.append_row(ORDERS_HEADER)
        return ws


def log_order(order: dict):
    """Дописывает одну строку с данными подтверждённой заявки в лист «Заявки».
    Лист и заголовки создаются автоматически при первом обращении, если их ещё нет.
    order: {'datetime','name','username','telegram_id','city','pair','amount','rate','total_vnd'}."""
    client = _get_client()
    spreadsheet = client.open_by_key(SHEET_ID)
    ws = _get_or_create_orders_worksheet(spreadsheet)

    row = [
        order.get("datetime", ""),
        order.get("name", ""),
        order.get("username", ""),
        order.get("telegram_id", ""),
        order.get("city", ""),
        order.get("pair", ""),
        order.get("amount", ""),
        order.get("rate", ""),
        order.get("total_vnd", ""),
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
