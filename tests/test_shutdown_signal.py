import sys
import signal

import start
from core import stop as stop_mgr


class FakeLoop:
    """Минимальный event loop для проверки остановки."""

    def __init__(self):
        self.stop_called = False
        self.call_soon_args = None

    def stop(self):
        self.stop_called = True

    def call_soon_threadsafe(self, callback, *args):
        # Запоминаем, какой колбэк был запрошен к исполнению, чтобы убедиться,
        # что приложение пытается завершить event loop корректно.
        self.call_soon_args = (callback, args)
        # Имитация немедленного выполнения для упрощения тестов.
        callback(*args)


class FakeTask:
    """Заглушка для asyncio.Task, фиксирует факт отмены."""

    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def reset_shutdown_state():
    """Сбрасывает глобальные флаги, чтобы тесты были независимыми."""

    start._shutdown_flag.clear()
    start.tg_stop_event.clear()
    start.tg_task = None
    start._main_loop = None


def test_shutdown_requests_loop_stop(monkeypatch):
    """Обработчик сигнала должен аккуратно остановить event loop."""

    reset_shutdown_state()
    fake_loop = FakeLoop()
    start._main_loop = fake_loop

    # Подменяем trigger, чтобы избежать побочных эффектов реальных обработчиков
    stop_calls = {}

    def fake_trigger():
        stop_calls["called"] = True
        return True

    monkeypatch.setattr(stop_mgr, "trigger", fake_trigger)

    start._shutdown(signum=signal.SIGINT, frame=None)

    assert start._shutdown_flag.is_set(), "Флаг повторной обработки должен быть поднят"
    assert start.tg_stop_event.is_set(), "Telegram-слушатель должен получить сигнал остановки"
    assert fake_loop.stop_called, "Должна быть запрошена остановка event loop"
    assert stop_calls.get("called"), "Должен быть вызван реестр остановки подсистем"


def test_shutdown_cancels_telegram_task(monkeypatch):
    """Если Telegram-слушатель активен, его нужно отменять."""

    reset_shutdown_state()
    fake_loop = FakeLoop()
    start._main_loop = fake_loop
    start.tg_task = FakeTask()

    monkeypatch.setattr(stop_mgr, "trigger", lambda: True)

    start._shutdown(signum=signal.SIGTERM, frame=None)

    assert start.tg_task.cancelled, "Telegram-задача должна быть отменена"
    assert fake_loop.stop_called, "Остановка event loop должна быть запрошена"


def test_shutdown_exits_when_loop_missing(monkeypatch):
    """При отсутствии loop должен вызываться sys.exit для быстрого завершения."""

    reset_shutdown_state()
    monkeypatch.setattr(stop_mgr, "trigger", lambda: True)

    exit_called = {}

    def fake_exit(code=0):
        exit_called["code"] = code
        raise SystemExit(code)

    monkeypatch.setattr(sys, "exit", fake_exit)

    try:
        start._shutdown(signum=signal.SIGINT, frame=None)
    except SystemExit:
        pass

    assert exit_called.get("code") == 0, "Ожидали завершение процесса через sys.exit(0)"
