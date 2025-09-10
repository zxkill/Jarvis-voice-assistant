import asyncio
import sys
import types
from py_trees.blackboard import Blackboard


def test_start_behavior_tree_ticks(monkeypatch):
    """Проверяем, что при запуске стартового дерева обновляется чёрная доска."""

    # Подменяем Telegram-слушатель, чтобы избежать обращения к конфигурации
    dummy_listener = types.SimpleNamespace(launch=lambda stop_event: None)
    monkeypatch.setitem(sys.modules, "notifiers.telegram_listener", dummy_listener)

    # Импортируем модуль `start` после подмены зависимостей
    import start

    async def _run_test():
        # Очищаем глобальную чёрную доску перед запуском
        Blackboard().clear()
        # Запускаем поведенческое дерево с быстрым тиковым интервалом
        start.start_behavior_tree(interval=0.01)
        # Даем дереву время на несколько тиков
        await asyncio.sleep(0.05)
        # Значение `idled` должно появиться в чёрной доске после тика
        assert Blackboard.get("idled") >= 1
        # Останавливаем дерево и очищаем обработчики стопа
        start.stop_mgr.trigger()
        await asyncio.sleep(0)
        start.stop_mgr._handlers.clear()
        # Возвращаем чёрную доску в исходное состояние
        Blackboard().clear()

    asyncio.run(_run_test())
