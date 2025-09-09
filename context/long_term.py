"""Долгосрочный контекст на основе **эпизодической памяти**.

Ранее события дня дублировались в таблице ``context_items`` и в
эпизодической памяти. Теперь используется только таблица
``episodic_memory``: в столбце ``meta`` хранится JSON с текстом события и
его метками. Такой подход упрощает поддержку и исключает расхождения
между источниками данных.
"""

from __future__ import annotations

import json
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
    # Возвращаем id в виде строки для обратной совместимости с прошлой
    # версией API, где ключ представлялся строкой.
    return str(event_id)


def get_events_by_label(label: str) -> List[str]:
    """Вернуть список текстов событий, помеченных меткой ``label``.

    Проходим по всей таблице ``episodic_memory`` и фильтруем записи,
    где в метаданных присутствует указанная метка. Такой подход остаётся
    простым и надёжным, хотя и не самый быстрый для больших объёмов.
    """

    with get_connection() as conn:
        # Загружаем текст события и его метаданные. Здесь может быть
        # большое количество строк, поэтому включаем подробный лог только
        # на уровне отладки.
        rows = conn.execute("SELECT text, meta FROM episodic_memory").fetchall()

    events: List[str] = []
    for row in rows:
        try:
            meta = json.loads(row["meta"])
        except Exception:
            # Повреждённые JSON-метаданные не должны ломать всю выборку.
            logger.debug(
                "get_events_by_label: пропуск записи с повреждёнными данными",
                extra={"label": label},
            )
            continue

        if not isinstance(meta, dict):
            logger.debug(
                "get_events_by_label: неожиданный формат метаданных %r",
                meta,
                extra={"label": label},
            )
            continue

        # Если искомая метка присутствует, добавляем текст события в
        # итоговый список.
        if label in meta.get("labels", []):
            events.append(str(row["text"]))

    logger.debug("get_events_by_label(%s): найдено %d событий", label, len(events))
    return events
