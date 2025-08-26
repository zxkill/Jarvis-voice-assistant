# skills/chit_chat_ru.py
"""Простейший small-talk на русском языке."""

from __future__ import annotations

import random
from display import get_driver, DisplayItem

# Набор коротких фраз для лёгкого общения.
PHRASES = [
    "Как дела?",
    "Пора размяться!",
    "Чем займёмся?",
]

# Паттерны, по которым ассистент ответит одной из фраз.
PATTERNS = [
    "как дела",
    "пора размяться",
    "давай поболтаем",
]


def _choose_phrase() -> str:
    """Вернуть случайную фразу из списка ``PHRASES``."""
    return random.choice(PHRASES)


def random_phrase() -> str:
    """Публичная функция для генерации случайной фразы.

    Используется как самим скиллом, так и другими компонентами
    (например, проактивным движком) для вставки лёгких реплик.
    """
    return _choose_phrase()


def handle(text: str) -> str:
    """Обработчик пользовательской команды."""
    phrase = _choose_phrase()
    # Отправляем текст на дисплей для визуального подтверждения.
    driver = get_driver()
    driver.draw(DisplayItem(kind="text", payload=phrase))
    return phrase
