"""Генерация проактивных подсказок через LLM."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import yaml

# Подключаем чтение статистики по отзывам на подсказки
from memory.reader import get_feedback_stats

from core.logging_json import configure_logging
from core import llm_engine
from core.events import Event, publish, subscribe

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
    context_json = json.dumps(context, ensure_ascii=False)
    final_prompt = f"{prompt}\nКонтекст: {context_json}" if context else prompt
    trace_id = event.attrs.get("trace_id")
    try:
        text = llm_engine.act(final_prompt, trace_id=trace_id)
    except Exception as exc:  # pragma: no cover - диагностика сетевых ошибок
        log.exception("llm failure", extra={"ctx": {"err": str(exc)}})
        return
    log.info(
        "suggestion generated",
        extra={"ctx": {"name": name, "trace_id": trace_id}},
    )
    publish(Event("suggestion.created", {"text": text, "reason_code": name}))


# Подписываемся на события триггеров при импортировании модуля.
subscribe("proactivity.trigger", _handle_trigger)
