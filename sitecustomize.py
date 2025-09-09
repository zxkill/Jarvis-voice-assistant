"""Совместимость для устаревших пакетов, ожидающих ``inspect.getargspec``.

Python 3.11 удалил :func:`inspect.getargspec`, что ломает некоторые
зависимости (например, ``pymorphy2``).  При старте интерпретатор автоматически
загружает этот модуль, поэтому мы подменяем недостающую функцию на
современный аналог :func:`inspect.getfullargspec`.
"""

import inspect

if not hasattr(inspect, "getargspec"):
    # Определяем совместимую обёртку на базе ``getfullargspec``.
    def _getargspec(func):  # type: ignore[override]
        spec = inspect.getfullargspec(func)
        return spec.args, spec.varargs, spec.varkw, spec.defaults

    inspect.getargspec = _getargspec  # type: ignore[attr-defined]
