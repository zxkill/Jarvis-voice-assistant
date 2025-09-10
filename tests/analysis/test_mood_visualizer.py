"""Тесты для модуля визуализации настроения."""

from pathlib import Path
import sys
import threading

import matplotlib.figure

# Добавляем корневую директорию в ``sys.path``, чтобы корректно импортировать пакет ``analysis``
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import analysis.mood_visualizer as mv
from analysis.mood_visualizer import plot_mood_history, watch_mood_history


class DummyDB:
    """Простая заглушка для ``memory.db`` в тестах."""

    def __init__(self, data):
        self._data = data

    def get_mood_history(self, limit: int):  # pragma: no cover - интерфейс заглушки
        return self._data[:limit]


def test_plot_mood_history(monkeypatch, tmp_path):
    """График строится и сохраняется без ошибок."""

    # Подготовим фиктивные данные истории настроения
    data = [
        {"ts": 1, "valence": -0.2, "arousal": 0.1},
        {"ts": 2, "valence": 0.0, "arousal": 0.0},
        {"ts": 3, "valence": 0.5, "arousal": -0.3},
    ]
    dummy = DummyDB(data)

    # Подменяем реальный модуль памяти нашей заглушкой
    monkeypatch.setattr(mv, "db", dummy)

    outfile = tmp_path / "plot.png"
    fig = plot_mood_history(limit=10, show=False, outfile=outfile)

    assert isinstance(fig, matplotlib.figure.Figure)
    assert outfile.exists()


def test_plot_mood_history_empty(monkeypatch):
    """При отсутствии данных выбрасывается ожидаемое исключение."""

    dummy = DummyDB([])
    monkeypatch.setattr(mv, "db", dummy)

    try:
        plot_mood_history(show=False)
    except ValueError as exc:  # ожидаемое исключение
        assert "empty" in str(exc)
    else:  # pragma: no cover - ошибка теста
        assert False, "ValueError не был возбуждён"


def test_watch_mood_history(monkeypatch):
    """Цикл наблюдения корректно обновляет график и завершает работу."""

    data = [
        {"ts": 1, "valence": 0.1, "arousal": 0.2},
        {"ts": 2, "valence": -0.1, "arousal": -0.2},
    ]
    dummy = DummyDB(data)
    monkeypatch.setattr("analysis.mood_visualizer.db", dummy)

    # Переопределяем backend на интерактивный, чтобы использовался ``plt.pause``
    monkeypatch.setattr(mv.matplotlib, "get_backend", lambda: "qt5agg")

    # Создаём событие, которое остановит цикл после первого обновления
    stop = threading.Event()

    def fake_pause(_):
        stop.set()  # останавливаем цикл после первой итерации

    monkeypatch.setattr(mv.plt, "pause", fake_pause)
    monkeypatch.setattr(mv.plt, "show", lambda: None)

    fig = watch_mood_history(limit=10, interval=0.01, stop_event=stop)
    assert isinstance(fig, matplotlib.figure.Figure)


def test_watch_mood_history_headless(monkeypatch):
    """В backend Agg ``plt.pause`` не вызывается, используется ожидание события."""

    data = [
        {"ts": 1, "valence": 0.1, "arousal": 0.2},
        {"ts": 2, "valence": -0.1, "arousal": -0.2},
    ]
    dummy = DummyDB(data)
    monkeypatch.setattr(mv, "db", dummy)

    # Форсируем использование безголового backend
    monkeypatch.setattr(mv.matplotlib, "get_backend", lambda: "agg")

    # Если ``plt.pause`` будет вызван, тест упадёт
    def fail_pause(_):  # pragma: no cover - проверка на ошибку
        raise AssertionError("pause should not be called in headless mode")

    monkeypatch.setattr(mv.plt, "pause", fail_pause)

    class Stopper:
        """Объект события, завершающий цикл после первой итерации."""

        def __init__(self) -> None:
            self._set = False

        def is_set(self) -> bool:
            return self._set

        def set(self) -> None:
            self._set = True

        def wait(self, _: float) -> bool:
            self._set = True
            return True

    stop = Stopper()
    fig = watch_mood_history(limit=10, interval=0.01, stop_event=stop)
    assert isinstance(fig, matplotlib.figure.Figure)

