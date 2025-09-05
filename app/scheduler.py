from __future__ import annotations
"""Запуск фоновых задач: подсказки пользователю и агрегация привычек.

Планировщик запускается из стартового скрипта и создаёт две асинхронные задачи:
генератор подсказок и ежедневное суммирование данных о привычках.
"""

import asyncio
import datetime as dt

from analysis import suggestions as analysis_suggestions
from analysis.habits import schedule_daily_aggregation
from analysis.proactivity import load_playbook
from core.logging_json import configure_logging
from core import llm_engine
from core.events import Event, publish, fire_proactive_trigger, subscribe
from memory import db as memory_db

# Отдельный логгер для задач планировщика
log = configure_logging("scheduler")


def start_background_tasks(suggestion_interval: int) -> None:
    """Запустить задачи генерации подсказок и ежедневной агрегации."""

    async def suggestion_scheduler() -> None:
        """Периодически вызывать генератор подсказок."""
        while True:
            try:
                analysis_suggestions.generate()
            except Exception:
                log.exception("suggestion generation failed")
            # Засыпаем на заданный интервал перед следующей проверкой
            await asyncio.sleep(suggestion_interval)

    # Создаём корутины в фоне, чтобы не блокировать основной поток
    asyncio.create_task(suggestion_scheduler())
    asyncio.create_task(schedule_daily_aggregation())
    asyncio.create_task(nightly_reflect())
    asyncio.create_task(_schedule_playbook())


def _run_nightly_reflection() -> None:
    """Выполнить ночную рефлексию один раз."""
    result = llm_engine.reflect()
    if isinstance(result, dict):
        digest = str(result.get("digest", ""))
        priorities = result.get("priorities")
        mood = result.get("mood")
    else:
        digest = str(result)
        priorities = None
        mood = None
    memory_db.add_daily_digest(digest, priorities, mood)
    log.info(
        "daily digest saved",
        extra={"ctx": {"length": len(digest)}},
    )
    if priorities:
        memory_db.set_priorities(str(priorities))
        log.debug(
            "priorities updated",
            extra={"ctx": {"priorities": priorities}},
        )
    if mood is not None:
        try:
            memory_db.set_mood_level(int(mood))
        except Exception:
            log.exception("failed to set mood", extra={"ctx": {"mood": mood}})
    publish(
        Event(
            kind="suggestion.created",
            attrs={"text": digest, "reason_code": "daily_digest"},
        )
    )
    log.info("daily digest notification sent")


async def nightly_reflect() -> None:
    """Планировщик ночной рефлексии."""
    while True:
        now = dt.datetime.now()
        target = now.replace(hour=23, minute=55, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        sleep_for = (target - now).total_seconds()
        log.debug(
            "sleep before nightly reflection",
            extra={"ctx": {"sleep_sec": int(sleep_for)}},
        )
        await asyncio.sleep(sleep_for)
        try:
            _run_nightly_reflection()
        except Exception:
            log.exception("nightly reflection failed")


# ---------------------------------------------------------------------------
async def _schedule_playbook() -> None:
    """Настроить триггеры плейбука проактивности."""

    playbook = load_playbook()

    async def _schedule_time(name: str, hhmm: str) -> None:
        """Ежедневный запуск сценария по времени ``HH:MM``."""
        while True:
            now = dt.datetime.now()
            hour, minute = map(int, hhmm.split(":"))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += dt.timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            fire_proactive_trigger("time", name)

    for name, cfg in playbook.items():
        trig = cfg.get("trigger")
        if trig == "time" and cfg.get("time"):
            asyncio.create_task(_schedule_time(name, cfg["time"]))
        elif cfg.get("event"):
            event_name = cfg["event"]

            def _handler(event: Event, *, _n=name, _t=trig) -> None:
                fire_proactive_trigger(_t, _n, event.attrs)

            subscribe(event_name, _handler)
