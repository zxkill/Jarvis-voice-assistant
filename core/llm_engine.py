"""Высокоуровневый интерфейс для работы с локальной LLM через Ollama.

Модуль предоставляет функции ``think``, ``act``, ``reflect``, ``summarise`` и
``mood``.  Каждая функция подставляет данные в соответствующий шаблон из
каталога ``prompts`` и отправляет запрос к локальной модели через
:class:`utils.ollama_client.OllamaClient`.

Полученные ответы сохраняются в краткосрочном и долговременном контексте,
что позволяет накапливать знания между вызовами.  Все действия подробно
логируются для удобной отладки.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from context import long_term, short_term
from utils.ollama_client import OllamaClient

logger = logging.getLogger(__name__)

# Путь к каталогу с текстовыми шаблонами
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Единый клиент Ollama для всего модуля
_client = OllamaClient()


def _load_prompt(name: str) -> str:
    """Загрузить шаблон с диска."""
    path = PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8")


def _compose_context() -> str:
    """Собрать краткосрочный контекст в одну строку."""
    return "\n".join(map(str, short_term.get_last()))


def _run(prompt_name: str, profile: str = "light", **kwargs: str) -> str:
    """Универсальный запуск запроса к LLM.

    :param prompt_name: имя файла шаблона без расширения;
    :param profile: ключ профиля генерации (``light``/``heavy``);
    :param kwargs: параметры для подстановки в шаблон.
    """

    template = _load_prompt(prompt_name)
    prompt = template.format(**kwargs)
    logger.debug("Готовый prompt %s: %s", prompt_name, prompt)
    reply = _client.generate(prompt, profile=profile)
    short_term.add({"stage": prompt_name, "text": reply})
    logger.info("Ответ %s: %s", prompt_name, reply)
    return reply


def think(topic: str) -> str:
    """Сформировать размышление по заданной теме."""
    context_text = _compose_context()
    events = "\n".join(long_term.get_events_by_label("think"))
    return _run(
        "think",
        topic=topic,
        context=context_text,
        long_context=events,
        profile="light",
    )


def act(command: str) -> str:
    """Предложить действие на основании команды пользователя."""
    context_text = _compose_context()
    events = "\n".join(long_term.get_events_by_label("act"))
    return _run(
        "act",
        command=command,
        context=context_text,
        long_context=events,
        profile="light",
    )


def reflect(note: str | None = None) -> str:
    """Проанализировать недавний опыт и сделать выводы."""
    context_text = _compose_context()
    events = "\n".join(long_term.get_events_by_label("reflect"))
    return _run(
        "reflect",
        context=context_text,
        note=note or "",
        long_context=events,
        profile="heavy",
    )


def summarise(text: str, labels: Iterable[str] | None = None) -> str:
    """Создать краткое резюме текста и сохранить в долгосрочную память."""
    context_text = _compose_context()
    summary = _run(
        "summarise",
        context=context_text + "\n" + text,
        profile="heavy",
    )
    long_term.add_daily_event(summary, labels or ["summary"])
    return summary


def mood(feeling: str) -> str:
    """Определить настроение на основе текущего ощущения пользователя."""
    context_text = _compose_context()
    history = "\n".join(long_term.get_events_by_label("mood"))
    result = _run(
        "mood",
        context=context_text + ("\n" + history if history else ""),
        feeling=feeling,
        profile="light",
    )
    long_term.add_daily_event(result, ["mood"])
    return result
