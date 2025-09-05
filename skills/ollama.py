#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Скилл для общения с локальной LLM через Ollama."""

from __future__ import annotations

from typing import List

from core import llm_engine
from core.logging_json import configure_logging

# Набор ключевых слов, при которых активируется скилл
PATTERNS: List[str] = [
    "расскажи",
    "объясни",
    "что такое",
    "почему",
    "как",
    "скажи",
]

THRESHOLD = 60  # Порог fuzzy-совпадения (60%)

log = configure_logging("skills.ollama")


def handle(text: str, *, trace_id: str) -> str:
    """Обработать реплику пользователя.

    В зависимости от содержания команды вызывает :func:`llm_engine.think`
    или :func:`llm_engine.act`.  Все параметры и результат подробно
    логируются, чтобы упростить диагностику.
    """

    lower = text.lower()
    try:
        if any(
            lower.startswith(cmd)
            for cmd in ["сделай", "поставь", "запусти", "выполни"]
        ):
            # Команда пользователя подразумевает действие
            log.debug("handle → act", extra={"trace_id": trace_id, "text": text})
            reply = llm_engine.act(text, trace_id=trace_id)
        else:
            # Во всех остальных случаях просим модель "подумать"
            log.debug("handle → think", extra={"trace_id": trace_id, "text": text})
            reply = llm_engine.think(text, trace_id=trace_id)
        return reply.strip()
    except Exception as exc:
        # Если обращение к LLM завершается неудачно, логируем подробности
        log.error(
            "Сбой в работе Ollama: %s", exc, extra={"trace_id": trace_id, "text": text}
        )
        # Возвращаем дружелюбное сообщение пользователю
        return "Извините, сервис генерации текста временно недоступен"
