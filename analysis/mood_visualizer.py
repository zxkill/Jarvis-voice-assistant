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
from pathlib import Path

# Сторонние библиотеки
import matplotlib

# Используем неблокирующий backend, чтобы модуль работал без дисплея
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

