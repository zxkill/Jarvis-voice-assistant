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

    # Замещаем sys.exit, чтобы не завершать процесс.
    monkeypatch.setattr(start.sys, "exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))

    with pytest.raises(SystemExit):
        start._shutdown(signal.SIGTERM, None)

    assert start.tg_stop_event.is_set()
    assert start.tg_task.cancelled

