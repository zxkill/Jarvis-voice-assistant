from __future__ import annotations

"""Утилиты для обработки естественного языка."""

import re
from functools import lru_cache

import pymorphy2
from num2words import num2words

_morph = pymorphy2.MorphAnalyzer()
_word_re = re.compile(r"[\w-]+")

__all__ = ["normalize", "numbers_to_words", "remove_spaces_in_numbers"]

@lru_cache(maxsize=10000)
def _normalize_word(word: str) -> str:
    return _morph.parse(word)[0].normal_form

def normalize(text: str) -> str:
    """Приводит все слова в *text* к начальной форме."""
    words = _word_re.findall(text.lower())
    return " ".join(_normalize_word(w) for w in words)


def remove_spaces_in_numbers(text: str) -> str:
    """Удаляет пробелы внутри чисел."""
    return re.sub(r"(\d)\s+(\d)", r"\1\2", text)


def numbers_to_words(text: str) -> str:
    """Заменяет цифры в *text* на русские слова."""
    for number in re.findall(r"\d+", text):
        text = text.replace(number, num2words(int(number), lang="ru"))
    return text
