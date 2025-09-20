"""Тесты сервисного слоя для журнала диалогов."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from memory.dialog_log import get_dialog_history, record_dialog_message


def test_record_dialog_message_enriches_metadata(dialog_db):
    """Проверяем, что сервис добавляет обязательные метаданные."""

    trace = "trace-service"
    message_id, stored_trace = record_dialog_message(
        "Привет",
        direction="incoming",
        channel="telegram",
        user_id=12345,
        trace_id=trace,
        metadata={"purpose": "unit"},
    )
    assert message_id > 0
    assert stored_trace == trace

    history = get_dialog_history(trace_id=trace, channel="telegram")
    assert len(history) == 1
    entry = history[0]
    assert entry.meta["channel"] == "telegram"
    assert entry.meta["user_id"] == "12345"
    assert entry.meta["status"] == "received"
    assert entry.meta["purpose"] == "unit"


def test_record_dialog_message_generates_trace(dialog_db):
    """При отсутствии trace_id сервис генерирует его автоматически."""

    message_id, trace = record_dialog_message(
        "Ответ",
        direction="outgoing",
        channel="voice",
        status="delivered",
        metadata={"emotion": "neutral"},
    )
    assert message_id > 0
    assert trace

    history = get_dialog_history(trace_id=trace, channel="voice")
    assert history
    entry = history[0]
    assert entry.meta["status"] == "delivered"
    assert entry.meta["channel"] == "voice"
    assert entry.meta["emotion"] == "neutral"
    assert entry.meta["user_id"] == "voice-user"
