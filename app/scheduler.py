from __future__ import annotations
"""Запуск фоновых задач: подсказки пользователю и агрегация привычек.

Планировщик запускается из стартового скрипта и создаёт две асинхронные задачи:
генератор подсказок и ежедневное суммирование данных о привычках.
"""

import asyncio

from analysis import suggestions as analysis_suggestions
from analysis.habits import schedule_daily_aggregation
from core.logging_json import configure_logging

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
