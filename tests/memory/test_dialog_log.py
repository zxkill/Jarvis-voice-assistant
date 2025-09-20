"""Подробные тесты сервисного слоя ``memory.dialog_log``.

В данном модуле мы проверяем интеграцию с реальной (но временной) SQLite-
базой, корректное обогащение метаданных, фильтрацию истории и обработку
нештатных сценариев. Богатые комментарии и логирование помогают
отслеживать ход тестов при их выполнении и ускоряют диагностику проблем.
"""

from __future__ import annotations

import logging
import sys
from contextvars import Token
from pathlib import Path
from typing import Iterator

import pytest

# В тестовой среде явно добавляем корень репозитория в ``sys.path``, чтобы
# импортировать тестируемые модули так же, как это делает основной код.
sys.path.append(str(Path(__file__).resolve().parents[2]))

from core.logging_json import TRACE_ID  # noqa: E402  # импорт после настройки пути
from memory import dialogs  # noqa: E402
import memory.dialog_log as dialog_log  # noqa: E402


# В тестах используем отдельный логгер, чтобы при необходимости выводить
# отладочные сообщения и видеть контекст выполнения каждого шага.
logger = logging.getLogger(__name__)


@pytest.fixture()
def deterministic_time(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Фикстура, дающая контролируемый поток временных меток.

    ``memory.dialogs`` полагается на ``time.time`` при записи сообщений в БД,
    поэтому для детерминированных проверок мы подменяем функцию
    генератором последовательности значений. Каждый тест, которому нужна
    предсказуемость по времени, может активировать фикстуру и получить
    возрастающую шкалу времени.
    """

    base = 1_700_000_000
    times = iter(range(base, base + 100))

    def fake_time() -> int:
        # Объёмный комментарий подчёркивает, что мы эмулируем системное
        # время, чтобы порядок сообщений в журнале однозначно зависел от
        # нашего сценария, а не от реального времени выполнения тестов.
        value = next(times)
        logger.debug("эмулируем вызов time.time", extra={"ctx": {"ts": value}})
        return value

    monkeypatch.setattr(dialogs.time, "time", fake_time)
    yield
    # После завершения теста среда автоматически вернёт оригинальную
    # реализацию ``time.time`` благодаря механизму ``monkeypatch``.


def test_record_and_fetch_history_filters(dialog_db, deterministic_time):
    """Проверяем запись и фильтрацию истории сообщений по ключевым полям."""

    # Запоминаем trace_id, чтобы затем убедиться в корректной фильтрации.
    shared_trace = "trace-chat-42"
    logger.info("запускаем сценарий записи диалога", extra={"ctx": {"trace": shared_trace}})

    first_id, first_trace = dialog_log.record_dialog_message(
        "Привет, Джарвис!",
        direction="incoming",
        channel="telegram",
        user_id="user-ru",
        trace_id=shared_trace,
        metadata={"intent": "greeting"},
    )
    assert first_id > 0
    assert first_trace == shared_trace

    second_id, second_trace = dialog_log.record_dialog_message(
        "Приветствую, пользователь!",
        direction="outgoing",
        channel="telegram",
        trace_id=shared_trace,
        user_id="user-ru",
        metadata={"reply_to": first_id},
    )
    assert second_id > 0
    assert second_trace == shared_trace

    # Добавляем дополнительное сообщение в другой канал, чтобы проверить,
    # что фильтр по ``channel`` и ``trace_id`` отсекает лишние записи.
    dialog_log.record_dialog_message(
        "Случайное сообщение",
        direction="incoming",
        channel="voice",
        trace_id="trace-random",
    )

    # Получаем историю конкретного диалога и проверяем порядок по времени.
    history = dialog_log.get_dialog_history(
        trace_id=shared_trace,
        channel="telegram",
        ascending=True,
    )
    assert [item.id for item in history] == [first_id, second_id]
    assert [item.direction for item in history] == ["incoming", "outgoing"]
    assert [item.meta["intent"] for item in history[:1]] == ["greeting"]
    assert history[1].meta["reply_to"] == first_id
    assert history[0].meta["status"] == "received"
    assert history[1].meta["status"] == "sent"
    assert history[0].meta["user_id"] == "user-ru"
    assert history[1].meta["user_id"] == "user-ru"


def test_record_message_uses_context_trace(dialog_db):
    """Убеждаемся, что trace_id из контекста подхватывается автоматически."""

    token: Token | None = None
    try:
        token = TRACE_ID.set("trace-from-context")
        message_id, trace = dialog_log.record_dialog_message(
            "Сообщение без явного trace",
            direction="incoming",
            channel="voice",
            metadata={"source": "context"},
        )
    finally:
        if token is not None:
            TRACE_ID.reset(token)

    assert message_id > 0
    assert trace == "trace-from-context"

    # История должна содержать автоматические метаданные: статус и
    # сгенерированного пользователя по каналу "voice".
    history = dialog_log.get_dialog_history(trace_id=trace, channel="voice")
    assert len(history) == 1
    record = history[0]
    assert record.meta["user_id"] == "voice-user"
    assert record.meta["status"] == "received"
    # После ``reset`` ожидаем, что контекстная переменная вернётся в
    # исходное состояние (пустую строку), а не сохранит старое значение.
    assert TRACE_ID.get() == ""


def test_get_history_returns_empty_for_missing_records(dialog_db):
    """Фильтрация по trace_id должна возвращать пустой список при отсутствии записей."""

    # Пишем одно сообщение для контрольного trace_id.
    dialog_log.record_dialog_message(
        "Тестовое сообщение",
        direction="incoming",
        channel="telegram",
        trace_id="trace-existing",
    )

    # Запрашиваем историю по другому trace_id и убеждаемся, что список пуст.
    history = dialog_log.get_dialog_history(
        trace_id="trace-missing",
        channel="telegram",
        direction="incoming",
    )
    assert history == []


def test_record_message_handles_storage_failure(monkeypatch: pytest.MonkeyPatch, dialog_db, caplog):
    """Сервис должен корректно обрабатывать ситуацию, когда запись не сохранилась."""

    caplog.set_level(logging.ERROR, logger="memory.dialog_log")

    def fake_log_message(*args, **kwargs):
        logger.warning("эмулируем сбой при сохранении сообщения")
        return -1

    monkeypatch.setattr(dialog_log, "dialogs", dialogs)
    monkeypatch.setattr(dialog_log.dialogs, "log_message", fake_log_message)

    message_id, trace = dialog_log.record_dialog_message(
        "Сбойная запись",
        direction="incoming",
        channel="telegram",
        trace_id="trace-failure",
    )

    assert message_id == -1
    assert trace == "trace-failure"
    assert TRACE_ID.get() != "trace-failure"


def test_invalid_direction_raises(dialog_db):
    """Неверное направление сообщения должно вызывать ``ValueError``."""

    with pytest.raises(ValueError):
        dialog_log.record_dialog_message(
            "Недопустимое направление",
            direction="sideways",
            channel="telegram",
        )

    with pytest.raises(ValueError):
        dialog_log.get_dialog_history(direction="wrong-way")


def test_normalize_history_entry_without_meta(monkeypatch: pytest.MonkeyPatch, dialog_db):
    """Проверяем заполнение метаданных по умолчанию, если они отсутствуют в БД."""

    fake_row = {
        "id": 99,
        "ts": 1_700_100_000,
        "direction": "outgoing",
        "channel": "voice",
        "trace_id": "trace-empty",
        "text": "Сообщение без меты",
        "meta": {},
    }

    monkeypatch.setattr(dialog_log, "dialogs", dialogs)
    monkeypatch.setattr(dialog_log.dialogs, "fetch_history", lambda **_: [fake_row])

    history = dialog_log.get_dialog_history(trace_id="trace-empty")
    assert len(history) == 1
    record = history[0]
    assert record.meta["channel"] == "voice"
    assert record.meta["user_id"] == "voice-user"
    assert record.meta["status"] == "sent"

