"""Тесты для проверки документации проекта.

Все проверки могут быть пропущены, если переменная окружения
``DOCS_TESTS`` не установлена. Это позволяет запускать основную
проверку покрытия без необходимости наличия файлов документации в
окружениях, где они не требуются.
"""

import logging
import os
from pathlib import Path

import pytest

# Настраиваем базовый логер для вывода отладочной информации
logger = logging.getLogger(__name__)


@pytest.mark.skipif(
    not os.environ.get("DOCS_TESTS"),
    reason="Документация не проверяется без DOCS_TESTS=1",
)
def test_docs_structure() -> None:
    """Убеждаемся, что каталог docs содержит все обязательные файлы."""
    docs_path = Path(__file__).resolve().parents[2] / "docs"
    logger.info("Проверяем каталог документации: %s", docs_path)
    assert docs_path.is_dir(), "Каталог docs отсутствует"

    required = {
        "setup_examples.md",
        "modules_interaction_scheme.md",
        "security_policies.md",
    }
    existing = {p.name for p in docs_path.iterdir()}
    logger.info("Найдены файлы: %s", existing)
    assert required.issubset(existing), "Отсутствуют файлы документации"


@pytest.mark.skipif(
    not os.environ.get("DOCS_TESTS"),
    reason="Документация не проверяется без DOCS_TESTS=1",
)
def test_readme_has_sections() -> None:
    """Проверяем, что README содержит ключевые разделы."""
    readme = Path(__file__).resolve().parents[2] / "README.md"
    text = readme.read_text(encoding="utf-8")
    logger.info("Длина README: %d символов", len(text))
    sections = [
        "## LLM ядро",
        "## Долговременная память",
        "## Проактивность",
        "## Ночная рефлексия",
        "## Ключевые слова",
    ]
    missing = [s for s in sections if s not in text]
    logger.info("Проверяем разделы: %s", sections)
    assert not missing, f"Отсутствуют разделы: {missing}"
