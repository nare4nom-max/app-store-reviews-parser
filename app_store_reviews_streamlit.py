"""Streamlit-приложение для сбора последних русскоязычных отзывов App Store.

Запуск:
    pip install -r requirements.txt
    streamlit run app_store_reviews_streamlit.py

Основной источник — внутренний JSON-маршрут сайта App Store. Это не
документированный стабильный API, поэтому предусмотрен резервный RSS Apple.
RSS обычно ограничен примерно 500 последними отзывами на одну сортировку.
"""

from __future__ import annotations

import csv
import hashlib
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

try:
    from langdetect import DetectorFactory, LangDetectException, detect

    DetectorFactory.seed = 0
except ImportError:  # Дальше используется упрощённая эвристика.
    LangDetectException = Exception  # type: ignore[assignment,misc]
    detect = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

COUNTRY_CODE = "ru"
TIMEZONE_NAME = "Europe/Moscow"
PAGE_SIZE = 20
REQUEST_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 1
MAX_PAGES = 2_000
MAX_RSS_PAGES = 10
ENABLE_RSS_FALLBACK = True

LOOKUP_URL = "https://itunes.apple.com/lookup"
REVIEWS_URL = (
    "https://apps.apple.com/api/apps/v1/catalog/ru/apps/{app_id}/reviews"
)
RSS_URL = (
    "https://itunes.apple.com/ru/rss/customerreviews/page={page}/"
    "id={app_id}/sortby=mostrecent/json"
)

REQUEST_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://apps.apple.com",
    "Referer": "https://apps.apple.com/",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

CSV_COLUMNS = ["review_text", "rating", "review_date", "author", "title"]
OUTPUT_DIR = Path("exports")

ProgressCallback = Callable[[str, int, int, str], None]


# ---------------------------------------------------------------------------
# Исключения
# ---------------------------------------------------------------------------

class AppStoreError(Exception):
    """Базовая ошибка при обращении к источникам App Store."""


class AppNotAvailableError(AppStoreError):
    """Приложение не найдено в российском App Store."""


class SourceAccessError(AppStoreError):
    """Источник запретил запрос или вернул ошибку HTTP."""


class RateLimitError(AppStoreError):
    """Источник ограничил частоту запросов (HTTP 429)."""


class SourceFormatError(AppStoreError):
    """Ответ источника пустой, повреждённый или неожиданной структуры."""


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def extract_app_id(app_url: str) -> str:
    """Извлекает числовой ID из ссылки App Store или принимает сам ID."""
    value = str(app_url).strip()
    if re.fullmatch(r"\d{5,20}", value):
        return value

    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    allowed_hosts = ("apps.apple.com", "itunes.apple.com")
    if not any(host == item or host.endswith("." + item) for item in allowed_hosts):
        raise ValueError(
            "Укажите ссылку с домена apps.apple.com / itunes.apple.com "
            "или числовой ID приложения."
        )

    match = re.search(r"(?:^|/)id(\d+)(?:$|/)", parsed.path)
    if not match:
        match = re.search(r"id(\d+)", value)
    if not match:
        raise ValueError("Не удалось найти ID приложения в ссылке App Store.")
    return match.group(1)


def make_session() -> requests.Session:
    """Создаёт HTTP-сессию с общими заголовками."""
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    return session


def retry_after_seconds(response: requests.Response | None) -> int:
    """Преобразует заголовок Retry-After в число секунд для сообщения."""
    value = response.headers.get("Retry-After") if response is not None else None
    if not value:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            retry_date = parsedate_to_datetime(value)
            if retry_date.tzinfo is None:
                retry_date = retry_date.replace(tzinfo=timezone.utc)
            return max(
                0,
                int((retry_date - datetime.now(timezone.utc)).total_seconds()),
            )
        except (TypeError, ValueError, OverflowError):
            return 0


def request_with_retries(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
    max_retries: int = MAX_RETRIES,
) -> requests.Response:
    """Выполняет запрос с ограниченными повторами; HTTP 429 не повторяет."""
    for attempt in range(max_retries + 1):
        try:
            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers=REQUEST_HEADERS,
            )
        except requests.RequestException as exc:
            if attempt >= max_retries:
                raise SourceAccessError(
                    f"Сетевая ошибка после {max_retries + 1} попыток: {exc}"
                ) from exc
            time.sleep(min(2 ** attempt, 8))
            continue

        if response.status_code == 200:
            return response
        if response.status_code == 403:
            raise SourceAccessError(
                "App Store вернул HTTP 403: доступ к источнику запрещён."
            )
        if response.status_code == 404:
            raise SourceAccessError("App Store вернул HTTP 404: ресурс не найден.")
        if response.status_code == 429:
            wait_seconds = retry_after_seconds(response)
            note = (
                f" Заголовок Retry-After: {wait_seconds} сек."
                if wait_seconds else ""
            )
            raise RateLimitError(
                "App Store вернул HTTP 429 (ограничение частоты запросов). "
                "Повтор в этом запуске не выполняется." + note
            )
        if response.status_code in (500, 502, 503, 504):
            if attempt >= max_retries:
                raise SourceAccessError(
                    f"Сервер App Store вернул HTTP {response.status_code} "
                    "после повторной попытки."
                )
            time.sleep(min(2 ** attempt, 8))
            continue
        raise SourceAccessError(
            f"Неожиданный HTTP-статус источника: {response.status_code}."
        )

    raise SourceAccessError("Не удалось выполнить запрос к App Store.")


def parse_date_to_iso(value: Any, timezone_name: str = TIMEZONE_NAME) -> str | None:
    """Преобразует дату к YYYY-MM-DD с часовым поясом Europe/Moscow."""
    if value is None or not str(value).strip():
        return None
    timestamp = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(timestamp):
        return None
    return timestamp.tz_convert(ZoneInfo(timezone_name)).date().isoformat()


def validate_app(app_id: str, session: requests.Session) -> dict[str, Any]:
    """Проверяет доступность приложения именно в российском App Store."""
    response = request_with_retries(
        session,
        LOOKUP_URL,
        params={"id": app_id, "country": COUNTRY_CODE},
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise SourceFormatError("Lookup API вернул невалидный JSON.") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise SourceFormatError("Lookup API вернул неожиданную структуру JSON.")
    if not payload["results"]:
        raise AppNotAvailableError(
            "Приложение не найдено в российском App Store (country=ru). "
            "Проверьте ссылку и доступность приложения в регионе RU."
        )

    app_info = payload["results"][0]
    if str(app_info.get("trackId", app_id)) != str(app_id):
        raise SourceFormatError("Lookup API вернул другое приложение.")
    return app_info


# ---------------------------------------------------------------------------
# Разбор ответов источников
# ---------------------------------------------------------------------------

def parse_json_reviews(payload: Any) -> tuple[list[dict[str, Any]], bool, bool]:
    """Извлекает отзывы и признак следующей страницы из JSON-ответа."""
    if not isinstance(payload, dict) or "data" not in payload:
        raise SourceFormatError(
            "В JSON отсутствует ожидаемое поле 'data'. Возможно, Apple изменила API."
        )
    items = payload["data"]
    if not isinstance(items, list):
        raise SourceFormatError("Поле 'data' имеет неожиданный тип.")

    next_value: Any = payload.get("next", None)
    if next_value is None and isinstance(payload.get("links"), dict):
        next_value = payload["links"].get("next")
    next_known = "next" in payload or (
        isinstance(payload.get("links"), dict) and "next" in payload["links"]
    )
    return items, next_known, bool(next_value)


def rss_label(value: Any) -> Any:
    """Достаёт значение label из поля RSS Apple."""
    if isinstance(value, dict):
        return value.get("label") or value.get("im:label") or ""
    return value or ""


def parse_rss_reviews(payload: Any) -> list[dict[str, Any]]:
    """Преобразует RSS Apple в общий формат JSON API."""
    if not isinstance(payload, dict) or not isinstance(payload.get("feed"), dict):
        raise SourceFormatError("RSS вернул неожиданную структуру JSON.")

    entries = payload["feed"].get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        raise SourceFormatError("Поле feed.entry имеет неожиданный тип.")

    normalized = []
    for entry in entries:
        if not isinstance(entry, dict) or "im:rating" not in entry:
            continue
        author_obj = entry.get("author", {})
        author_name = author_obj.get("name", {}) if isinstance(author_obj, dict) else {}
        normalized.append(
            {
                "id": rss_label(entry.get("id")),
                "attributes": {
                    "rating": rss_label(entry.get("im:rating")),
                    "title": rss_label(entry.get("title")),
                    "review": rss_label(entry.get("content")),
                    "userName": rss_label(author_name),
                    "date": rss_label(entry.get("updated")),
                },
            }
        )
    return normalized


# ---------------------------------------------------------------------------
# Нормализация, язык и дубликаты
# ---------------------------------------------------------------------------

RUSSIAN_SHORT_PHRASES = {
    "да", "нет", "ок", "окей", "супер", "класс", "круто", "норм",
    "нормально", "хорошо", "плохо", "спасибо", "отлично", "ужас",
    "топ", "кайф", "люблю", "работает", "понятно", "удобно", "огонь",
}
RUSSIAN_MARKERS = set("ёъыэ")


def detect_russian_language(review_text: str, title: str = "") -> str:
    """Возвращает ru, other или unknown по тексту и заголовку отзыва."""
    combined = f"{title or ''} {review_text or ''}".strip().lower()
    if not combined:
        return "unknown"

    words = re.findall(r"[а-яё]+", combined)
    cyrillic_letters = re.findall(r"[а-яё]", combined)
    latin_letters = re.findall(r"[a-z]", combined)
    if not cyrillic_letters:
        return "other" if latin_letters else "unknown"
    if any(word in RUSSIAN_SHORT_PHRASES for word in words):
        return "ru"
    if len(cyrillic_letters) <= 12 and any(
        letter in RUSSIAN_MARKERS for letter in cyrillic_letters
    ):
        return "ru"

    if detect is None:
        # Консервативная эвристика для запуска без langdetect.
        return "ru" if len(cyrillic_letters) >= 8 and len(words) >= 2 else "unknown"
    try:
        return "ru" if detect(combined) == "ru" else "other"
    except LangDetectException:
        return "unknown"


def first_value(mapping: Any, keys: list[str], default: Any = "") -> Any:
    """Возвращает первое непустое значение из списка ключей словаря."""
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return value
    return default


def parse_reviews(raw_reviews: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Нормализует отзывы из JSON API или RSS; возвращает число некорректных."""
    parsed = []
    invalid_count = 0
    for item in raw_reviews:
        if not isinstance(item, dict):
            invalid_count += 1
            continue
        attributes = item.get("attributes")
        if not isinstance(attributes, dict):
            attributes = item

        try:
            rating = int(float(first_value(attributes, ["rating", "im:rating"])))
        except (TypeError, ValueError, OverflowError):
            invalid_count += 1
            continue
        if rating not in range(1, 6):
            invalid_count += 1
            continue

        title = str(first_value(attributes, ["title"], "") or "").strip()
        review_text = str(
            first_value(attributes, ["review", "body", "text", "content"], "") or ""
        ).strip()
        author = str(
            first_value(
                attributes,
                ["userName", "reviewerNickname", "author", "authorName"],
                "",
            ) or ""
        ).strip()
        raw_date = first_value(
            attributes,
            ["date", "createdDate", "updated", "publishedDate"],
            "",
        )
        review_date = parse_date_to_iso(raw_date)
        if not review_date or (not review_text and not title):
            invalid_count += 1
            continue

        source_id = first_value(item, ["id", "reviewId"], "")
        if not source_id:
            source_id = first_value(attributes, ["id", "reviewId"], "")

        parsed.append(
            {
                "review_text": review_text,
                "rating": rating,
                "review_date": review_date,
                "author": author,
                "title": title,
                "_source_id": str(source_id).strip() if source_id else "",
            }
        )
    return parsed, invalid_count


def normalize_text(value: Any) -> str:
    """Нормализует текст для поиска дубликатов."""
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def dedupe_keys(review: dict[str, Any]) -> tuple[str | None, str]:
    """Создаёт ключи для дедупликации по ID и по содержимому отзыва."""
    source_id = str(review.get("_source_id", "")).strip()
    id_key = f"id:{source_id}" if source_id else None
    content = "|".join(
        [
            normalize_text(review.get("author")),
            str(review.get("review_date", "")),
            str(review.get("rating", "")),
            normalize_text(review.get("title")),
            normalize_text(review.get("review_text")),
        ]
    )
    content_key = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return id_key, content_key


def remove_duplicates(
    reviews: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Удаляет дубликаты по ID источника и хешу нормализованного содержимого."""
    unique = []
    seen_ids = set()
    seen_content = set()
    duplicates = 0
    for review in reviews:
        id_key, content_key = dedupe_keys(review)
        if (id_key and id_key in seen_ids) or content_key in seen_content:
            duplicates += 1
            continue
        if id_key:
            seen_ids.add(id_key)
        seen_content.add(content_key)
        unique.append(review)
    return unique, duplicates


def select_latest_unique_reviews(
    reviews: list[dict[str, Any]], max_reviews: int
) -> tuple[list[dict[str, Any]], int]:
    """Оставляет последние уникальные отзывы; 0 означает отсутствие лимита."""
    unique, duplicates = remove_duplicates(reviews)
    unique.sort(key=lambda item: item.get("review_date", ""), reverse=True)
    if max_reviews > 0:
        unique = unique[:max_reviews]
    unique.sort(key=lambda item: item.get("review_date", ""))
    return unique, duplicates


def filter_and_count_eligible(
    raw_reviews: list[dict[str, Any]], ratings: list[int]
) -> int:
    """Считает уникальные русскоязычные отзывы выбранных оценок."""
    parsed, _ = parse_reviews(raw_reviews)
    eligible = [
        review
        for review in parsed
        if review["rating"] in ratings
        and detect_russian_language(review["review_text"], review["title"]) == "ru"
    ]
    unique, _ = remove_duplicates(eligible)
    return len(unique)


def get_date_coverage_message(reviews: list[dict[str, Any]]) -> str:
    """Формирует описание диапазона дат полученной выборки."""
    dates = sorted(
        str(item.get("review_date", ""))
        for item in reviews
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(item.get("review_date", "")))
    )
    if not dates:
        return "Нет корректных дат для оценки диапазона."
    return f"От {dates[0]} до {dates[-1]} (датированных отзывов: {len(dates)})."


# ---------------------------------------------------------------------------
# Сбор из JSON и RSS
# ---------------------------------------------------------------------------

def fetch_reviews_json(
    app_id: str,
    target_count: int,
    ratings: list[int],
    session: requests.Session,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Постранично загружает последние отзывы через JSON-маршрут App Store."""
    reviews: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "source": "JSON-интерфейс App Store",
        "pages": 0,
        "complete": False,
        "reason": "not_started",
        "message": "",
    }
    offset = 0
    seen_page_signatures: set[str] = set()

    for page_number in range(1, MAX_PAGES + 1):
        params = {
            "platform": "web",
            "limit": PAGE_SIZE,
            "sort": "recent",
            "offset": offset,
        }
        try:
            response = request_with_retries(
                session, REVIEWS_URL.format(app_id=app_id), params=params
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise SourceFormatError("JSON-источник вернул невалидный JSON.") from exc
            items, next_known, has_next = parse_json_reviews(payload)
        except (AppStoreError, requests.RequestException) as exc:
            report.update(reason="source_error", message=str(exc))
            break

        report["pages"] += 1
        if not items:
            if next_known and not has_next:
                report.update(
                    complete=True,
                    reason="empty_last_page",
                    message="Источник явно сообщил, что следующей страницы нет.",
                )
            else:
                report.update(
                    reason="empty_or_unknown_page",
                    message=(
                        "Получена пустая страница без подтверждённого признака конца. "
                        "Пустой ответ не считается доказательством отсутствия отзывов."
                    ),
                )
            break

        signature = hashlib.sha256(
            repr(items).encode("utf-8", errors="replace")
        ).hexdigest()
        if signature in seen_page_signatures:
            report.update(
                reason="repeated_page",
                message="Источник повторил страницу; дальнейшая загрузка остановлена.",
            )
            break
        seen_page_signatures.add(signature)

        reviews.extend(items)
        eligible_count = filter_and_count_eligible(reviews, ratings)
        if progress_callback:
            progress_callback(
                "JSON", report["pages"], len(reviews),
                f"Получено {len(reviews)} записей; подходящих русскоязычных: {eligible_count}.",
            )

        if target_count > 0 and eligible_count >= target_count:
            report.update(
                reason="target_reached",
                message=f"Набрано не менее {target_count} подходящих уникальных отзывов.",
            )
            break
        if not next_known:
            report.update(
                reason="next_page_unknown",
                message=(
                    "В ответе отсутствует признак следующей страницы. "
                    "Полноту истории подтвердить нельзя."
                ),
            )
            break
        if not has_next:
            report.update(
                complete=True,
                reason="no_next_page",
                message="Источник явно сообщил, что следующей страницы нет.",
            )
            break

        offset += len(items)
        if REQUEST_DELAY_SECONDS:
            time.sleep(REQUEST_DELAY_SECONDS)
    else:
        report.update(
            reason="max_pages_reached",
            message=f"Достигнут защитный предел в {MAX_PAGES} страниц.",
        )

    report["collected"] = len(reviews)
    return reviews, report


def fetch_reviews_rss(
    app_id: str,
    target_count: int,
    ratings: list[int],
    existing_reviews: list[dict[str, Any]],
    session: requests.Session,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Загружает RSS последних отзывов (обычно до 10 × 50 записей)."""
    reviews: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "source": "RSS Apple (последние отзывы)",
        "pages": 0,
        "complete": False,
        "reason": "not_started",
        "message": "",
    }

    for page_number in range(1, MAX_RSS_PAGES + 1):
        url = RSS_URL.format(page=page_number, app_id=app_id)
        try:
            response = request_with_retries(session, url)
            try:
                payload = response.json()
            except ValueError as exc:
                raise SourceFormatError("RSS вернул невалидный JSON.") from exc
            page_reviews = parse_rss_reviews(payload)

            if not page_reviews and page_number == 1:
                # Только один контролируемый повтор пустой первой страницы.
                response = request_with_retries(
                    session, url, params={"_": str(int(time.time()))}
                )
                try:
                    page_reviews = parse_rss_reviews(response.json())
                except ValueError as exc:
                    raise SourceFormatError("Повторный ответ RSS не является JSON.") from exc
        except (AppStoreError, requests.RequestException) as exc:
            report.update(reason="source_error", message=str(exc))
            break

        report["pages"] += 1
        if not page_reviews:
            report.update(
                reason="rss_empty_page",
                message=(
                    "RSS вернул пустую страницу. Это не доказывает, "
                    "что в App Store нет отзывов."
                ),
            )
            break

        reviews.extend(page_reviews)
        combined = existing_reviews + reviews
        eligible_count = filter_and_count_eligible(combined, ratings)
        if progress_callback:
            progress_callback(
                "RSS", report["pages"], len(reviews),
                f"RSS: получено {len(reviews)}; вместе с JSON подходящих: {eligible_count}.",
            )

        if target_count > 0 and eligible_count >= target_count:
            report.update(
                reason="target_reached",
                message=f"Набрано не менее {target_count} подходящих уникальных отзывов.",
            )
            break
        if page_number < MAX_RSS_PAGES and REQUEST_DELAY_SECONDS:
            time.sleep(REQUEST_DELAY_SECONDS)
    else:
        report.update(
            reason="rss_page_cap",
            message=(
                "Достигнут предел RSS в 10 страниц. Публичная лента обычно даёт "
                "до 500 последних записей и не является полным архивом."
            ),
        )

    if report["reason"] == "not_started":
        report.update(reason="stopped_unknown", message="Сбор RSS остановлен неизвестным образом.")
    report["collected"] = len(reviews)
    return reviews, report


# ---------------------------------------------------------------------------
# Подготовка CSV и статистики
# ---------------------------------------------------------------------------

def build_csv_bytes(reviews: list[dict[str, Any]]) -> bytes:
    """Создаёт CSV в UTF-8 с BOM и строго пятью столбцами."""
    rows = [{column: review.get(column, "") for column in CSV_COLUMNS} for review in reviews]
    dataframe = pd.DataFrame(rows, columns=CSV_COLUMNS)
    if not dataframe.empty:
        dataframe = dataframe.sort_values(
            by="review_date", ascending=True, kind="stable"
        )
    return dataframe.to_csv(
        index=False,
        encoding="utf-8-sig",
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\n",
    ).encode("utf-8-sig")


def save_csv_locally(csv_bytes: bytes, app_id: str) -> Path:
    """Сохраняет копию CSV в локальную папку exports рядом с проектом."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = OUTPUT_DIR / f"app_store_reviews_ru_{app_id}_{timestamp}.csv"
    output_path.write_bytes(csv_bytes)
    return output_path


def process_reviews(
    raw_reviews: list[dict[str, Any]], ratings: list[int], max_reviews: int
) -> tuple[list[dict[str, Any]], dict[str, int], int, int]:
    """Фильтрует по языку и оценкам, удаляет дубликаты и ограничивает результат."""
    parsed, invalid_count = parse_reviews(raw_reviews)
    language_counts = {"ru": 0, "other": 0, "unknown": 0}
    russian_reviews = []

    for review in parsed:
        language = detect_russian_language(review["review_text"], review["title"])
        language_counts[language] += 1
        if language == "ru":
            russian_reviews.append(review)

    rating_filtered = [
        review for review in russian_reviews if review["rating"] in ratings
    ]
    final_reviews, duplicates_removed = select_latest_unique_reviews(
        rating_filtered, max_reviews
    )
    return final_reviews, language_counts, invalid_count, duplicates_removed


# ---------------------------------------------------------------------------
# Streamlit-интерфейс
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Парсер отзывов App Store RU",
    page_icon="⭐",
    layout="wide",
)

st.title("Парсер отзывов российского App Store")
st.caption(
    "Сбор последних русскоязычных отзывов конкурирующих приложений. "
    "Результат можно скачать в CSV для Excel."
)

with st.sidebar:
    st.header("Настройки сбора")
    st.markdown("**Регион:** российский App Store (`ru`)")
    max_reviews = st.number_input(
        "Максимальное количество отзывов",
        min_value=0,
        max_value=1000,
        value=500,
        step=50,
        help=(
            "Максимум итоговых уникальных русскоязычных отзывов после фильтрации. "
            "0 — без пользовательского лимита, в пределах возможностей источников."
        ),
    )
    selected_ratings = st.multiselect(
        "Оценки",
        options=[1, 2, 3, 4, 5],
        default=[1, 2, 3, 4, 5],
        format_func=lambda value: f"{value} {'звезда' if value == 1 else 'звезды' if value in (2, 3, 4) else 'звёзд'}",
    )
    st.divider()
    st.markdown("**Источники данных**")
    st.write("1. JSON-интерфейс App Store — основной")
    st.write("2. RSS Apple — резервный, обычно до 500 последних отзывов")
    st.caption(
        "JSON-маршрут является внутренним интерфейсом сайта Apple и может "
        "временно ограничивать запросы HTTP 429."
    )

app_url = st.text_input(
    "Ссылка на приложение в App Store",
    placeholder="https://apps.apple.com/ru/app/example/id123456789",
    help="Можно вставить ссылку с любым регионом: ID будет извлечён, а проверка выполнится для магазина RU.",
)

run_button = st.button(
    "Собрать последние отзывы",
    type="primary",
    use_container_width=True,
    disabled=not app_url.strip() or not selected_ratings,
)

if run_button:
    status_box = st.status("Подготовка к сбору отзывов…", expanded=True)
    progress_bar = st.progress(0, text="Ожидание первого ответа источника")
    progress_text = st.empty()

    def update_progress(source: str, page: int, collected: int, message: str) -> None:
        """Обновляет прогресс-бар и пояснение в интерфейсе Streamlit."""
        if max_reviews:
            # Прогресс ориентировочный: часть записей будет исключена фильтрами.
            fraction = min(0.95, collected / max(max_reviews * 2, 1))
            progress_bar.progress(int(fraction * 100), text=f"Источник: {source}")
        else:
            progress_bar.progress(min(95, page % 96), text=f"Источник: {source}")
        progress_text.info(f"{source} · страница {page} · {message}")

    try:
        if not selected_ratings:
            raise ValueError("Выберите хотя бы одну оценку.")

        app_id = extract_app_id(app_url)
        status_box.write(f"ID приложения: `{app_id}`")
        session = make_session()

        status_box.write("Проверка доступности приложения в российском App Store…")
        app_info = validate_app(app_id, session)
        app_name = app_info.get("trackName", "Приложение App Store")
        status_box.write(f"Приложение найдено: **{app_name}**")

        status_box.write("Загрузка последних отзывов через основной JSON-источник…")
        json_reviews, json_report = fetch_reviews_json(
            app_id=app_id,
            target_count=int(max_reviews),
            ratings=selected_ratings,
            session=session,
            progress_callback=update_progress,
        )
        raw_reviews = list(json_reviews)
        source_reports = [json_report]

        json_eligible = filter_and_count_eligible(raw_reviews, selected_ratings)
        need_rss = ENABLE_RSS_FALLBACK and (
            int(max_reviews) == 0 or json_eligible < int(max_reviews)
        )
        if need_rss:
            status_box.write(
                "Основной источник не набрал установленное количество. "
                "Пробуем дополнить выборку RSS Apple…"
            )
            rss_reviews, rss_report = fetch_reviews_rss(
                app_id=app_id,
                target_count=int(max_reviews),
                ratings=selected_ratings,
                existing_reviews=raw_reviews,
                session=session,
                progress_callback=update_progress,
            )
            raw_reviews.extend(rss_reviews)
            source_reports.append(rss_report)

        final_reviews, language_counts, invalid_count, duplicates_removed = process_reviews(
            raw_reviews, selected_ratings, int(max_reviews)
        )
        parsed_reviews, _ = parse_reviews(raw_reviews)
        rating_count = sum(
            1 for review in parsed_reviews
            if detect_russian_language(review["review_text"], review["title"]) == "ru"
            and review["rating"] in selected_ratings
        )

        csv_bytes = build_csv_bytes(final_reviews)
        output_path = save_csv_locally(csv_bytes, app_id)
        progress_bar.progress(100, text="Обработка завершена")
        status_box.update(label="Сбор и обработка завершены", state="complete", expanded=False)

        st.subheader(f"Результаты: {app_name}")
        metric_columns = st.columns(4)
        metric_columns[0].metric("Получено записей", len(raw_reviews))
        metric_columns[1].metric("Русскоязычных", language_counts["ru"])
        metric_columns[2].metric("Удалено дубликатов", duplicates_removed)
        metric_columns[3].metric("Итоговых отзывов", len(final_reviews))

        detail_columns = st.columns(3)
        detail_columns[0].metric("Другие языки", language_counts["other"])
        detail_columns[1].metric("Язык неизвестен", language_counts["unknown"])
        detail_columns[2].metric("Некорректные записи", invalid_count)

        st.write(f"**Диапазон дат выборки:** {get_date_coverage_message(parsed_reviews)}")
        st.write(f"**После фильтрации по выбранным оценкам:** {rating_count}")
        st.write(f"**Сохранено в файл:** `{output_path}`")

        if int(max_reviews) and len(final_reviews) >= int(max_reviews):
            st.success(f"Получено {len(final_reviews)} уникальных отзывов — установленный лимит достигнут.")
        else:
            st.warning(
                "Получено меньше отзывов, чем запрошено. Сохранены все подходящие "
                "уникальные записи, доступные через использованные источники."
            )

        for report in source_reports:
            with st.expander(
                f"Источник: {report['source']} — {report['reason']}",
                expanded=not report.get("complete", False),
            ):
                st.write(f"Страниц обработано: {report.get('pages', 0)}")
                st.write(f"Получено записей: {report.get('collected', 0)}")
                st.write(f"Полнота подтверждена источником: {'да' if report.get('complete') else 'нет'}")
                if report.get("message"):
                    st.write(report["message"])

        if len(final_reviews) == 0:
            st.error(
                "Подходящих отзывов не найдено. CSV содержит только заголовки. "
                "Это не доказывает, что у приложения нет отзывов: причиной могут быть "
                "ограничения источников или выбранные оценки/язык."
            )

        dataframe = pd.DataFrame(final_reviews, columns=CSV_COLUMNS)
        st.subheader("Предпросмотр CSV")
        st.dataframe(dataframe, use_container_width=True, hide_index=True)

        filename = f"app_store_reviews_ru_{app_id}_latest.csv"
        st.download_button(
            label="Скачать CSV",
            data=csv_bytes,
            file_name=filename,
            mime="text/csv",
            type="primary",
            use_container_width=True,
        )

        st.info(
            "Ограничение: RSS Apple обычно предоставляет до 500 последних записей. "
            "Внутренний JSON-интерфейс может ограничивать доступ (например, HTTP 429). "
            "Поэтому достигнутое количество не означает, что это все отзывы приложения."
        )

    except (ValueError, AppStoreError, requests.RequestException) as exc:
        status_box.update(label="Сбор не завершён", state="error", expanded=True)
        st.error(str(exc))
        st.info(
            "Проверьте ссылку, доступность приложения в российском App Store "
            "и попробуйте повторить сбор позднее, если источник вернул HTTP 429."
        )
    except Exception as exc:  # Защита интерфейса от необработанных ошибок.
        status_box.update(label="Непредвиденная ошибка", state="error", expanded=True)
        st.exception(exc)

st.divider()
st.caption(
    "CSV содержит пять полей: review_text, rating, review_date, author, title. "
    "Файл сохраняется в папку exports рядом с приложением и доступен для скачивания кнопкой выше."
)
