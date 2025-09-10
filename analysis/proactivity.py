"""Генерация проактивных подсказок из YAML-плейбука без участия LLM.

Модуль читает сценарии из файла ``playbook.yaml`` и формирует текст
подсказок напрямую, что позволяет работать офлайн и ускоряет отклик.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

# Подключаем чтение статистики по отзывам на подсказки
from memory.reader import get_feedback_stats, was_event_triggered
# Универсальные помощники для поиска дат событий пользователя
from memory import events as user_events

from core.logging_json import configure_logging
from core.events import Event, publish, subscribe
# Сохраняем сгенерированные подсказки в базу, чтобы позже учитывать
# реакцию пользователя. Это обеспечивает уникальный ``suggestion_id``
# для каждой реплики и позволяет адаптировать политику.
from memory.writer import add_suggestion

log = configure_logging("analysis.proactivity")

PLAYBOOK_PATH = Path(__file__).resolve().parent.parent / "proactive" / "playbook.yaml"


def load_playbook(path: Path | None = None) -> Dict[str, Any]:
    """Загрузить сценарии из YAML-плейбука.

    Плейбук описывает возможные подсказки и условия их запуска.
    Возвращается словарь ``имя_сценария -> параметры``.
    """

    path = path or PLAYBOOK_PATH
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        log.warning("playbook missing", extra={"ctx": {"path": str(path)}})
        return {}
    return data.get("scenarios", {})


def feedback_acceptance_ratio() -> Dict[str, float]:
    """Рассчитать долю принятых и отклонённых подсказок.

    Таблица ``suggestion_feedback`` накапливает ответы пользователя на
    проактивные предложения. Функция подсчитывает количество принятых и
    отклонённых подсказок и возвращает их долю. Значения находятся в
    диапазоне ``0..1``. При отсутствии данных возвращаются нули.
    """

    # Получаем агрегированную статистику по отзывам из слоя памяти
    stats = get_feedback_stats()
    accepted = stats.get("accepted", 0)
    rejected = stats.get("rejected", 0)
    total = accepted + rejected
    if total == 0:
        log.info("no feedback yet")
        return {"accepted": 0.0, "rejected": 0.0}

    accepted_share = accepted / total
    rejected_share = rejected / total
    # Логируем рассчитанные показатели для удобной диагностики
    log.info(
        "feedback ratio", 
        extra={
            "ctx": {
                "accepted_share": round(accepted_share, 3),
                "rejected_share": round(rejected_share, 3),
            }
        },
    )
    return {"accepted": accepted_share, "rejected": rejected_share}


def _handle_trigger(event: Event) -> None:
    """Обработать событие проактивного триггера и сгенерировать подсказку."""

    name = event.attrs.get("name")
    playbook = load_playbook()
    scenario = playbook.get(name)
    if not scenario:
        log.warning("unknown scenario", extra={"ctx": {"name": name}})
        return
    prompt = scenario.get("prompt", "")
    context = event.attrs.get("context", {})
    trace_id = event.attrs.get("trace_id")
    # Без обращения к LLM используем текст плейбука напрямую.
    code = scenario.get("code", "")
    text_template = scenario.get("text") or prompt
    try:
        text = text_template.format(**context) if context else text_template
    except Exception:
        text = text_template

    # Если LLM вернул код события и он уже встречался недавно, пропускаем
    # дальнейшую обработку, чтобы не спамить пользователя повторными советами.
    if code and was_event_triggered(code):
        log.info("event already triggered", extra={"ctx": {"code": code}})
        return

    # Фильтрация преждевременных поздравлений и других датированных событий
    t_low = text.lower()
    if "с днем рождения" in t_low or "с днём рождения" in t_low:
        # Проверяем, действительно ли праздник скоро наступит
        if not user_events.is_event_soon("день рожд"):
            log.info(
                "skip birthday greeting", extra={"ctx": {"text": text, "name": name}}
            )
            return

    # Сохраняем подсказку в БД, чтобы получить её уникальный идентификатор.
    suggestion_id = add_suggestion(text, code or name)
    if suggestion_id is None:
        log.info(
            "duplicate suggestion", extra={"ctx": {"text": text, "name": name}}
        )
        return

    log.info(
        "suggestion generated",
        extra={
            "ctx": {
                "name": name,
                "suggestion_id": suggestion_id,
                "trace_id": trace_id,
            }
        },
    )

    # Публикуем событие с текстом подсказки и ``suggestion_id``. Наличие
    # идентификатора позволяет ``ProactiveEngine`` войти в режим ожидания
    # ответа и корректно обрабатывать фидбэк пользователя.
    publish(
        Event(
            "suggestion.created",
            {
                "text": text,
                "reason_code": code or name,
                "suggestion_id": suggestion_id,
                "trace_id": trace_id,
            },
        )
    )


# Подписываемся на события триггеров при импортировании модуля.
subscribe("proactivity.trigger", _handle_trigger)
