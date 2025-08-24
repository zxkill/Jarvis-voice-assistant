from __future__ import annotations
"""Фоновый цикл обработки событий графического интерфейса.

Модуль запускает асинхронный цикл, который регулярно опрашивает выбранный
драйвер дисплея и передаёт управление GUI из основного event loop.
"""

import asyncio

from display import get_driver
from core.logging_json import configure_logging

# Локальный логгер, чтобы легче отслеживать проблемы с отображением.
log = configure_logging("gui")


async def gui_loop() -> None:
    """Бесконечно обрабатывать события драйвера дисплея.

    Цикл вынесен в отдельную корутину, чтобы не блокировать основной код.
    В случае ошибки она логируется, но цикл продолжает работу.
    """
    drv = get_driver()
    while True:
        try:
            # Обработка событий может блокировать, поэтому выполняем её в потоке.
            await asyncio.to_thread(drv.process_events)
        except Exception:
            log.exception("Ошибка обработки GUI")
        # Небольшая пауза, чтобы не загружать процессор.
        await asyncio.sleep(0.05)
