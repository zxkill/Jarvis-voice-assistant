"""Локальное преобразование текста в векторные представления.

Модуль обеспечивает две схемы построения эмбеддингов:

``simple``
    Быстрое и полностью офлайновое хеширование слов. Этот вариант не
    требует сторонних библиотек и всегда доступен.

``transformer``
    Использование локальной модели `sentence-transformers`. Если
    библиотека или веса модели недоступны, модуль автоматически
    переключается на упрощённый режим ``simple``.

Тип профиля выбирается через секцию ``[EMBEDDINGS]`` в ``config.ini``.
Такой подход позволяет экспериментировать с более качественными
эмбеддингами, сохраняя гарантированный fallback на лёгкое хеширование.
"""

from __future__ import annotations

# Стандартные библиотеки
import hashlib
import logging
from typing import List, Optional
from pathlib import Path
from configparser import ConfigParser

import numpy as np

# Логгер для отслеживания вычислений эмбеддингов
logger = logging.getLogger(__name__)

# ────────── Конфигурация ──────────
# Загружаем профиль эмбеддингов из config.ini один раз при импорте.
_cfg = ConfigParser()
_cfg.read(Path(__file__).resolve().parents[1] / "config.ini", encoding="utf-8")
EMBEDDING_PROFILE = _cfg.get("EMBEDDINGS", "profile", fallback="simple").strip().lower()

# Имя модели по умолчанию для sentence-transformers. При необходимости
# его можно заменить через дополнительные опции конфигурации.
DEFAULT_MODEL_NAME = "paraphrase-MiniLM-L3-v2"

# Кэш для загруженной трансформерной модели, чтобы не тратить время на
# повторную инициализацию.
_MODEL: Optional[object] = None

# Размерность вектора по умолчанию. Увеличенное значение снижает
# вероятность коллизий при хешировании и повышает качество поиска.
# Большая размерность практически исключает коллизии между словами
# даже в небольших корпусах текста.
VECTOR_SIZE = 4096


def _hash_embedding(text: str, *, size: int = VECTOR_SIZE) -> List[float]:
    """Построить эмбеддинг методом простого хеширования.

    Эта функция выделена отдельно, чтобы её можно было переиспользовать,
    например, при тестировании или в качестве fallback для сложных
    моделей.
    """

    vec = np.zeros(size, dtype=float)
    logger.debug("Хеш‑эмбеддинг для текста: %r", text)
    for word in text.lower().split():
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "little") % size
        vec[idx] += 1.0
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    logger.debug("Результат хеширования: %s", vec)
    return vec.tolist()


def _load_model() -> Optional[object]:
    """Загрузить и кэшировать локальную модель ``sentence-transformers``.

    При любой ошибке загрузки возвращается ``None`` и вызывающий код
    должен перейти на резервный режим хеширования.
    """

    global _MODEL
    if _MODEL is not None:
        return _MODEL
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore

        logger.info(
            "Загружается трансформерная модель эмбеддингов: %s", DEFAULT_MODEL_NAME
        )
        _MODEL = SentenceTransformer(DEFAULT_MODEL_NAME)
    except Exception as exc:  # pragma: no cover - защитный блок
        logger.warning("Не удалось загрузить модель, fallback на simple: %s", exc)
        _MODEL = None
    return _MODEL


def get_embedding(text: str, *, size: int = VECTOR_SIZE) -> List[float]:
    """Преобразовать *text* в вектор фиксированной длины.

    Выбор алгоритма зависит от глобальной переменной
    :data:`EMBEDDING_PROFILE`, загруженной из ``config.ini``. В случае
    проблем с моделью ``transformer`` автоматически используется
    резервный алгоритм ``simple``.
    """

    logger.debug(
        "Построение эмбеддинга профиля %s для текста: %r",
        EMBEDDING_PROFILE,
        text,
    )

    if EMBEDDING_PROFILE == "transformer":
        model = _load_model()
        if model is not None:
            try:
                vec = model.encode([text])[0]
                logger.debug("Трансформер вернул вектор размерности %d", len(vec))
                return list(map(float, vec))
            except Exception as exc:  # pragma: no cover - на случай ошибок модели
                logger.warning("Ошибка при получении эмбеддинга: %s", exc)

    # Fallback на простое хеширование
    return _hash_embedding(text, size=size)
