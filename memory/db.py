"""Хранилище на SQLite и миграции для памяти Jarvis."""

from __future__ import annotations

# Стандартные библиотеки
import sqlite3
import time
import logging
import json
from pathlib import Path
import os

# Для шифрования конфиденциальных полей используем симметричный алгоритм
from cryptography.fernet import Fernet

# Путь к файлу БД, создаётся рядом с модулем
DB_PATH = Path(__file__).with_name("memory.sqlite3")

# SQL‑скрипты для создания таблиц и индексов
SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        payload TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)
    """,
    """
    CREATE TABLE IF NOT EXISTS presence_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        start_ts INTEGER NOT NULL,
        end_ts INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        ts INTEGER NOT NULL,
        processed INTEGER NOT NULL DEFAULT 0,
        reason_code TEXT,
        fingerprint TEXT
    )
    """,
    """
    ALTER TABLE suggestions ADD COLUMN reason_code TEXT
    """,
    """
    ALTER TABLE suggestions ADD COLUMN fingerprint TEXT
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_suggestions_fingerprint
        ON suggestions(fingerprint)
    """,
    """
    -- Таблица для хранения откликов пользователей на подсказки
    CREATE TABLE IF NOT EXISTS suggestion_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        suggestion_id INTEGER NOT NULL,
        response_text TEXT,
        accepted INTEGER NOT NULL,
        ts INTEGER NOT NULL,
        FOREIGN KEY (suggestion_id) REFERENCES suggestions(id) ON DELETE CASCADE
    )
    """,
    """
    -- Индекс ускоряет выборку отзывов по ID подсказки
    CREATE INDEX IF NOT EXISTS idx_suggestion_feedback_suggestion_id
        ON suggestion_feedback(suggestion_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS timers (
        label   TEXT PRIMARY KEY,
        typ     TEXT NOT NULL,
        end_ts  INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS context_items (
        key TEXT PRIMARY KEY,
        value TEXT,
        ts INTEGER NOT NULL
    )
    """,
    """
    -- Таблица для эпизодической памяти: хранит события с эмбеддингами
    CREATE TABLE IF NOT EXISTS episodic_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        text TEXT NOT NULL,
        embedding TEXT NOT NULL,
        meta TEXT
    )
    """,
    """
    -- Таблица связывает события с их метками для быстрого поиска
    CREATE TABLE IF NOT EXISTS event_labels (
        event_id INTEGER NOT NULL,
        label    TEXT    NOT NULL,
        PRIMARY KEY (event_id, label),
        FOREIGN KEY (event_id) REFERENCES episodic_memory(id) ON DELETE CASCADE
    )
    """,
    """
    -- Индекс ускоряет выборку событий по метке
    CREATE INDEX IF NOT EXISTS idx_event_labels_label
        ON event_labels(label)
    """,
    """
    -- Таблица для семантической памяти: факты и знания
    CREATE TABLE IF NOT EXISTS semantic_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        text TEXT NOT NULL,
        embedding TEXT NOT NULL,
        meta TEXT
    )
    """,
    """
    -- Хранение ежедневного дайджеста с приоритетами и настроением
    CREATE TABLE IF NOT EXISTS daily_digest (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        digest TEXT NOT NULL,
        priorities TEXT,
        mood INTEGER
    )
    """,
    """
    -- Индекс для быстрого поиска и очистки по временным меткам
    CREATE INDEX IF NOT EXISTS idx_daily_digest_ts ON daily_digest(ts)
    """,
    """
    -- История изменений настроения и текстовых профилей
    CREATE TABLE IF NOT EXISTS mood_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        valence REAL NOT NULL,
        arousal REAL NOT NULL,
        source TEXT,
        profile TEXT
    )
    """,
]

# Удерживаем события не дольше двух недель
RETENTION_SECONDS = 14 * 24 * 3600  # две недели

# Политика хранения дневных дайджестов
DIGEST_RETENTION_DAYS = 30  # сохраняем дайджесты за последние 30 дней
MAX_DIGESTS = 100  # и не более 100 последних записей

# ---------------------------------------------------------------------------
# Работа с ключом шифрования
# ---------------------------------------------------------------------------

# Имя переменной окружения, в которой ожидается ключ
_KEY_ENV_VAR = "JARVIS_DB_KEY"


def _get_cipher() -> Fernet:
    """Создать объект ``Fernet`` для шифрования/дешифрования.

    Ключ берётся из переменной окружения ``JARVIS_DB_KEY``.
    Если ключ не задан, генерируем исключение, чтобы разработчик явно
    указал его в окружении. Такая мера исключает хранение ключа в коде.
    """

    key = os.environ.get(_KEY_ENV_VAR)
    if not key:
        raise RuntimeError(
            "Не задан ключ шифрования в переменной окружения JARVIS_DB_KEY"
        )
    return Fernet(key.encode() if not isinstance(key, bytes) else key)


def encrypt(data: str) -> str:
    """Зашифровать строку ``data`` и вернуть base64‑представление."""

    cipher = _get_cipher()
    token = cipher.encrypt(data.encode("utf-8"))
    logging.getLogger(__name__).debug("Строка зашифрована")
    return token.decode("utf-8")


def decrypt(token: str) -> str:
    """Расшифровать строку, полученную из :func:`encrypt`."""

    cipher = _get_cipher()
    data = cipher.decrypt(token.encode("utf-8"))
    logging.getLogger(__name__).debug("Строка расшифрована")
    return data.decode("utf-8")


def get_connection(retries: int = 5, delay: float = 0.2) -> sqlite3.Connection:
    """Вернуть подключение SQLite с миграциями и ротацией.

    При активной записи из нескольких потоков SQLite иногда выдаёт
    ``OperationalError: database is locked``. Чтобы не терять события, мы
    повторяем попытку подключения и коммита несколько раз с небольшой
    паузой. Такая стратегия значительно повышает стабильность работы
    подсистемы памяти.

    :param retries: число повторных попыток при блокировке БД
    :param delay: задержка между попытками в секундах
    """

    logger = logging.getLogger(__name__)

    for attempt in range(1, retries + 1):
        conn: sqlite3.Connection | None = None
        try:
            # ``timeout`` и ``busy_timeout`` заставляют SQLite подождать
            # освобождения файла, вместо мгновенного выброса исключения
            conn = sqlite3.connect(DB_PATH, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 30000")

            # Выполняем обслуживание базы перед отдачей соединения
            _migrate(conn)  # миграции
            _rotate_events(conn)  # очистка старых событий
            _cleanup_timers(conn)  # удаление просроченных таймеров
            _cleanup_old_digests(
                conn, DIGEST_RETENTION_DAYS, MAX_DIGESTS
            )  # очистка дайджестов

            conn.commit()
            return conn
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
            if "locked" in str(exc).lower() and attempt < retries:
                # Подробно логируем, чтобы понимать частоту блокировок
                logger.warning(
                    "База данных заблокирована, повторяем попытку",
                    extra={"ctx": {"attempt": attempt, "retries": retries}},
                )
                time.sleep(delay)
            else:
                logger.exception("Не удалось получить подключение к БД")
                raise


def _migrate(conn: sqlite3.Connection) -> None:
    """Прогоняем DDL-миграции, игнорируя уже применённые шаги."""
    for ddl in SCHEMA:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as exc:
            # Если столбец уже существует или таблица создана, SQLite выбросит
            # ``OperationalError``. Для идемпотентности миграций такие ошибки
            # подавляются, но логируются для отладки.
            logging.getLogger(__name__).debug("migration skipped: %s", exc)


def _rotate_events(conn: sqlite3.Connection) -> None:
    """Удаляем из таблицы events записи старше порога."""
    cutoff = int(time.time() - RETENTION_SECONDS)
    conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))


def _cleanup_timers(conn: sqlite3.Connection) -> None:
    """Удаляем старые записи о таймерах из таблицы ``timers``.

    Таймеры остаются в базе до подтверждения пользователем, поэтому
    чистим только те, что завершились более суток назад.
    """
    now = int(time.time())
    cutoff = now - 24 * 3600  # оставляем информацию за последние 24 часа
    conn.execute("DELETE FROM timers WHERE end_ts <= ?", (cutoff,))


def _cleanup_old_digests(
    conn: sqlite3.Connection,
    retention_days: int | None = DIGEST_RETENTION_DAYS,
    max_count: int | None = MAX_DIGESTS,
) -> int:
    """Удалить устаревшие записи из таблицы ``daily_digest``.

    Поддерживаются две стратегии хранения, которые можно комбинировать:
    по времени и по максимальному количеству записей. Это позволяет
    контролировать размер базы данных и упрощает последующий анализ.

    :param retention_days: Максимальный возраст записи в днях, ``None`` –
        не ограничивать по времени.
    :param max_count: Максимальное число последних записей, которое
        следует оставить, ``None`` – не ограничивать по количеству.
    :return: Общее число удалённых строк.
    """

    total_deleted = 0

    # --- Удаление по времени ------------------------------------------------
    if retention_days is not None:
        cutoff = int(time.time() - retention_days * 24 * 3600)
        cur = conn.execute("DELETE FROM daily_digest WHERE ts < ?", (cutoff,))
        deleted_time = cur.rowcount
        total_deleted += deleted_time
    else:
        deleted_time = 0

    # --- Ограничение по количеству -----------------------------------------
    deleted_count = 0
    if max_count is not None:
        # Выбираем идентификаторы записей, которые превышают лимит по числу
        cur = conn.execute(
            "SELECT id FROM daily_digest ORDER BY ts DESC LIMIT -1 OFFSET ?",
            (max_count,),
        )
        ids = [row[0] for row in cur.fetchall()]
        if ids:
            cur = conn.execute(
                f"DELETE FROM daily_digest WHERE id IN ({','.join('?' for _ in ids)})",
                ids,
            )
            deleted_count = cur.rowcount
            total_deleted += deleted_count

    logging.getLogger(__name__).debug(
        "очистка дайджестов",
        extra={
            "ctx": {
                "by_time": deleted_time,
                "by_count": deleted_count,
                "total": total_deleted,
            }
        },
    )
    return total_deleted


# --- Mood helpers ----------------------------------------------------------
# Единый ключ, под которым в ``context_items`` хранится настроение
MOOD_KEY = "emotion:mood"
# Устаревший ключ для координат valence/arousal (используется при миграции)
_LEGACY_MOOD_STATE_KEY = "emotion:mood_state"
# Ключ для хранения актуальных приоритетов на завтра
PRIORITIES_KEY = "reflection:priorities"


def get_mood(trace_id: str | None = None) -> dict:
    """Вернуть текущее настроение ``{valence, arousal}``.

    При необходимости выполняется миграция со старого формата, где
    использовалось поле ``level`` и отдельный ключ ``emotion:mood_state``.
    """

    start = time.time()
    migrated = False
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM context_items WHERE key=?", (MOOD_KEY,)
        ).fetchone()

    mood: dict[str, float]
    if row:
        try:
            data = json.loads(row["value"])
            if not isinstance(data, dict):
                raise TypeError
        except (json.JSONDecodeError, TypeError):
            # В старом формате под ключом хранилось число уровня
            data = {"level": int(row["value"]) if row["value"] else 0}
            migrated = True
        mood = {
            "valence": float(data.get("valence", 0.0)),
            "arousal": float(data.get("arousal", 0.0)),
        }
    else:
        mood = {"valence": 0.0, "arousal": 0.0}

    if migrated or row is None or any(k not in data for k in ("valence", "arousal")):
        with get_connection() as conn:
            row_state = conn.execute(
                "SELECT value FROM context_items WHERE key=?",
                (_LEGACY_MOOD_STATE_KEY,),
            ).fetchone()
        if row_state:
            try:
                legacy = json.loads(row_state["value"])
                mood["valence"] = float(legacy.get("valence", 0.0))
                mood["arousal"] = float(legacy.get("arousal", 0.0))
            except Exception:
                pass
        _store_mood(mood)
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM context_items WHERE key=?",
                (_LEGACY_MOOD_STATE_KEY,),
            )
        migrated = True

    duration = int((time.time() - start) * 1000)
    logging.getLogger(__name__).info(
        json.dumps(
            {
                "event": "db.get_mood",
                "trace_id": trace_id,
                "duration_ms": duration,
                "valence": mood["valence"],
                "arousal": mood["arousal"],
                "migrated": migrated,
            },
            ensure_ascii=False,
        )
    )
    return mood


def _store_mood(mood: dict) -> None:
    """Вспомогательная функция сохранения настроения без логирования."""

    ts = int(time.time())
    payload = json.dumps(mood, ensure_ascii=False)
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO context_items (key, value, ts) VALUES (?, ?, ?)",
            (MOOD_KEY, payload, ts),
        )


def set_mood(mood: dict, trace_id: str | None = None) -> None:
    """Сохранить настроение, поддерживая частичное обновление полей."""

    start = time.time()
    current = get_mood()
    current.update(mood)
    normalized = {
        "valence": float(current.get("valence", 0.0)),
        "arousal": float(current.get("arousal", 0.0)),
    }
    _store_mood(normalized)
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM context_items WHERE key=?",
            (_LEGACY_MOOD_STATE_KEY,),
        )
    duration = int((time.time() - start) * 1000)
    logging.getLogger(__name__).info(
        json.dumps(
            {
                "event": "db.set_mood",
                "trace_id": trace_id,
                "duration_ms": duration,
                "valence": normalized["valence"],
                "arousal": normalized["arousal"],
            },
            ensure_ascii=False,
        )
    )

def set_priorities(priorities: str) -> None:
    """Сохранить список приоритетов на следующий день."""
    ts = int(time.time())
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO context_items (key, value, ts) VALUES (?, ?, ?)",
            (PRIORITIES_KEY, priorities, ts),
        )


def add_daily_digest(digest: str, priorities: str | None, mood: int | None) -> int:
    """Записать результат вечерней рефлексии в отдельную таблицу."""
    ts = int(time.time())
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO daily_digest (ts, digest, priorities, mood) VALUES (?, ?, ?, ?)",
            (ts, digest, priorities, mood),
        )
        digest_id = int(cur.lastrowid)
        logging.getLogger(__name__).debug(
            "сохранён новый дайджест", extra={"ctx": {"id": digest_id}}
        )
        return digest_id


def get_last_digest() -> dict | None:
    """Вернуть последний сохранённый дайджест или ``None``."""

    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, ts, digest, priorities, mood FROM daily_digest ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    if row:
        result = dict(row)
        logging.getLogger(__name__).debug(
            "получен последний дайджест", extra={"ctx": {"id": result["id"]}}
        )
        return result
    logging.getLogger(__name__).debug("дайджестов в БД не найдено")
    return None


def list_digests(limit: int = 100) -> list[dict]:
    """Вернуть список последних ``limit`` дайджестов."""

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, ts, digest, priorities, mood FROM daily_digest ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = [dict(r) for r in rows]
    logging.getLogger(__name__).debug(
        "получен список дайджестов", extra={"ctx": {"count": len(result)}}
    )
    return result


def cleanup_old_digests(
    retention_days: int | None = DIGEST_RETENTION_DAYS,
    max_count: int | None = MAX_DIGESTS,
) -> int:
    """Публичная обёртка для удаления старых дайджестов.

    Аргументы аналогичны :func:`_cleanup_old_digests` и позволяют гибко
    управлять политикой хранения без прямого доступа к соединению БД.
    """

    with get_connection() as conn:
        deleted = _cleanup_old_digests(conn, retention_days, max_count)
    return deleted


def clear_daily_digest() -> int:
    """Полностью удалить содержимое таблицы ``daily_digest``.

    Возвращает количество удалённых записей. Функция полезна для
    ручной очистки дневных дайджестов при отладке или обнаружении
    некорректных данных.
    """

    with get_connection() as conn:
        cur = conn.execute("DELETE FROM daily_digest")
        deleted = cur.rowcount
    logging.getLogger(__name__).debug(
        "daily digest cleared", extra={"ctx": {"count": deleted}}
    )
    return deleted


def clear_episodic_memory() -> int:
    """Очистить таблицу ``episodic_memory`` и связанные метки.

    Используется для удаления всех эпизодических воспоминаний. Возвращает
    количество удалённых записей из основной таблицы.
    """

    with get_connection() as conn:
        conn.execute("DELETE FROM event_labels")
        cur = conn.execute("DELETE FROM episodic_memory")
        deleted = cur.rowcount
    logging.getLogger(__name__).debug(
        "episodic memory cleared", extra={"ctx": {"count": deleted}}
    )
    return deleted


def clear_semantic_memory() -> int:
    """Очистить таблицу ``semantic_memory``.

    Удаляет все факты и возвращает число затронутых строк, позволяя
    администратору при необходимости сбросить знания ассистента.
    """

    with get_connection() as conn:
        cur = conn.execute("DELETE FROM semantic_memory")
        deleted = cur.rowcount
    logging.getLogger(__name__).debug(
        "semantic memory cleared", extra={"ctx": {"count": deleted}}
    )
    return deleted


# --- Mood history helpers --------------------------------------------------

def add_mood_history(valence: float, arousal: float, source: str, profile: str) -> None:
    """Добавить запись о настроении в таблицу ``mood_history``.

    ``source`` описывает, что именно повлияло на изменение настроения
    (например, ``dialog.success``), а ``profile`` содержит текстовый
    ответ LLM.  Благодаря этому можно анализировать, как события
    отражаются на состоянии персонажа.
    """

    ts = int(time.time())
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO mood_history (ts, valence, arousal, source, profile) VALUES (?, ?, ?, ?, ?)",
            (ts, valence, arousal, source, profile),
        )
    logging.getLogger(__name__).info(
        json.dumps(
            {
                "event": "db.add_mood_history",
                "source": source,
                "valence": valence,
                "arousal": arousal,
            },
            ensure_ascii=False,
        )
    )


def get_mood_history(limit: int = 100) -> list[dict]:
    """Вернуть последние записи из ``mood_history``.

    Возвращается список словарей со значениями ``ts``, ``valence``,
    ``arousal``, ``source`` и ``profile``.
    """

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT ts, valence, arousal, source, profile FROM mood_history ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
