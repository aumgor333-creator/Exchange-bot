"""
Чтение курсов из Google Таблицы.

Ожидаемая структура листа (строка заголовка + данные):
  A: Пара (например RUB/VND, USD/VND, USDT/VND)
  B: От (нижняя граница суммы)
  C: До (верхняя граница суммы, пусто = без верхней границы)
  D: Курс

Если у вас другой порядок колонок — поправьте номера ниже (COL_*).
"""

import json
import os

import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
WORKSHEET_NAME = os.environ.get("GOOGLE_WORKSHEET_NAME", "Курсы")

# Индексы колонок (0 = A, 1 = B, 2 = C, 3 = D)
COL_PAIR = 0
COL_FROM = 1
COL_TO = 2
COL_RATE = 3

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
_client = None


def _get_client():
    global _client
    if _client is None:
        creds_raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
        creds_dict = json.loads(creds_raw)
        creds = Credentials.from_service_account_info(creds_dict, scopes=_SCOPES)
        _client = gspread.authorize(creds)
    return _client


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
