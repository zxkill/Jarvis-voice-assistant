import re
import sys
import types

# Создаём заглушку для pymorphy2, чтобы не тянуть реальные словари
# и избежать несовместимости с текущей версией Python в тестовой среде.
dummy_morph = types.SimpleNamespace(
    MorphAnalyzer=lambda: types.SimpleNamespace(
        parse=lambda self, word: [types.SimpleNamespace(normal_form=word)]
    )
)
sys.modules.setdefault("pymorphy2", dummy_morph)

from core.nlp import normalize_tts_text


def test_basic_normalization() -> None:
    """Проверяем комплексную нормализацию текста."""
    text = "Привет,   мир!!!  Мне  123  рубля??"
    assert (
        normalize_tts_text(text)
        == "Привет, мир! Мне сто двадцать три рубля?"
    )


def test_removes_symbols() -> None:
    """Убираем посторонние символы, оставляя только полезные слова."""
    text = "Тест @#%* (строка)"
    assert normalize_tts_text(text) == "Тест строка"


def test_digits_without_spaces() -> None:
    """Пробелы внутри цифр удаляются перед их преобразованием в слова."""
    text = "код 1 0 0"
    result = normalize_tts_text(text)
    assert "сто" in result and not re.search(r"\d", result)


def test_punctuation_collapse() -> None:
    """Повторяющиеся знаки препинания схлопываются до одного."""
    assert normalize_tts_text("Что??") == "Что?"
