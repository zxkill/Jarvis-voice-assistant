"""Вспомогательные функции для обработки ответов ассистента.

Этот модуль содержит утилиту :func:`extract_reply`, которая извлекает
поле ``reply`` из JSON-строки.  LLM теперь возвращает ответы в формате
``{"accepted": bool, "reply": str}``, поэтому перед озвучкой и
отправкой сообщений в Telegram необходимо вытащить только текст
ответа, игнорируя остальную структуру.
"""

from __future__ import annotations

import json
from core.logging_json import configure_logging

log = configure_logging("utils.reply")


def extract_reply(text: str) -> str:
    """Вернуть значение поля ``reply`` из JSON-ответа.

    Функция пытается распознать в *text* JSON-структуру и извлекает из
    неё значение ключа ``reply``.  Поддерживаются как чистые строки
    JSON, так и ответы, заключённые в блок кода вида `````json``.

    Если разобрать строку не удалось или поле ``reply`` отсутствует,
    возвращается исходный текст без изменений.
    """

    raw = text.strip()
    try:
        # Если ответ заключён в тройные обратные кавычки, убираем их
        # вместе с указанием языка (```json).
        if raw.startswith("```") and raw.endswith("```"):
            lines = raw.splitlines()
            # Первая строка может содержать язык: ```json
            first = lines.pop(0)
            if lines:
                last = lines.pop(-1)  # закрывающие ```
            raw = "\n".join(lines)
            log.debug("удалён блок кода: %r", first)
        data = json.loads(raw)
        if isinstance(data, dict) and "reply" in data:
            reply = str(data.get("reply", ""))
            log.debug("извлечён текст ответа: %r", reply)
            return reply
    except json.JSONDecodeError:
        # Некорректный JSON — не страшно, просто оставляем исходный текст.
        log.debug("не удалось разобрать JSON: %r", text)
    except Exception:  # pragma: no cover - на всякий случай логируем сбои
        log.exception("ошибка при извлечении ответа")
    return text
