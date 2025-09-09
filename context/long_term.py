"""Долгосрочный контекст на основе **эпизодической памяти**.

Ранее события дня дублировались в таблице ``context_items`` и в
эпизодической памяти. Теперь используется только таблица
``episodic_memory``: в столбце ``meta`` хранится JSON с текстом события и
его метками. Такой подход упрощает поддержку и исключает расхождения
между источниками данных.
"""

from __future__ import annotations

import logging
from typing import Iterable, List

from memory.db import get_connection
from memory.long_memory import store_event

# Логгер для наблюдения за операциями долговременной памяти
logger = logging.getLogger(__name__)


def add_daily_event(text: str, labels: Iterable[str]) -> str:
    """Сохранить событие дня в эпизодической памяти и вернуть его id.

    :param text: текстовое описание события
    :param labels: набор меток для последующего поиска
    :return: идентификатор записи в виде строки
    """

    # Преобразуем список меток в обычный список, чтобы можно было
    # сериализовать его в JSON. Дополнительные проверки позволяют
    # заметить проблемы уже на этапе сохранения.
    labels_list = list(labels)
    logger.debug("add_daily_event: сохраняем %s с метками %s", text, labels_list)

    try:
        # Сохраняем событие в таблице ``episodic_memory``. Функция
        # ``store_event`` сама вычисляет эмбеддинг и возвращает id
        # добавленной записи.
        event_id = store_event(text, {"labels": labels_list})
    except Exception:
        # Подробный лог поможет понять причину ошибки сохранения.
        logger.exception("Не удалось сохранить событие дня")
        raise

    logger.debug("add_daily_event: событие сохранено с id=%s", event_id)

    # После сохранения события привязываем каждую метку к отдельной записи
    # в таблице ``event_labels``. Это позволит фильтровать события
    # непосредственно на уровне SQL без полного обхода таблицы.
    try:
        with get_connection() as conn:
            for lbl in labels_list:
                conn.execute(
                    "INSERT OR IGNORE INTO event_labels (event_id, label) VALUES (?, ?)",
                    (int(event_id), lbl),
                )
    except Exception:
        # Логируем, но не прерываем выполнение: основное событие уже
        # сохранено, а проблема с метками не должна ломать основной поток.
        logger.exception("add_daily_event: не удалось сохранить метки %s", labels_list)

    # Возвращаем id в виде строки для обратной совместимости с прошлой
    # версией API, где ключ представлялся строкой.
    return str(event_id)


def get_events_by_label(label: str) -> List[str]:
    """Вернуть список текстов событий, помеченных меткой ``label``.

    В новой реализации фильтрация выполняется на стороне SQLite с
    использованием вспомогательной таблицы ``event_labels``. Благодаря
    этому нет необходимости загружать все записи в память и разбирать
    JSON-метаданные, что значительно ускоряет запросы на больших объёмах
    данных.
    """

    with get_connection() as conn:
        # Выполняем объединение ``episodic_memory`` и ``event_labels``
        # и выбираем только тексты событий с нужной меткой. Индекс по
        # столбцу ``label`` обеспечивает высокую скорость выборки.
        rows = conn.execute(
            """
            SELECT e.text
              FROM episodic_memory AS e
              JOIN event_labels AS l ON e.id = l.event_id
             WHERE l.label = ?
             ORDER BY e.ts
            """,
            (label,),
        ).fetchall()

    events = [str(row["text"]) for row in rows]
    logger.debug("get_events_by_label(%s): найдено %d событий", label, len(events))
    return events
