"""Простой инструмент визуализации истории настроения Jarvis.

Модуль читает последние записи таблицы ``mood_history`` из SQLite и
строит график валентности и возбуждённости.  В интерактивном режиме
открывается окно matplotlib, а при запуске на сервере график можно
сохранить в PNG.

Пример использования из командной строки::

    python -m analysis.mood_visualizer --limit 50 --show

Для тестов и автоматизации предусмотрен режим без показа окна,
возвращающий объект ``matplotlib.figure.Figure``.
"""

from __future__ import annotations

# Стандартные библиотеки
import argparse
import logging
import os
from pathlib import Path
import threading
import time

# Сторонние библиотеки
import matplotlib

# Если переменная окружения DISPLAY отсутствует (например, на сервере без X11),
# переключаемся на неблокирующий backend ``Agg``.  Это позволяет выполнять тесты
# и строить графики в headless-окружении.  При наличии DISPLAY будет выбран
# стандартный интерактивный backend, чтобы окна matplotlib открывались
# автоматически.
if os.environ.get("DISPLAY", "") == "":
    matplotlib.use("Agg")  # type: ignore
import matplotlib.pyplot as plt

# Внутренние модули
from memory import db
from core.logging_json import configure_logging


log = configure_logging("analysis.mood_visualizer")


def plot_mood_history(limit: int = 100, show: bool = False, outfile: Path | None = None):
    """Построить график истории настроения и вернуть фигуру.

    :param limit: сколько последних записей извлечь из БД
    :param show: показать ли окно matplotlib (не используется в тестах)
    :param outfile: путь для сохранения PNG; если ``None`` — ничего не
        сохраняется
    :return: объект ``Figure`` с построенным графиком
    """

    # Получаем данные из базы; если записей нет, вызываем исключение
    history = list(reversed(db.get_mood_history(limit)))
    if not history:
        raise ValueError("mood history is empty")

    # Разворачиваем данные в отдельные массивы для построения
    ts = [item["ts"] for item in history]
    valence = [item["valence"] for item in history]
    arousal = [item["arousal"] for item in history]

    log.debug(
        "plot %d points", len(history), extra={"ctx": {"limit": limit}}
    )

    fig, ax = plt.subplots()
    ax.plot(ts, valence, label="valence", color="tab:blue")
    ax.plot(ts, arousal, label="arousal", color="tab:orange")
    ax.set_xlabel("timestamp")
    ax.set_ylabel("value")
    ax.set_title("История настроения Jarvis")
    ax.set_ylim(-1, 1)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best")
    fig.tight_layout()

    if outfile is not None:
        fig.savefig(outfile)
        log.info("saved plot", extra={"ctx": {"path": str(outfile)}})

    if show:
        # Показ окна выполняется только по запросу, чтобы скрипт
        # оставался совместимым с безголовыми окружениями.
        plt.show()

    return fig


def watch_mood_history(
    limit: int = 100,
    interval: float = 1.0,
    stop_event: threading.Event | None = None,
) -> matplotlib.figure.Figure:
    """Отображать график настроения в реальном времени.

    Функция запускается в отдельном потоке и периодически опрашивает БД,
    обновляя кривые валентности и возбуждения.  В интерактивном окружении
    окно matplotlib остаётся открытым до тех пор, пока не будет установлен
    ``stop_event``.

    :param limit: сколько последних точек истории отображать
    :param interval: интервал обновления графика в секундах
    :param stop_event: внешний флаг завершения работы; если ``None`` —
        цикл выполняется бесконечно
    :return: объект ``Figure`` для тестов и дополнительной обработки
    """

    # Подготовка окна и начальный набор данных
    fig, ax = plt.subplots()
    line_v, = ax.plot([], [], label="valence", color="tab:blue")
    line_a, = ax.plot([], [], label="arousal", color="tab:orange")
    ax.set_xlabel("timestamp")
    ax.set_ylabel("value")
    ax.set_title("История настроения Jarvis (реальное время)")
    ax.set_ylim(-1, 1)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best")

    backend = matplotlib.get_backend().lower()
    log.info(
        "realtime mood watch started",
        extra={"ctx": {"limit": limit, "interval": interval, "backend": backend}},
    )
    if backend == "agg":
        # В безголовом режиме отключаем ``plt.pause``, чтобы избежать предупреждений
        log.debug("headless backend detected; using event wait instead of plt.pause")

    def _refresh() -> None:
        """Обновить линии графика свежими данными из БД."""
        history = list(reversed(db.get_mood_history(limit)))
        if not history:
            return
        ts = [item["ts"] for item in history]
        valence = [item["valence"] for item in history]
        arousal = [item["arousal"] for item in history]
        line_v.set_data(ts, valence)
        line_a.set_data(ts, arousal)
        ax.relim()
        ax.autoscale_view()
        fig.canvas.draw_idle()

    # Основной цикл: обновляем график и выполняем паузу ``interval`` секунд.
    interactive = backend != "agg"
    while stop_event is None or not stop_event.is_set():
        _refresh()
        if interactive:
            try:
                plt.pause(interval)
            except Exception:
                # В headless-окружении ``pause`` может выбросить исключение;
                # логируем его и завершаем цикл, чтобы не блокировать ассистента.
                log.exception("matplotlib pause failed")
                break
        else:
            # ``plt.pause`` генерирует предупреждение в backend Agg, поэтому
            # используем ``Event.wait`` или ``sleep`` для корректной задержки.
            if stop_event is None:
                time.sleep(interval)
            else:
                if stop_event.wait(interval):
                    break

    log.info("realtime mood watch stopped")
    return fig


def _parse_args() -> argparse.Namespace:
    """Разобрать аргументы командной строки."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100, help="сколько записей брать из БД")
    parser.add_argument("--show", action="store_true", help="показать окно matplotlib")
    parser.add_argument(
        "--outfile",
        type=Path,
        help="сохранить график в указанный PNG файл",
    )
    return parser.parse_args()


def main() -> None:  # pragma: no cover - CLI точка входа
    """Точка входа для ``python -m analysis.mood_visualizer``."""
    args = _parse_args()
    try:
        plot_mood_history(limit=args.limit, show=args.show, outfile=args.outfile)
    except Exception:
        logging.exception("plot failed")


if __name__ == "__main__":  # pragma: no cover
    main()

