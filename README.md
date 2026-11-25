# Парсер отзывов App Store (RU) — Streamlit

Локальное Streamlit-приложение для сбора доступных последних русскоязычных отзывов российского App Store.

## Файлы

- `app_store_reviews_streamlit.py` — код приложения.
- `requirements.txt` — зависимости для локального запуска и Streamlit Community Cloud.

## Локальный запуск (Windows)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run app_store_reviews_streamlit.py
```

После запуска откройте `http://localhost:8501`.

## Локальный запуск (macOS / Linux)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app_store_reviews_streamlit.py
```

## Развёртывание в Streamlit Community Cloud

1. Загрузите эти файлы в корень GitHub-репозитория.
2. Откройте https://share.streamlit.io/ и войдите через GitHub.
3. Нажмите **Create app**.
4. Выберите репозиторий, ветку `main` и файл `app_store_reviews_streamlit.py`.
5. Нажмите **Deploy**.

`requirements.txt` должен находиться в корне репозитория, чтобы Community Cloud установил зависимости автоматически.

## Ограничения источников

- Приложение запрашивает отзывы российского магазина (`ru`).
- JSON-маршрут App Store — внутренний интерфейс сайта Apple, а не гарантированный стабильный публичный API; возможен HTTP 429.
- RSS Apple ограничен доступной глубиной (обычно около 500 последних отзывов на сортировку).
- Нельзя гарантировать получение заданного количества отзывов или полной истории. Смотрите статус источника и предупреждения в интерфейсе.
- Публикация приложения в интернете означает, что любой пользователь, имеющий ссылку на Streamlit-приложение, может отправлять запросы через него. Для личного использования лучше запускать локально или ограничить доступ.
