"""Навык обработки общей команды «стоп».

Команда предназначена для прерывания текущей озвучки и остановки активных
действий в других навыках.  Сам навык ничего не произносит в ответ,
чтобы не мешать пользователю сразу отдавать новые команды.
"""
from __future__ import annotations

from core import stop as _stop_mgr

try:  # импортируем TTS только при наличии зависимостей
    from working_tts import stop_speaking as _tts_stop
except Exception:  # pragma: no cover - fallback, если TTS недоступен
    def _tts_stop() -> None:  # type: ignore
        pass

PATTERNS = ["стоп"]


def handle(text: str) -> str:
    """Главная функция навыка: останавливает озвучку и активные действия."""

    _tts_stop()            # прекращаем произнесение текущего текста
    _stop_mgr.trigger()    # уведомляем другие навыки
    return ""             # ничего не озвучиваем в ответ
