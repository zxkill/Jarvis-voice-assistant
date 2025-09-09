from __future__ import annotations

import logging
from display import DisplayDriver, DisplayItem


class NoneDisplayDriver(DisplayDriver):
    """Тихий драйвер дисплея, полностью отключающий вывод."""

    def __init__(self) -> None:
        # Логгер позволяет отслеживать факт инициализации драйвера
        self._log = logging.getLogger(__name__)
        self._log.info("Дисплей отключён: используется заглушечный драйвер")

    def draw(self, item: DisplayItem) -> None:  # noqa: D401
        """Игнорируем любые команды на отрисовку."""
        self._log.debug("Игнорируем элемент %s", item)

    def process_events(self) -> None:  # noqa: D401
        """События отсутствуют, поэтому метод пустой."""
        return
