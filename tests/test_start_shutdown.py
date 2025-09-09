import signal
import threading
import types
import sys
import pytest


def test_shutdown_cancels_telegram_listener(monkeypatch):
    # Подменяем тяжёлые зависимости до импорта ``start``.
    dummy_morph = types.SimpleNamespace(parse=lambda self, word: [types.SimpleNamespace(normal_form=word)])
    dummy_pymorph = types.SimpleNamespace(MorphAnalyzer=lambda: dummy_morph)
    sys.modules["pymorphy2"] = dummy_pymorph
    sys.modules.setdefault("sounddevice", types.SimpleNamespace())
    cfg = types.SimpleNamespace(
        user=types.SimpleNamespace(telegram_user_id=0),
        telegram=types.SimpleNamespace(token=""),
    )
    monkeypatch.setattr("core.config.load_config", lambda: cfg)

    import start

    # Подготовим фиктивную задачу, чтобы проверить вызов ``cancel``.
    class DummyTask:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    start.tg_task = DummyTask()
    start.tg_stop_event = threading.Event()

    # Вызываем обработчик сигнала и ожидаем завершение через ``SystemExit``.
    with pytest.raises(SystemExit):
        start._shutdown(signal.SIGTERM, None)

    assert start.tg_stop_event.is_set()
    assert start.tg_task.cancelled
    # Сбрасываем флаг для других тестов.
    start._shutdown_flag = threading.Event()


def test_shutdown_does_not_stop_loop(monkeypatch):
    """Проверяем, что ``_shutdown`` не пытается остановить ``event loop``.

    Ранее вызывался ``loop.stop``, что приводило к ``RuntimeError`` при
    использовании ``asyncio.run``. Теперь функция просто завершает процесс
    через ``SystemExit``.
    """

    # Подменяем тяжёлые зависимости до импорта ``start``.
    dummy_morph = types.SimpleNamespace(parse=lambda self, word: [types.SimpleNamespace(normal_form=word)])
    dummy_pymorph = types.SimpleNamespace(MorphAnalyzer=lambda: dummy_morph)
    sys.modules["pymorphy2"] = dummy_pymorph
    sys.modules.setdefault("sounddevice", types.SimpleNamespace())
    cfg = types.SimpleNamespace(
        user=types.SimpleNamespace(telegram_user_id=0),
        telegram=types.SimpleNamespace(token=""),
    )
    monkeypatch.setattr("core.config.load_config", lambda: cfg)

    import start

    start.tg_task = types.SimpleNamespace(cancel=lambda: None)
    start.tg_stop_event = threading.Event()

    # Если ``_shutdown`` попытается получить ``event loop``, мы получим
    # ``RuntimeError``. Успешное выполнение означает, что цикл не трогается.
    monkeypatch.setattr(
        "asyncio.get_event_loop", lambda: (_ for _ in ()).throw(RuntimeError("loop accessed"))
    )

    with pytest.raises(SystemExit):
        start._shutdown(signal.SIGINT, None)

    # Сбрасываем флаг, чтобы не влиять на другие тесты.
    start._shutdown_flag = threading.Event()

