import asyncio
import sys
import types
from dataclasses import dataclass
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


def test_behavior_tree_shutdown(monkeypatch):
    """Убеждаемся, что при остановке вызывается штатный ``shutdown`` дерева."""

    # Делаем минимальную заглушку дерева, чтобы отследить вызовы без запуска
    # реальных зависимостей и логики из ``py_trees``.
    @dataclass
    class DummyTree:
        ticks: int = 0
        shutdown_called: int = 0

        def tick(self):
            self.ticks += 1

        def shutdown(self):
            self.shutdown_called += 1

    dummy_listener = types.SimpleNamespace(launch=lambda stop_event: None)
    monkeypatch.setitem(sys.modules, "notifiers.telegram_listener", dummy_listener)

    import start

    async def _run_test():
        Blackboard().clear()
        tree = DummyTree()
        # Подменяем фабрику дерева на заглушку
        monkeypatch.setattr(start, "create_behavior_tree", lambda: tree)

        start.start_behavior_tree(interval=0.01)
        await asyncio.sleep(0.03)
        # Проверяем, что дерево действительно тикает
        assert tree.ticks > 0

        # Стоп-обработчик должен вызвать ``shutdown`` ровно один раз
        start.stop_mgr.trigger()
        await asyncio.sleep(0)
        assert tree.shutdown_called == 1

        # Повторный вызов не должен приводить к повторному shutdown
        start.stop_mgr.trigger()
        await asyncio.sleep(0)
        assert tree.shutdown_called == 1

        # Чистим состояние между тестами
        start.stop_mgr._handlers.clear()
        Blackboard().clear()

    asyncio.run(_run_test())
