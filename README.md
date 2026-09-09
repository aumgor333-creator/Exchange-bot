# Бот обмена валют — установка и запуск на Railway

## Что понадобится
1. Токен Telegram-бота (уже есть — тот же бот, что был в Make, либо новый от @BotFather).
2. Chat ID менеджера — куда падают заявки.
3. Доступ к Google Таблице с курсами через сервисный аккаунт Google.

## Шаг 1. Google-сервисный аккаунт (доступ к таблице)
1. Зайдите на https://console.cloud.google.com/ → создайте проект (или выберите существующий).
2. В меню слева: **APIs & Services → Library** → включите **Google Sheets API**.
3. **APIs & Services → Credentials → Create Credentials → Service Account** → создайте аккаунт.
4. Откройте созданный сервисный аккаунт → вкладка **Keys → Add Key → JSON** — скачается файл ключа. Его содержимое целиком (весь JSON) пойдёт в переменную `GOOGLE_SERVICE_ACCOUNT_JSON`.
5. Откройте вашу Google Таблицу с курсами → кнопка **"Настройки доступа"** → добавьте email сервисного аккаунта (выглядит как `xxx@xxx.iam.gserviceaccount.com`, он есть в скачанном JSON-файле в поле `client_email`) с правом **Читатель**.
6. Проверьте структуру листа с курсами — по умолчанию бот ожидает колонки:
   `A: Пара | B: От | C: До | D: Курс` (например `RUB/VND | 0 | 49999 | 25500`).
   Если у вас другой порядок — поменяйте `COL_PAIR`, `COL_FROM`, `COL_TO`, `COL_RATE` в файле `sheets.py`.

## Шаг 2. Chat ID менеджера
Проще всего: напишите любое сообщение вашему боту от имени менеджера, затем откройте в браузере
`https://api.telegram.org/bot<ВАШ_ТОКЕН>/getUpdates` и найдите `"chat":{"id": ...}` — это и есть нужный ID.

## Шаг 3. Выложить код на GitHub
1. Зарегистрируйтесь на https://github.com (если ещё нет аккаунта).
2. Создайте новый репозиторий (New repository), например `exchange-bot`.
3. Загрузите туда все файлы из этой папки (`bot.py`, `sheets.py`, `requirements.txt`, `README.md`) — можно просто перетащить их в веб-интерфейсе GitHub через "Add file → Upload files".

## Шаг 4. Деплой на Railway
1. Зайдите на https://railway.app → **Login with GitHub**.
2. **New Project → Deploy from GitHub repo** → выберите репозиторий `exchange-bot`.
3. Откройте проект → вкладка **Variables** → добавьте:
   - `TELEGRAM_BOT_TOKEN` — токен бота
   - `MANAGER_CHAT_ID` — chat id менеджера (шаг 2)
   - `GOOGLE_SHEET_ID` — ID таблицы (кусок ссылки между `/d/` и `/edit`)
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — весь JSON-ключ из шага 1 (целиком, в одну строку)
   - `GOOGLE_WORKSHEET_NAME` — название листа с курсами (по умолчанию `Курсы`)
4. Railway сам определит, что это Python-проект, установит зависимости из `requirements.txt` и запустит `python bot.py`.
5. Готово — откройте бота в Telegram и отправьте `/start`.

## Проверить логи
В Railway: вкладка **Deployments → View Logs** — там видно каждое сообщение и ошибки, если что-то пойдёт не так.
