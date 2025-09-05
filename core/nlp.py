from __future__ import annotations

"""Утилиты для обработки естественного языка."""

import re
from functools import lru_cache

import pymorphy2
from num2words import num2words

from core.logging_json import configure_logging

# ────────────────────────── 0. ЛОГИРОВАНИЕ ─────────────────────────
# Настраиваем отдельный логгер для модуля NLP, чтобы удобно отслеживать
# все этапы нормализации текста.
log = configure_logging("core.nlp")

# ────────────────────────── 1. КОНСТАНТЫ ───────────────────────────
_morph = pymorphy2.MorphAnalyzer()
_word_re = re.compile(r"[\w-]+")
# Разрешённые символы для синтеза речи: буквы, цифры, пробелы и базовая
# пунктуация. Всё остальное будет удалено при нормализации.
_ALLOWED_CHARS_RE = re.compile(r"[^0-9a-zA-Zа-яА-ЯёЁ.,!?\-\s]")

__all__ = [
    "normalize",
    "numbers_to_words",
    "remove_spaces_in_numbers",
    "normalize_tts_text",
]

@lru_cache(maxsize=10000)
def _normalize_word(word: str) -> str:
    return _morph.parse(word)[0].normal_form

def normalize(text: str) -> str:
    """Приводит все слова в *text* к начальной форме."""
    words = _word_re.findall(text.lower())
    return " ".join(_normalize_word(w) for w in words)


def remove_spaces_in_numbers(text: str) -> str:
    """Удаляет пробелы, встречающиеся между цифрами.

    Используем жадные просмотр назад/вперёд, чтобы удалить **все** пробелы
    внутри группы цифр за один проход, например ``"1 0 0"`` → ``"100"``.
    """
    return re.sub(r"(?<=\d)\s+(?=\d)", "", text)


def numbers_to_words(text: str) -> str:
    """Заменяет цифры в *text* на русские слова."""
    for number in re.findall(r"\d+", text):
        text = text.replace(number, num2words(int(number), lang="ru"))
    return text


def normalize_tts_text(text: str) -> str:
    """Подготавливает строку *text* для передачи в TTS.

    Выполняются следующие шаги:

    1. Удаляются пробелы внутри чисел, чтобы корректно преобразовать их
       в слова.
    2. Все цифры заменяются на русские слова.
    3. Удаляются лишние символы, не влияющие на озвучку.
    4. Схлопываются повторяющиеся знаки препинания и пробелы.

    Возвращается очищенный текст, пригодный для синтеза речи.
    """

    original = text

    # 1) Убираем пробелы внутри чисел: «1 000» -> «1000»
    text = remove_spaces_in_numbers(text)

    # 2) Преобразуем все числа в русские слова
    text = numbers_to_words(text)

    # 3) Удаляем все символы кроме разрешённых
    text = _ALLOWED_CHARS_RE.sub(" ", text)

    # 4) Схлопываем повторяющиеся знаки препинания: «!!!» -> «!»
    text = re.sub(r"[.!?]{2,}", lambda m: m.group(0)[0], text)

    # 5) Нормализуем последовательности пробелов
    text = re.sub(r"\s+", " ", text).strip()

    log.debug("normalize_tts_text original=%r normalized=%r", original, text)
    return text
