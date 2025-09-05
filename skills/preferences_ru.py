"""Навык для сохранения пользовательских предпочтений."""

# Стандартные библиотеки
import logging
import re

from memory.preferences import save_preference

# Паттерны, по которым будет вызываться навык. Используется fuzzy-сопоставление,
# поэтому достаточно указать ключевую фразу.
PATTERNS = ["запомни"]

logger = logging.getLogger(__name__)


def _extract_preference(text: str) -> str:
    """Выделить формулировку предпочтения из произвольной фразы.

    Ожидается выражение вида «запомни, что я не ем хлеб». Функция удаляет
    служебные слова и возвращает чистый текст предпочтения.
    """

    # Отбрасываем вводную часть «запомни» и возможное «что» сразу после неё
    pref = re.sub(r"^.*?запомни\s*(что)?", "", text, flags=re.IGNORECASE)
    return pref.strip(" ,.")


def handle(text: str, trace_id: str | None = None) -> str:
    """Основная точка входа для навыка.

    :param text: исходная фраза пользователя
    :param trace_id: идентификатор диалога для сквозного логирования
    :return: текст подтверждения для отправки пользователю
    """

    logger.debug("Разбор предпочтения из фразы: %s", text, extra={"trace_id": trace_id})
    preference = _extract_preference(text)
    if not preference:
        logger.warning("Не удалось выделить текст предпочтения", extra={"trace_id": trace_id})
        return "Не понял, что нужно запомнить"

    save_preference(preference)
    logger.info("Запомнено предпочтение: %s", preference, extra={"trace_id": trace_id})
    return f"Запомнил: {preference}"
