import pathlib
import sys
import time

# Добавляем корень репозитория в PYTHONPATH для корректного импорта модулей
sys.path.append(str(pathlib.Path(__file__).resolve().parents[2]))

from core import events, stop


def test_proactive_threads_join_on_stop():
    """Проверяет, что фоновые потоки проактивных триггеров
    корректно завершаются при глобальной остановке.
    """

    def slow_handler(event: events.Event) -> None:
        """Имитация долгого обработчика LLM."""
        time.sleep(0.2)

    events._subscribers.clear()
    events.subscribe("proactivity.trigger", slow_handler)

    thread = events.fire_proactive_trigger("time", "demo")
    assert thread.is_alive(), "Поток должен быть запущен"

    # ``stop.trigger`` должен дождаться завершения потока и очистить список
    assert stop.trigger() is True
    assert not thread.is_alive()
    assert events._proactive_threads == []
