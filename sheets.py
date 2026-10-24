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


# ---------------------------------------------------------------------------
# ПАРТНЁРЫ
# Лист «Партнёры»: A Код | B Имя партнёра | C Маржа % | D Chat ID партнёра | E Разница курса RUB
# Код — латиницей, он же метка в ссылке: t.me/<бот>?start=<код>.
# Маржа — на сколько процентов курс для клиентов партнёра ниже вашего (1 = 1%).
# Разница курса RUB — если заполнена, курс рубля для клиентов партнёра = ваш курс минус
# это число (286 − 2 = 284); маржа % тогда действует только для USD и USDT.
# Chat ID бот вписывает сам, когда партнёр откроет свою ссылку входа (/partners).
#
# Лист «Клиенты партнёров»: кто из клиентов пришёл от какого партнёра.
# Клиент закрепляется за первым партнёром навсегда.
# ---------------------------------------------------------------------------
PARTNERS_WORKSHEET_NAME = os.environ.get("GOOGLE_PARTNERS_WORKSHEET_NAME", "Партнёры")
PARTNERS_HEADER = ["Код", "Имя партнёра", "Маржа %", "Chat ID партнёра", "Разница курса RUB"]
PARTNERS_EXAMPLE = ["vietlife", "Артём · viet-life.ru", "1", "", "2"]

CLIENTS_WORKSHEET_NAME = os.environ.get("GOOGLE_CLIENTS_WORKSHEET_NAME", "Клиенты партнёров")
CLIENTS_HEADER = ["Telegram ID", "Имя", "Username", "Партнёр", "Дата"]

_partners_cache = {"at": 0.0, "data": None}
_clients_cache = None  # {telegram_id(str): partner_code}
PARTNERS_TTL = 60  # сек — правки в таблице подхватываются в течение минуты


def _sheet(name, header, example=None):
    spreadsheet = _get_client().open_by_key(SHEET_ID)
    try:
        return spreadsheet.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=name, rows=500, cols=len(header))
        ws.append_row(header)
        if example:
            ws.append_row(example)
        return ws


def _margin(value):
    """'1' / '1%' / '1,5' → 0.01 / 0.01 / 0.015."""
    v = _to_float(str(value or "").replace("%", ""))
    if v is None or v < 0 or v >= 50:
        return 0.0
    return v / 100


def get_partners(force=False):
    """{код: {'code','name','margin','chat_id','row'}} — с кэшем на минуту."""
    import time
    now = time.time()
    if not force and _partners_cache["data"] is not None and now - _partners_cache["at"] < PARTNERS_TTL:
        return _partners_cache["data"]
    ws = _sheet(PARTNERS_WORKSHEET_NAME, PARTNERS_HEADER, PARTNERS_EXAMPLE)
    values = ws.get_all_values()
    if values and len([c for c in values[0] if c.strip()]) < len(PARTNERS_HEADER):
        ws.update(range_name="A1", values=[PARTNERS_HEADER])  # добавить новый заголовок «Разница курса RUB»
    data = {}
    for i, row in enumerate(values[1:], start=2):
        row = (row + [""] * 5)[:5]
        code = row[0].strip().lower()
        if not code:
            continue
        data[code] = {
            "code": code,
            "name": row[1].strip() or code,
            "margin": _margin(row[2]),
            "chat_id": row[3].strip(),
            "diff": _to_float(row[4]) if _to_float(row[4]) and _to_float(row[4]) > 0 else None,
            "row": i,
        }
    _partners_cache.update(at=now, data=data)
    return data


def partner_by_chat(chat_id):
    for p in get_partners().values():
        if p["chat_id"] and p["chat_id"] == str(chat_id):
            return p
    return None


def set_partner_chat(code, chat_id):
    p = get_partners(force=True).get(code)
    if not p:
        return False
    ws = _sheet(PARTNERS_WORKSHEET_NAME, PARTNERS_HEADER)
    ws.update_cell(p["row"], 4, str(chat_id))
    get_partners(force=True)
    return True


def _load_clients():
    global _clients_cache
    if _clients_cache is None:
        ws = _sheet(CLIENTS_WORKSHEET_NAME, CLIENTS_HEADER)
        _clients_cache = {}
        for row in ws.get_all_values()[1:]:
            if len(row) >= 4 and row[0].strip() and row[3].strip():
                _clients_cache.setdefault(row[0].strip(), row[3].strip().lower())
    return _clients_cache


def get_client_partner(telegram_id):
    """Код партнёра, за которым закреплён клиент, или None."""
    return _load_clients().get(str(telegram_id))


def set_client_partner(telegram_id, name, username, code):
    """Закрепляет клиента за партнёром, если он ещё ни за кем не закреплён.
    Возвращает код партнёра, за которым клиент закреплён в итоге."""
    clients = _load_clients()
    key = str(telegram_id)
    if key in clients:
        return clients[key]
    ws = _sheet(CLIENTS_WORKSHEET_NAME, CLIENTS_HEADER)
    ws.append_row(
        [key, name, username, code, datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M")],
        value_input_option="RAW",
    )
    clients[key] = code
    return code


def partner_rate(rate, margin, diff=None):
    """Курс для клиента партнёра, всегда целое число.
    Рубль (курс меньше 1000): ваш курс минус «Разница курса RUB», если она задана;
    иначе и для USD/USDT — ниже вашего на маржу %, с округлением вниз."""
    import math
    if rate < 1000 and diff:
        return math.floor(rate - diff)
    return math.floor(rate * (1 - margin))


# ---------------------------------------------------------------------------
# ЗАЯВКИ: дополнительные колонки для партнёров и статусов
# ---------------------------------------------------------------------------
ORDERS_HEADER += ["Партнёр", "Ваш курс", "Доход партнёра VND", "Статус"]
COL_O_PARTNER, COL_O_BASE, COL_O_INCOME, COL_O_STATUS = 10, 11, 12, 13  # номера колонок (A = 1)


def _orders_ws():
    ws = _get_or_create_orders_worksheet(_get_client().open_by_key(SHEET_ID))
    head = ws.row_values(1)
    if len(head) < len(ORDERS_HEADER):
        ws.update(range_name="A1", values=[ORDERS_HEADER])
    return ws


def log_order_full(order: dict):
    """Как log_order, плюс партнёр, ваш курс, доход партнёра и статус.
    Возвращает номер строки (он же номер заявки + 1)."""
    ws = _orders_ws()
    row = [
        order.get("datetime", ""), order.get("name", ""), order.get("username", ""),
        order.get("telegram_id", ""), order.get("city", ""), order.get("pair", ""),
        order.get("amount", ""), order.get("rate", ""), order.get("total_vnd", ""),
        order.get("partner", ""), order.get("base_rate", ""), order.get("partner_income", ""),
        order.get("status", "новая"),
    ]
    resp = ws.append_row(row, value_input_option="USER_ENTERED")
    rng = ((resp or {}).get("updates") or {}).get("updatedRange", "")
    import re
    m = re.search(r"!A(\d+)", rng)
    return int(m.group(1)) if m else None


def get_order(row_no):
    ws = _orders_ws()
    vals = ws.row_values(row_no)
    vals = (vals + [""] * len(ORDERS_HEADER))[: len(ORDERS_HEADER)]
    return dict(zip(ORDERS_HEADER, vals))


def set_order_status(row_no, status):
    _orders_ws().update_cell(row_no, COL_O_STATUS, status)


def partner_orders(code, month_prefix):
    """Заявки партнёра за месяц ('2026-10'): список (номер строки, dict)."""
    ws = _orders_ws()
    out = []
    for i, vals in enumerate(ws.get_all_values()[1:], start=2):
        vals = (vals + [""] * len(ORDERS_HEADER))[: len(ORDERS_HEADER)]
        d = dict(zip(ORDERS_HEADER, vals))
        if d["Партнёр"].strip().lower() == code and d["Дата/время"].startswith(month_prefix):
            out.append((i, d))
    return out
